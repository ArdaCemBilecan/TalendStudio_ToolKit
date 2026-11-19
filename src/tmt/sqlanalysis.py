"""Which tables does a SQL statement read and write? Static analysis with sqlglot, nothing executed."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

Kind = Literal["select", "insert", "update", "delete", "merge", "ddl", "other"]
WriteAction = Literal["insert", "update", "upsert", "delete", "truncate", "unknown"]


@dataclass(frozen=True)
class TableName:
    database: str | None
    schema: str | None
    name: str

    def qualified(self) -> str:
        return ".".join(p for p in (self.database, self.schema, self.name) if p)


@dataclass
class StatementInfo:
    kind: Kind = "other"
    parsed: bool = False
    reads: list[TableName] = field(default_factory=list)
    writes: list[tuple[TableName, WriteAction]] = field(default_factory=list)


def split_qualified(name: str) -> TableName:
    """``SCHEMA.TABLE`` / ``DB.SCHEMA.TABLE`` / ``TABLE`` -> TableName (quotes stripped)."""
    parts = [p.strip().strip('"`[]') for p in name.strip().split(".")]
    parts = [p for p in parts if p]
    if not parts:
        return TableName(None, None, name.strip())
    if len(parts) == 1:
        return TableName(None, None, parts[0])
    if len(parts) == 2:
        return TableName(None, parts[0], parts[1])
    return TableName(parts[-3], parts[-2], parts[-1])


def _tn(t: exp.Table) -> TableName:
    return TableName(t.catalog or None, t.db or None, t.name)


def _tables(node: exp.Expression | None, exclude: set[str]) -> list[TableName]:
    if node is None:
        return []
    ctes = {c.alias_or_name for c in node.find_all(exp.CTE)}
    out: list[TableName] = []
    for t in node.find_all(exp.Table):
        if not t.name or (not t.db and t.name in ctes) or t.name in exclude:
            continue
        out.append(_tn(t))
    return out


def _target(node: exp.Expression | None) -> exp.Table | None:
    if isinstance(node, exp.Schema):  # INSERT INTO t (a, b) ...
        node = node.this
    return node if isinstance(node, exp.Table) else None


def analyze_sql(sql: str, dialect: str | None = "oracle") -> StatementInfo:
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except (SqlglotError, ValueError):
        return StatementInfo()
    if tree is None or isinstance(tree, exp.Command):
        return StatementInfo()

    info = StatementInfo(parsed=True)
    if isinstance(tree, (exp.Select, exp.Union, exp.Subquery)):
        info.kind, info.reads = "select", _tables(tree, set())
    elif isinstance(tree, exp.Insert):
        info.kind = "insert"
        target = _target(tree.this)
        if target is not None:
            info.writes.append((_tn(target), "insert"))
        info.reads = _tables(tree.expression, set())
    elif isinstance(tree, exp.Update):
        info.kind = "update"
        target = _target(tree.this)
        if target is not None:
            info.writes.append((_tn(target), "update"))
        info.reads = _tables(tree, {target.name} if target else set())
    elif isinstance(tree, exp.Delete):
        info.kind = "delete"
        target = _target(tree.this)
        if target is not None:
            info.writes.append((_tn(target), "delete"))
        info.reads = _tables(tree, {target.name} if target else set())
    elif isinstance(tree, exp.Merge):
        info.kind = "merge"
        target = _target(tree.this)
        if target is not None:
            info.writes.append((_tn(target), "upsert"))
        using = tree.args.get("using")
        info.reads = _tables(using, set()) if using is not None else []
    elif isinstance(tree, exp.TruncateTable):
        info.kind = "ddl"
        info.writes = [(_tn(t), "truncate") for t in tree.expressions if isinstance(t, exp.Table)]
    elif isinstance(tree, (exp.Create, exp.Drop, exp.Alter)):
        info.kind = "ddl"
        target = _target(tree.this)
        if target is not None:
            info.writes.append((_tn(target), "unknown"))
        info.reads = _tables(tree.args.get("expression"), set())
    else:
        info.kind = "other"
    return info
