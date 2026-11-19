"""A deliberately tiny second parser, used to prove the plugin interface.

Format (``*.dummyjob.json``)::

    {"name": "load_x", "version": "1.0", "steps": [
        {"id": "r1", "kind": "read",  "table": "SRC.X"},
        {"id": "w1", "kind": "write", "table": "STG.X", "after": ["r1"]}]}

It shows the minimum a parser has to do: recognise its files, build the tool-independent :class:`Job`, and raise
:class:`ParseError` for anything unusable. See docs/adding_a_parser.md.
"""

from __future__ import annotations

import json
from pathlib import Path

from tmt.errors import ParseError
from tmt.models import (
    Category,
    Component,
    Connection,
    ConnKind,
    Expression,
    Job,
    Resolution,
    SourceRef,
    Subjob,
    TableRef,
)
from tmt.parsers.base import BaseParser

SUFFIX = ".dummyjob.json"


class DummyParser(BaseParser):
    name = "dummy"

    def can_parse(self, path: Path) -> bool:
        return path.is_file() and path.name.endswith(SUFFIX)

    def parse(self, path: Path, *, context: str | None = None, base: Path | None = None) -> Job:
        shown = path.relative_to(base).as_posix() if base and path.is_relative_to(base) else path.name
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            steps = raw["steps"]
            name = str(raw["name"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ParseError(shown, f"not a dummy job: {exc}") from exc

        components: list[Component] = []
        reads: list[TableRef] = []
        writes: list[TableRef] = []
        conns: list[Connection] = []
        for step in steps:
            kind = step.get("kind")
            if kind not in ("read", "write", "transform"):
                raise ParseError(shown, f"step {step.get('id')!r} has unknown kind {kind!r}")
            category = {"read": Category.INPUT, "write": Category.OUTPUT, "transform": Category.TRANSFORM}[kind]
            components.append(Component(id=step["id"], type=f"dummy_{kind}", category=category,
                                        implementation="classic", subjob_id=None,
                                        source_ref=SourceRef(file=shown)))
            if kind in ("read", "write"):
                table = str(step["table"])
                schema, _, tname = table.rpartition(".")
                expr = Expression(raw=f'"{table}"', resolution=Resolution.RESOLVED, value=table)
                ref = TableRef(db_schema=schema or None, name=tname, expression=expr,
                               resolution=Resolution.RESOLVED, access=kind, action="select" if kind == "read"
                               else "insert", component_id=step["id"], via="table_param")
                (reads if kind == "read" else writes).append(ref)
            for dep in step.get("after", []):
                conns.append(Connection(name=f"{dep}->{step['id']}", source=dep, target=step["id"],
                                        kind=ConnKind.FLOW))
        ids = [c.id for c in components]
        for c in components:
            c.subjob_id = ids[0]
        return Job(
            source_tool=self.name, name=name, version=str(raw.get("version", "1.0")), path=shown,
            components=sorted(components, key=lambda c: c.id),
            connections=sorted(conns, key=lambda c: c.name),
            subjobs=[Subjob(id=ids[0], component_ids=sorted(ids))] if ids else [],
            reads=reads, writes=writes,
        )
