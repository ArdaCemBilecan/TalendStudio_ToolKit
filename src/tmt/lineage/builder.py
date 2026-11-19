"""Lineage graph: job and table nodes, read/write/runs edges, ordering and renderers.

Edges: ``table -> job`` (job reads), ``job -> table`` (job writes), ``parent -> child`` (tRunJob).
Table names that depend on an unresolved context variable are flagged ``resolved=False`` and keep
their raw expression; a name folded with the selected context group is still flagged, because it can
differ at runtime.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import networkx as nx

from tmt.models import Job, Resolution, TableRef


# ------------------------------------------------------------------ building
def _table_id(ref: TableRef) -> tuple[str, str, str]:
    """(node id, kind, label). Names are matched case-insensitively; the first spelling is the label."""
    kind = "file" if ref.via == "file" else "table"
    if ref.name:
        label = ".".join(p for p in (ref.database, ref.db_schema, ref.name) if p)
        return f"{kind}:{label.upper()}", kind, label
    return f"{kind}:?{ref.expression.raw}", kind, ref.expression.raw


def build_graph(jobs: list[Job]) -> nx.DiGraph:
    g: nx.DiGraph = nx.DiGraph()
    keys = {j.key for j in jobs}
    for job in sorted(jobs, key=lambda j: j.key):
        jid = f"job:{job.key}"
        g.add_node(jid, type="job", label=job.key, resolved=True, path=job.path)
        for refs, direction in ((job.reads, "read"), (job.writes, "write")):
            for ref in refs:
                tid, kind, label = _table_id(ref)
                resolved = ref.resolution is Resolution.RESOLVED
                if tid not in g:
                    g.add_node(tid, type=kind, label=label, resolved=resolved, expression=ref.expression.raw)
                elif resolved:
                    # one plainly-named reference is enough to call the node resolved
                    g.nodes[tid].update(resolved=True, expression=ref.expression.raw)
                a, b = (tid, jid) if direction == "read" else (jid, tid)
                if g.has_edge(a, b):
                    edge = g[a][b]
                    edge["actions"] = sorted(set(edge["actions"]) | {ref.action})
                    edge["resolved"] = edge["resolved"] and resolved
                else:
                    g.add_edge(a, b, kind=direction, actions=[ref.action], resolved=resolved)
        for child in job.child_jobs:
            target = child.key or child.name
            cid = f"job:{target}"
            if cid not in g:
                g.add_node(cid, type="job", label=target, resolved=target in keys, external=True)
            g.add_edge(jid, cid, kind="runs", actions=[], resolved=True)
    return g


def focus(g: nx.DiGraph, job: str) -> nx.DiGraph:
    """The job plus everything upstream and downstream of it.

    Jobs started through ``tRunJob`` are part of the focused job, so the tables *they* read are kept too.
    """
    jid = f"job:{job}"
    if jid not in g:
        raise KeyError(job)
    keep = {jid} | nx.ancestors(g, jid) | nx.descendants(g, jid)
    runs = nx.DiGraph([(u, v) for u, v, a in g.edges(data=True) if a["kind"] == "runs"])
    if jid in runs:
        for child in nx.descendants(runs, jid):
            keep |= set(g.predecessors(child))
    sub: nx.DiGraph = g.subgraph(keep).copy()
    return sub


# ------------------------------------------------------------------ analysis
@dataclass
class LineageAnalysis:
    migration_order: list[str] = field(default_factory=list)
    cycles: list[list[str]] = field(default_factory=list)
    tables_never_read: list[str] = field(default_factory=list)
    tables_never_written: list[str] = field(default_factory=list)
    most_depended_on: list[dict[str, Any]] = field(default_factory=list)
    jobs_without_inputs: list[str] = field(default_factory=list)
    unresolved_tables: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "migration_order": self.migration_order,
            "cycles": self.cycles,
            "tables_never_read": self.tables_never_read,
            "tables_never_written": self.tables_never_written,
            "most_depended_on": self.most_depended_on,
            "jobs_without_inputs": self.jobs_without_inputs,
            "unresolved_tables": self.unresolved_tables,
        }


def _dependency_graph(g: nx.DiGraph) -> nx.DiGraph:
    """Edge ``a -> b`` means *a must be migrated before b*."""
    d: nx.DiGraph = nx.DiGraph()
    d.add_nodes_from(n for n, a in g.nodes(data=True) if a["type"] == "job")
    for n, a in g.nodes(data=True):
        if a["type"] == "job":
            continue
        for w in g.predecessors(n):
            for r in g.successors(n):
                if w != r:
                    d.add_edge(w, r)
    for u, v, a in g.edges(data=True):
        if a["kind"] == "runs":
            d.add_edge(v, u)  # a child must exist before its parent
    return d


def analyze(g: nx.DiGraph) -> LineageAnalysis:
    out = LineageAnalysis()
    label = lambda n: str(g.nodes[n]["label"])  # noqa: E731
    d = _dependency_graph(g)

    cond = nx.condensation(d)
    order: list[str] = []
    def group_key(c: int) -> str:
        return min(label(n) for n in cond.nodes[c]["members"])

    for comp in nx.lexicographical_topological_sort(cond, key=group_key):
        members = sorted(cond.nodes[comp]["members"], key=label)
        order += [label(m) for m in members]
        if len(members) > 1:
            out.cycles.append([label(m) for m in members])
    out.migration_order = order
    out.cycles.sort()

    tables = [n for n, a in g.nodes(data=True) if a["type"] in ("table", "file")]
    out.tables_never_read = sorted(label(t) for t in tables if g.out_degree(t) == 0)
    out.tables_never_written = sorted(label(t) for t in tables if g.in_degree(t) == 0)
    out.most_depended_on = sorted(
        ({"table": label(t), "readers": g.out_degree(t)} for t in tables if g.out_degree(t) > 0),
        key=lambda x: (-x["readers"], x["table"]),
    )
    out.jobs_without_inputs = sorted(
        label(n) for n, a in g.nodes(data=True)
        if a["type"] == "job" and not any(g.nodes[p]["type"] != "job" for p in g.predecessors(n))
    )
    out.unresolved_tables = sorted(label(t) for t in tables if not g.nodes[t]["resolved"])
    return out


# ------------------------------------------------------------------ renderers
def _short_ids(g: nx.DiGraph) -> dict[str, str]:
    return {n: f"n{i}" for i, n in enumerate(sorted(g.nodes), start=1)}


def _esc(text: str) -> str:
    return text.replace('"', "#quot;")


def to_mermaid(g: nx.DiGraph) -> str:
    ids = _short_ids(g)
    lines = [
        "graph LR",
        "  classDef job fill:#dbeafe,stroke:#1d4ed8,color:#0f172a;",
        "  classDef table fill:#dcfce7,stroke:#15803d,color:#0f172a;",
        "  classDef file fill:#fef9c3,stroke:#a16207,color:#0f172a;",
        "  classDef unresolved stroke-dasharray: 5 3,stroke:#b91c1c;",
    ]
    for n in sorted(g.nodes):
        a = g.nodes[n]
        text = _esc(a["label"] if a["resolved"] or a["type"] == "job" else f"{a['label']} (unresolved)")
        shape = {"job": '(["{}"])', "table": '[("{}")]', "file": '>"{}"]'}[a["type"]].format(text)
        lines.append(f"  {ids[n]}{shape}")
    for u, v in sorted(g.edges, key=lambda e: (e[0], e[1])):
        a = g[u][v]
        if a["kind"] == "runs":
            lines.append(f"  {ids[u]} ==>|tRunJob| {ids[v]}")
        elif a["kind"] == "write" and a["actions"] and a["actions"] != ["insert"]:
            arrow = "-->" if a["resolved"] else "-.->"
            lines.append(f"  {ids[u]} {arrow}|{'/'.join(a['actions'])}| {ids[v]}")
        else:
            arrow = "-->" if a["resolved"] else "-.->"
            lines.append(f"  {ids[u]} {arrow} {ids[v]}")
    for cls in ("job", "table", "file"):
        members = [ids[n] for n in sorted(g.nodes) if g.nodes[n]["type"] == cls]
        if members:
            lines.append(f"  class {','.join(members)} {cls};")
    dashed = [ids[n] for n in sorted(g.nodes) if not g.nodes[n]["resolved"] and g.nodes[n]["type"] != "job"]
    if dashed:
        lines.append(f"  class {','.join(dashed)} unresolved;")
    return "\n".join(lines) + "\n"


def to_dot(g: nx.DiGraph) -> str:
    ids = _short_ids(g)
    lines = ["digraph lineage {", "  rankdir=LR;", '  node [fontname="Helvetica"];']
    for n in sorted(g.nodes):
        a = g.nodes[n]
        shape = {"job": "box, style=\"rounded,filled\", fillcolor=\"#dbeafe\"",
                 "table": "cylinder, style=filled, fillcolor=\"#dcfce7\"",
                 "file": "note, style=filled, fillcolor=\"#fef9c3\""}[a["type"]]
        style = ", color=\"#b91c1c\", penwidth=2" if not a["resolved"] and a["type"] != "job" else ""
        label = a["label"].replace('"', '\\"')
        lines.append(f'  {ids[n]} [label="{label}", shape={shape}{style}];')
    for u, v in sorted(g.edges, key=lambda e: (e[0], e[1])):
        a = g[u][v]
        dashed = "" if a["resolved"] else " [style=dashed]"
        extra = ' [label="tRunJob", style=bold]' if a["kind"] == "runs" else dashed
        lines.append(f"  {ids[u]} -> {ids[v]}{extra};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def to_json(g: nx.DiGraph, analysis: LineageAnalysis | None = None) -> dict[str, Any]:
    nodes = [{"id": n, **{k: v for k, v in sorted(g.nodes[n].items())}} for n in sorted(g.nodes)]
    edges = [{"source": u, "target": v, **{k: val for k, val in sorted(g[u][v].items())}}
             for u, v in sorted(g.edges, key=lambda e: (e[0], e[1]))]
    out: dict[str, Any] = {"nodes": nodes, "edges": edges}
    if analysis is not None:
        out["analysis"] = analysis.to_dict()
    return out


_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_name(s: str) -> str:
    return _SAFE.sub("_", s)
