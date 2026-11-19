from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from tests.helpers import node, write_job
from tmt import __version__
from tmt.cli import app

SAMPLES = Path(__file__).resolve().parents[2] / "samples" / "talend"
runner = CliRunner()


def test_scan_writes_models(tmp_path: Path) -> None:
    r = runner.invoke(app, ["scan", str(SAMPLES), "--out", str(tmp_path)])
    assert r.exit_code == 0, r.output
    files = sorted(p.name for p in (tmp_path / "model").glob("*.json"))
    assert "_all.json" in files and "job_join_lookup.json" in files and len(files) == 18
    assert "_errors.json" not in files
    data = json.loads((tmp_path / "model" / "job_join_lookup.json").read_text(encoding="utf-8"))
    assert data["name"] == "job_join_lookup" and data["schema_version"] == "0.4"
    assert "17 jobs parsed, 0 failed" in r.output


def test_scan_accepts_a_single_file(tmp_path: Path) -> None:
    one = SAMPLES / "process" / "job_aggregate_0.1.item"
    r = runner.invoke(app, ["scan", str(one), "--out", str(tmp_path)])
    assert r.exit_code == 0 and (tmp_path / "model" / "job_aggregate.json").exists()


def test_scan_reports_failures_and_exits_nonzero_after_finishing(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    write_job(src, "good", node("a_1", "tLogRow"))
    (src / "bad_0.1.item").write_text("<talendfile:ProcessType", encoding="utf-8")
    out = tmp_path / "out"
    r = runner.invoke(app, ["scan", str(src), "--out", str(out)])
    assert r.exit_code == 1
    assert (out / "model" / "good.json").exists()  # the healthy job was still written
    errors = json.loads((out / "model" / "_errors.json").read_text(encoding="utf-8"))
    assert errors[0]["path"] == "bad_0.1.item"


def test_stale_errors_file_is_removed(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    write_job(src, "good", node("a_1", "tLogRow"))
    out = tmp_path / "out"
    (out / "model").mkdir(parents=True)
    (out / "model" / "_errors.json").write_text("[]", encoding="utf-8")
    assert runner.invoke(app, ["scan", str(src), "--out", str(out)]).exit_code == 0
    assert not (out / "model" / "_errors.json").exists()


def test_fail_on_unsupported(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    write_job(src, "weird", node("x_1", "tLogicGate"))
    ok = runner.invoke(app, ["scan", str(src), "--out", str(tmp_path / "o1")])
    strict = runner.invoke(app, ["scan", str(src), "--out", str(tmp_path / "o2"), "--fail-on-unsupported"])
    assert ok.exit_code == 0 and strict.exit_code == 2


def test_scan_context_option(tmp_path: Path) -> None:
    r = runner.invoke(app, ["scan", str(SAMPLES), "--out", str(tmp_path), "--context", "Prod"])
    assert r.exit_code == 0
    data = json.loads((tmp_path / "model" / "job_context_driven.json").read_text(encoding="utf-8"))
    assert data["writes"][0]["db_schema"] == "STG"


def test_graph_mermaid_to_stdout() -> None:
    r = runner.invoke(app, ["graph", str(SAMPLES)])
    assert r.exit_code == 0 and r.stdout.startswith("graph LR")
    assert "==>|tRunJob|" in r.stdout and "SRC.ORDERS" in r.stdout


def test_graph_formats_and_output_dir(tmp_path: Path) -> None:
    for fmt, ext in (("mermaid", "mmd"), ("dot", "dot"), ("json", "json")):
        r = runner.invoke(app, ["graph", str(SAMPLES), "--format", fmt, "--out", str(tmp_path)])
        assert r.exit_code == 0, r.output
        assert (tmp_path / "graph" / f"lineage.{ext}").read_text(encoding="utf-8")
    data = json.loads((tmp_path / "graph" / "lineage.json").read_text(encoding="utf-8"))
    assert data["analysis"]["migration_order"].index("job_orchestrator") > data["analysis"]["migration_order"].index(
        "job_simple_copy")
    assert data["analysis"]["cycles"] == []


def test_graph_focus() -> None:
    r = runner.invoke(app, ["graph", str(SAMPLES), "--focus", "job_orchestrator"])
    assert r.exit_code == 0
    assert "job_orchestrator" in r.stdout and "job_aggregate" not in r.stdout


def test_graph_unknown_focus_is_a_clean_error() -> None:
    r = runner.invoke(app, ["graph", str(SAMPLES), "--focus", "nope"])
    assert r.exit_code == 2 and "not found" in r.output


def test_missing_path_is_rejected(tmp_path: Path) -> None:
    assert runner.invoke(app, ["scan", str(tmp_path / "does_not_exist")]).exit_code != 0


def test_version_flag() -> None:
    r = runner.invoke(app, ["--version"])
    assert r.exit_code == 0 and f"tmt {__version__}" in r.output


def test_scan_models_now_carry_complexity(tmp_path: Path) -> None:
    assert runner.invoke(app, ["scan", str(SAMPLES), "--out", str(tmp_path)]).exit_code == 0
    data = json.loads((tmp_path / "model" / "job_runif.json").read_text(encoding="utf-8"))
    assert data["complexity"]["level"] == "hard" and data["complexity"]["score"] == 33
    assert {r["factor"] for r in data["complexity"]["reasons"]} == {"component", "custom_code", "forced_hard"}










def test_config_changes_levels(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("scoring:\n  thresholds:\n    easy_max: 0\n    medium_max: 1\n", encoding="utf-8")
    r = runner.invoke(app, ["scan", str(SAMPLES), "--out", str(tmp_path / "o"), "--config", str(cfg)])
    assert r.exit_code == 0
    data = json.loads((tmp_path / "o" / "model" / "job_aggregate.json").read_text(encoding="utf-8"))
    assert data["complexity"]["score"] == 3 and data["complexity"]["level"] == "hard"


def test_invalid_config_exits_2_with_a_message(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("scoring:\n  weights:\n    compnent: 1\n", encoding="utf-8")
    for cmd in ("scan", "graph"):
        r = runner.invoke(app, [cmd, str(SAMPLES), "--config", str(cfg)])
        assert r.exit_code == 2 and "invalid config" in r.output, cmd


def test_scan_summary_shows_level_and_score(tmp_path: Path) -> None:
    r = runner.invoke(app, ["scan", str(SAMPLES), "--out", str(tmp_path)])
    assert "level" in r.output and "score" in r.output and "hard" in r.output


def test_graph_master_prints_the_workflow_numbers_on_stderr_and_a_pure_graph_on_stdout() -> None:
    demo = SAMPLES.parent / "talend_demo"
    r = runner.invoke(app, ["graph", str(demo), "--master", "job_master_daily"])
    assert r.exit_code == 0 and r.stdout.startswith("graph LR") and "job_stage_orders" in r.stdout
    assert "job_export_sales" not in r.stdout  # not started by this master
    assert "4 jobs, 12 sources, 10 targets" in r.stderr
    assert "2 distinct): 10x ODS_DB, 2x AKY_DB" in r.stderr


def test_graph_master_and_focus_are_exclusive() -> None:
    r = runner.invoke(app, ["graph", str(SAMPLES), "--master", "job_orchestrator", "--focus", "job_aggregate"])
    assert r.exit_code == 2 and "either --master or --focus" in r.output
