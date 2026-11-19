"""Plugin discovery: a broken plugin must never stop tmt."""

from __future__ import annotations

import json
import logging
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from tests.helpers import node, write_job
from tmt.parsers import registry
from tmt.scan import scan


def ep(name: str, value: str, group: str = registry.ENTRY_POINT_GROUP) -> EntryPoint:
    return EntryPoint(name=name, value=value, group=group)


@pytest.fixture()
def plugins(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    def install(*eps: EntryPoint) -> None:
        monkeypatch.setattr(registry, "entry_points", lambda group: [e for e in eps if e.group == group])

    return install


def test_builtin_talend_parser_is_always_first(plugins) -> None:  # type: ignore[no-untyped-def]
    plugins()
    assert [p.name for p in registry.load_parsers()] == ["talend"]


def test_valid_plugin_is_loaded_after_the_builtin(plugins) -> None:  # type: ignore[no-untyped-def]
    plugins(ep("dummy", "tests.dummy_parser:DummyParser"))
    assert [p.name for p in registry.load_parsers()] == ["talend", "dummy"]


def test_plugins_are_ordered_by_entry_point_name(plugins) -> None:  # type: ignore[no-untyped-def]
    plugins(ep("zzz", "tests.dummy_parser:DummyParser"), ep("aaa", "tests.plugin_samples:SecondDummy"))
    assert [p.name for p in registry.load_parsers()] == ["talend", "second", "dummy"]


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("no_such_module_xyz:Thing", "No module named"),  # import error
        ("tests.dummy_parser:SUFFIX", "not a subclass"),  # not a class
        ("tests.plugin_samples:NotAParser", "not a subclass"),  # a class, but wrong base
        ("tests.plugin_samples:NamelessParser", "non-empty"),  # no usable name
        ("tests.plugin_samples:ImpostorParser", "already registered"),  # clashes with the built-in
        ("tests.plugin_samples:ExplodingParser", "boom"),  # constructor raises
    ],
)
def test_broken_plugin_is_skipped_with_a_warning(plugins, caplog, value: str, why: str) -> None:  # type: ignore[no-untyped-def]
    plugins(ep("bad", value), ep("dummy", "tests.dummy_parser:DummyParser"))
    with caplog.at_level(logging.WARNING):
        names = [p.name for p in registry.load_parsers()]
    assert names == ["talend", "dummy"]  # the healthy plugin still loads
    assert "ignoring parser plugin 'bad'" in caplog.text and why in caplog.text


def test_dialect_is_passed_only_to_parsers_that_accept_it(plugins) -> None:  # type: ignore[no-untyped-def]
    plugins(ep("dialectual", "tests.plugin_samples:DialectParser"))
    parsers = {p.name: p for p in registry.load_parsers(dialect="postgres")}
    assert parsers["dialectual"].dialect == "postgres"  # type: ignore[attr-defined]
    assert parsers["talend"].dialect == "postgres"  # type: ignore[attr-defined]


def test_select_by_name_and_unknown_name() -> None:
    parsers = registry.load_parsers()
    assert registry.select(parsers, None) is parsers
    assert [p.name for p in registry.select(parsers, "talend")] == ["talend"]
    with pytest.raises(KeyError) as exc:
        registry.select(parsers, "nope")
    assert "talend" in exc.value.args[0]


def test_scan_picks_the_right_parser_per_file(plugins, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    plugins(ep("dummy", "tests.dummy_parser:DummyParser"))
    write_job(tmp_path, "talend_job", node("a_1", "tLogRow"))
    (tmp_path / "x.dummyjob.json").write_text(json.dumps({"name": "dummy_job", "steps": [
        {"id": "r", "kind": "read", "table": "S.T"}]}), encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
    result = scan(tmp_path)
    assert {(j.name, j.source_tool) for j in result.jobs} == {("talend_job", "talend"), ("dummy_job", "dummy")}
    assert result.failures == []


def test_jobs_from_different_tools_share_one_lineage_graph(plugins, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    from tmt.lineage import builder

    plugins(ep("dummy", "tests.dummy_parser:DummyParser"))
    (tmp_path / "w.dummyjob.json").write_text(json.dumps({"name": "writer", "steps": [
        {"id": "w", "kind": "write", "table": "STG.SHARED"}]}), encoding="utf-8")
    (tmp_path / "r.dummyjob.json").write_text(json.dumps({"name": "reader", "steps": [
        {"id": "r", "kind": "read", "table": "stg.shared"}]}), encoding="utf-8")
    g = builder.build_graph(scan(tmp_path).jobs)
    assert builder.analyze(g).migration_order == ["writer", "reader"]


# ------------------------------------------------------------------ explainers


