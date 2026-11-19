"""CLI behaviour that users feel: errors that say what to do, quiet mode, pipes, plugins, no stack traces."""

from __future__ import annotations

import json
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.helpers import node, write_job
from tmt.cli import app
from tmt.parsers import registry

SAMPLES = Path(__file__).resolve().parents[2] / "samples" / "talend"
runner = CliRunner()


def invoke(*args: str):  # type: ignore[no-untyped-def]
    return runner.invoke(app, list(args))


def no_traceback(r) -> None:  # type: ignore[no-untyped-def]
    assert "Traceback" not in r.output and "Error:" not in r.output.split("\n")[0], r.output


# ------------------------------------------------------------------ discoverability
def test_help_shows_examples_and_exit_codes() -> None:
    r = invoke("--help")
    assert r.exit_code == 0
    for needle in ("Examples:", "tmt ui", "Exit codes:", "graph", "parsers", "scan"):
        assert needle in r.output


def test_every_command_has_help_text() -> None:
    for cmd in ("scan", "graph", "ui", "parsers"):
        r = invoke(cmd, "--help")
        assert r.exit_code == 0 and len(r.output.strip().splitlines()) > 3, cmd


def test_no_arguments_shows_usage() -> None:
    r = invoke()
    assert "Usage" in r.output


def test_parsers_command_lists_parsers() -> None:
    r = invoke("parsers")
    assert r.exit_code == 0
    assert "talend" in r.output and "built-in" in r.output and "parsers" in r.output


# ------------------------------------------------------------------ errors say what to do
def test_empty_folder_is_a_clear_error(tmp_path: Path) -> None:
    for cmd in ("scan", "graph"):
        r = invoke(cmd, str(tmp_path))
        assert r.exit_code == 2 and "no job files found" in r.output and "hint:" in r.output, cmd
        no_traceback(r)


