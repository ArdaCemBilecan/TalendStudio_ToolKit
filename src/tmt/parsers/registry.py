"""Discover parsers: the built-in Talend parser plus any installed plugin.

A plugin is any installed package that declares an entry point in the ``tmt.parsers`` group::

    [project.entry-points."tmt.parsers"]
    datastage = "tmt_datastage:DataStageParser"

A broken plugin (import error, wrong type, duplicate name) is logged and skipped; it never stops ``tmt``.
"""

from __future__ import annotations

import inspect
import logging
from importlib.metadata import EntryPoint, entry_points

from tmt.parsers.base import BaseParser
from tmt.parsers.talend.parser import TalendParser

log = logging.getLogger(__name__)
ENTRY_POINT_GROUP = "tmt.parsers"


def _instantiate(cls: type[BaseParser], dialect: str | None) -> BaseParser:
    """Plugins may accept ``dialect`` (the SQL dialect from config.yaml); it is optional for them."""
    if "dialect" in inspect.signature(cls).parameters:
        return cls(dialect=dialect)  # type: ignore[call-arg]
    return cls()


def _plugin_entry_points() -> list[EntryPoint]:
    return sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda e: e.name)


def load_parsers(dialect: str | None = "oracle") -> list[BaseParser]:
    """Built-in parsers first, then plugins sorted by entry-point name (deterministic)."""
    parsers: list[BaseParser] = [TalendParser(dialect=dialect)]
    for ep in _plugin_entry_points():
        try:
            cls = ep.load()
            if not (inspect.isclass(cls) and issubclass(cls, BaseParser)):
                raise TypeError(f"{ep.value} is not a subclass of tmt.parsers.base.BaseParser")
            parser = _instantiate(cls, dialect)
            name = getattr(parser, "name", "")
            if not isinstance(name, str) or not name:
                raise TypeError(f"{ep.value} does not define a non-empty `name`")
            if any(p.name == name for p in parsers):
                raise ValueError(f"a parser named {name!r} is already registered")
        except Exception as exc:  # a plugin must never take down the tool
            log.warning("ignoring parser plugin %r: %s", ep.name, exc)
            continue
        parsers.append(parser)
    return parsers


def select(parsers: list[BaseParser], name: str | None) -> list[BaseParser]:
    """Restrict to one parser by name. Raises KeyError (with the known names) if it does not exist."""
    if name is None:
        return parsers
    chosen = [p for p in parsers if p.name == name]
    if not chosen:
        raise KeyError(", ".join(p.name for p in parsers))
    return chosen
