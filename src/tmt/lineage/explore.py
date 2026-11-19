"""Dependency exploration around one *master job*: what it starts, what it reads and writes, which databases it uses.

Model-only (works for any parser). The numbers are the ones a customer asks for:

* **sources** and **targets**: the distinct tables/files read and written by the master and every job it starts
  (directly or through other jobs), so one table is counted once however many jobs touch it;
* **database connections**: how many components use each connection, by name (``10x ODS_DB``), and how many jobs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import networkx as nx

from tmt.lineage import builder
from tmt.models import Category, Job, TableRef

LIST_LIMIT = 40


@dataclass(frozen=True)
class ConnectionUse:
    name: str
    kind: str  # repository | inferred | built_in | unresolved
    uses: int  # DB components using it
    jobs: int  # distinct jobs using it
    inferred: int = 0  # of those uses, how many were recognised from context variables only (no explicit link)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "uses": self.uses, "jobs": self.jobs, "inferred": self.inferred}


@dataclass
class Exploration:
    master: str | None  # None = the whole workspace
    jobs: list[Job]  # the jobs shown (the master's hierarchy, plus related ones when asked)
    graph: nx.DiGraph
    summary: dict[str, Any]
    details: dict[str, dict[str, Any]] = field(default_factory=dict)


def _label(r: TableRef) -> str:
    q = ".".join(p for p in (r.database, r.db_schema, r.name) if p)
    return q or r.expression.raw


def table_key(r: TableRef) -> str:
    return _label(r).upper()


def _distinct(refs: list[TableRef]) -> dict[str, TableRef]:
    out: dict[str, TableRef] = {}
    for r in refs:
        out.setdefault(table_key(r), r)
    return out


def hierarchy(jobs: list[Job], master: str) -> list[Job]:
    """The master plus every job it starts, directly or through other jobs. Stable order, no duplicates."""
    by_key = {j.key: j for j in jobs}
    if master not in by_key:
        raise KeyError(master)
    seen: dict[str, Job] = {}
    queue = [master]
    while queue:
        key = queue.pop(0)
        if key in seen or key not in by_key:
            continue
        seen[key] = by_key[key]
        queue += [c.key for c in by_key[key].child_jobs if c.key]
    return list(seen.values())


def connection_usage(jobs: list[Job]) -> list[ConnectionUse]:
    """How often each database connection is used. A connection component only *defines* one, so it is not counted."""
    uses: dict[tuple[str, str], list[str]] = {}
    for j in jobs:
        for c in j.components:
            if c.connection is None or c.category is Category.CONNECTION:
                continue
            uses.setdefault((c.connection.name, c.connection.kind), []).append(j.key)
    by_name: dict[str, dict[str, list[str]]] = {}
    for (name, kind), where in uses.items():  # an inferred and a repository use of the same name are one connection
        by_name.setdefault(name, {})[kind] = where
    result = []
    for name, kinds in by_name.items():
        every = [k for where in kinds.values() for k in where]
        kind = "repository" if "repository" in kinds else next(iter(kinds))
        result.append(ConnectionUse(name, kind, len(every), len(set(every)), len(kinds.get("inferred", []))))
    return sorted(result, key=lambda c: (-c.uses, c.name))


def masters(jobs: list[Job]) -> list[dict[str, Any]]:
    """Jobs to offer as a starting point: those that start others and are started by nobody first, then the rest."""
    called = {c.key for j in jobs for c in j.child_jobs if c.key}
    by_key = {j.key for j in jobs}
    rows = []
    for j in jobs:
        size = len(hierarchy(jobs, j.key)) - 1
        cx = j.complexity
        direct = [c.key for c in j.child_jobs if c.key and c.key in by_key and c.key != j.key]
        rows.append({"key": j.key, "name": j.name, "project": j.project, "starts": size,
                     "children": sorted(dict.fromkeys(direct), key=str.upper),
                     "is_master": size > 0 and j.key not in called,
                     "level": cx.level if cx else None, "score": cx.score if cx else None})
    return sorted(rows, key=lambda r: (not r["is_master"], -r["starts"], r["key"].upper()))


def _names(items: dict[str, TableRef]) -> tuple[list[str], int]:
    names = sorted((_label(r) for r in items.values()), key=str.upper)
    return names[:LIST_LIMIT], len(names)


def _totals(jobs: list[Job]) -> dict[str, Any]:
    reads = _distinct([r for j in jobs for r in j.reads])
    writes = _distinct([w for j in jobs for w in j.writes])
    src, n_src = _names(reads)
    tgt, n_tgt = _names(writes)
    both = len(set(reads) & set(writes))
    return {"sources": n_src, "targets": n_tgt, "source_names": src, "target_names": tgt, "intermediate": both}


def _conn_dicts(jobs: list[Job]) -> list[dict[str, Any]]:
    return [c.to_dict() for c in connection_usage(jobs)]


NODE_TYPES = ("job", "table", "file")


def collapse(g: nx.DiGraph, hide: set[str]) -> nx.DiGraph:
    """The graph without the node types in ``hide``, keeping what they connected.

    Walking through a hidden node joins what feeds it to what it feeds, so hiding tables turns ``A -> T -> B`` into
    ``A -> B`` (job order through shared tables) and hiding jobs turns ``T1 -> J -> T2`` into ``T1 -> T2`` (table
    lineage). Edges that existed before keep their kind (``runs`` stays ``runs``); the new ones are ``flow``.
    """
    if not hide:
        return g
    out: nx.DiGraph = nx.DiGraph()
    visible = {n for n, a in g.nodes(data=True) if a["type"] not in hide}
    for n in g.nodes:
        if n in visible:
            out.add_node(n, **g.nodes[n])
    for n in visible:
        seen: set[str] = set()
        stack = list(g.successors(n))
        while stack:
            v = stack.pop()
            if v in seen:
                continue
            seen.add(v)
            if v in visible:
                if v == n or out.has_edge(n, v):
                    continue
                if g.has_edge(n, v):
                    out.add_edge(n, v, **g[n][v])
                else:
                    out.add_edge(n, v, kind="flow", actions=[], resolved=True)
            else:
                stack.extend(g.successors(v))
    return out


def explore(all_jobs: list[Job], master: str | None, related: bool = False) -> Exploration:
    """Everything the page needs for one master job (or the whole workspace when ``master`` is None)."""
    if master is None:
        shown = list(all_jobs)
        title = None
    else:
        shown = hierarchy(all_jobs, master)
        title = master
        if related:  # jobs outside the hierarchy that read or write the same tables
            touched = {table_key(r) for j in shown for r in (*j.reads, *j.writes)}
            keys = {j.key for j in shown}
            shown += [j for j in all_jobs if j.key not in keys
                      and touched & {table_key(r) for r in (*j.reads, *j.writes)}]
    graph = builder.build_graph(shown)
    by_key = {j.key: j for j in all_jobs}

    details: dict[str, dict[str, Any]] = {}
    for node, attrs in graph.nodes(data=True):
        if attrs["type"] == "job":
            job = by_key.get(str(attrs["label"]))
            if job is None:
                details[node] = {"type": "job", "title": str(attrs["label"]), "external": True}
                continue
            tree = hierarchy(all_jobs, job.key)
            own = _totals([job])
            sub = _totals(tree)
            details[node] = {
                "type": "job", "title": job.key, "project": job.project,
                "level": job.complexity.level if job.complexity else None,
                "score": job.complexity.score if job.complexity else None,
                "flags": len(job.issues), "components": len(job.components),
                "own": {"sources": own["sources"], "targets": own["targets"]},
                "sub_jobs": len(tree) - 1,
                "total": {"sources": sub["sources"], "targets": sub["targets"]},
                "source_names": sub["source_names"], "target_names": sub["target_names"],
                "connections": _conn_dicts(tree), "is_master": job.key == master,
            }
        else:
            readers = sorted(graph.nodes[s]["label"] for s in graph.successors(node))
            writers = sorted(graph.nodes[p]["label"] for p in graph.predecessors(node))
            details[node] = {"type": attrs["type"], "title": str(attrs["label"]), "readers": readers[:LIST_LIMIT],
                             "writers": writers[:LIST_LIMIT], "n_readers": len(readers), "n_writers": len(writers),
                             "unresolved": not attrs["resolved"]}

    totals = _totals(shown)
    conns = connection_usage(shown)
    summary: dict[str, Any] = {
        "master": title, "jobs": len(shown), "sub_jobs": (len(hierarchy(all_jobs, master)) - 1) if master else None,
        "related_jobs": (len(shown) - len(hierarchy(all_jobs, master))) if master else 0,
        **totals,
        "connections": [c.to_dict() for c in conns],
        "distinct_connections": len(conns),
        "total_uses": sum(c.uses for c in conns),
        "flags": sum(len(j.issues) for j in shown),
        "levels": {lv: sum(1 for j in shown if j.complexity and j.complexity.level == lv)
                   for lv in ("easy", "medium", "hard")},
    }
    return Exploration(master, shown, graph, summary, details)