def test_folder_with_only_other_files(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hi", encoding="utf-8")
    r = invoke("scan", str(tmp_path))
    assert r.exit_code == 2 and "process/*.item" in r.output


def test_unknown_parser_lists_the_installed_ones() -> None:
    r = invoke("scan", str(SAMPLES), "--parser", "nope")
    assert r.exit_code == 2 and "unknown parser 'nope'" in r.output
    assert "talend" in r.output and "tmt parsers" in r.output


def test_explicit_parser_still_works(tmp_path: Path) -> None:
    r = invoke("scan", str(SAMPLES), "--parser", "talend", "--out", str(tmp_path), "-q")
    assert r.exit_code == 0 and (tmp_path / "model" / "_all.json").exists()


def test_unknown_job_for_graph_lists_known_jobs() -> None:
    for cmd, flag in (("graph", "--focus"), ("graph", "--master")):
        r = invoke(cmd, str(SAMPLES), flag, "nope")
        assert r.exit_code == 2 and "job 'nope' not found" in r.output and "job_aggregate" in r.output, cmd
        no_traceback(r)


def test_config_error_has_a_hint(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("bogus: 1\n", encoding="utf-8")
    r = invoke("scan", str(SAMPLES), "--config", str(cfg))
    assert r.exit_code == 2 and "invalid config" in r.output and "config.example.yaml" in r.output




def test_fail_on_unsupported_explains_where_to_look(tmp_path: Path) -> None:
    src = tmp_path / "s"
    src.mkdir()
    write_job(src, "weird", node("x_1", "tLogicGate"))
    r = invoke("scan", str(src), "--out", str(tmp_path / "o"), "--fail-on-unsupported")
    assert r.exit_code == 2 and "unsupported_component" in r.output


# ------------------------------------------------------------------ quiet mode and pipes
def test_quiet_prints_nothing_but_still_writes(tmp_path: Path) -> None:
    for cmd in ("scan",):
        out = tmp_path / cmd
        r = invoke(cmd, str(SAMPLES), "--out", str(out), "-q")
        assert r.exit_code == 0 and r.stdout.strip() == "", (cmd, r.stdout)
        assert out.exists()


def test_quiet_does_not_hide_errors(tmp_path: Path) -> None:
    src = tmp_path / "s"
    src.mkdir()
    (src / "bad_0.1.item").write_text("<talendfile:ProcessType", encoding="utf-8")
    r = invoke("scan", str(src), "--out", str(tmp_path / "o"), "-q")
    assert r.exit_code == 1 and "FAILED" in r.output and "bad_0.1.item" in r.output


def test_graph_stdout_is_pure_so_it_can_be_piped() -> None:
    r = runner.invoke(app, ["graph", str(SAMPLES)])
    assert r.stdout.startswith("graph LR") and "migration order" not in r.stdout
    assert "migration order" in r.stderr  # diagnostics go to stderr


def test_graph_json_to_stdout_parses() -> None:
    r = invoke("graph", str(SAMPLES), "--format", "json")
    data = json.loads(r.stdout)
    assert set(data) == {"nodes", "edges", "analysis"}


def test_paths_are_never_broken_across_lines(tmp_path: Path) -> None:
    deep = tmp_path / ("very_long_directory_name_" * 3) / "out"
    r = invoke("scan", str(SAMPLES), "--out", str(deep))
    assert f"wrote 18 files to {deep / 'model'}" in r.stdout.replace("\r", "")


def test_unwritable_output_path_is_a_clean_error(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a folder", encoding="utf-8")
    for cmd in ("scan", "graph"):
        args = [cmd, str(SAMPLES), "--out", str(blocker / "sub")]
        if cmd == "graph":
            args += ["--format", "json"]
        r = runner.invoke(app, args)
        assert r.exit_code == 2 and "cannot read or write files" in r.output and "hint:" in r.output, cmd
        no_traceback(r)
        assert r.exception is None or isinstance(r.exception, SystemExit), cmd


def test_markup_like_text_in_names_is_printed_literally(tmp_path: Path) -> None:
    src = tmp_path / "s"
    src.mkdir()
    (src / "[bold red]x_0.1.item").write_text("<talendfile:ProcessType", encoding="utf-8")
    r = invoke("scan", str(src), "--out", str(tmp_path / "o"))
    assert "[bold red]x_0.1.item" in r.output and r.exit_code == 1


def test_singular_and_plural_wording(tmp_path: Path) -> None:
    src = tmp_path / "s"
    src.mkdir()
    write_job(src, "only", node("a_1", "tLogRow"))
    r = invoke("scan", str(src), "--out", str(tmp_path / "o"))
    assert "1 job parsed" in r.output and "job(s)" not in r.output and "file(s)" not in r.output




def test_status_is_written_in_words_not_colour_alone(tmp_path: Path) -> None:
    r = invoke("scan", str(SAMPLES), "--out", str(tmp_path), "-v")
    assert "hard" in r.output and "medium" in r.output and "easy" in r.output


# ------------------------------------------------------------------ explain










# ------------------------------------------------------------------ plugins through the CLI
def test_plugin_parser_through_the_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    eps = [EntryPoint(name="dummy", value="tests.dummy_parser:DummyParser", group=registry.ENTRY_POINT_GROUP)]
    monkeypatch.setattr(registry, "entry_points", lambda group: eps)
    (tmp_path / "x.dummyjob.json").write_text(json.dumps({"name": "dj", "steps": [
        {"id": "r", "kind": "read", "table": "S.T"}, {"id": "w", "kind": "write", "table": "T.U", "after": ["r"]}]}),
        encoding="utf-8")
    r = invoke("scan", str(tmp_path), "--out", str(tmp_path / "o"))
    assert r.exit_code == 0 and "1 job parsed" in r.output
    assert json.loads((tmp_path / "o" / "model" / "dj.json").read_text(encoding="utf-8"))["source_tool"] == "dummy"
    assert "plugin" in invoke("parsers").output
    assert invoke("scan", str(tmp_path), "--out", str(tmp_path / "o2"), "--parser", "talend").exit_code == 2
    full = invoke("graph", str(tmp_path), "--format", "svg", "--out", str(tmp_path / "o3"))
    assert full.exit_code == 0 and (tmp_path / "o3" / "graph" / "lineage.svg").exists()
