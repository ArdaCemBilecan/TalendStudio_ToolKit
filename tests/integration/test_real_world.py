"""Regression tests for what a real workspace does and the samples do not.

Found by scanning a real Talend workspace: the same job name in several projects (a PROD project cloned from
DEV repeats every name *and* every Studio id), and migration-tool placeholders instead of real queries.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.helpers import conn, node, param, write_job
from tmt.cli import app
from tmt.lineage import builder
from tmt.models import IssueCode, Resolution
from tmt.parsers.talend.components import _implausible_table
from tmt.scan import scan, write_models

SAMPLE_DIR = Path(__file__).resolve().parents[2] / "samples" / "talend" / "process"
runner = CliRunner()


def make_project(root: Path, label: str, jobs: tuple[str, ...], folder: str = "") -> Path:
    """A Studio-like project folder: talend.project + process/<folder>/<job>_0.1.{item,properties}."""
    proj = root / label
    target = proj / "process" / folder
    target.mkdir(parents=True)
    (proj / "talend.project").write_text(
        f'<xmi:XMI xmlns:xmi="x"><TalendProperties:Project technicalLabel="{label}" label="{label}"/></xmi:XMI>',
        encoding="utf-8")
    for job in jobs:
        for ext in ("item", "properties"):
            shutil.copy(SAMPLE_DIR / f"{job}_0.1.{ext}", target)
    return proj


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """DEV and PROD hold the same jobs (same names, same Studio ids); an orchestrator calls DEV children."""
    jobs = ("job_filter_map", "job_simple_copy", "job_join_lookup", "job_orchestrator", "job_aggregate")
    make_project(tmp_path, "DEV", jobs)
    make_project(tmp_path, "PROD", jobs[:2])  # PROD has fewer; its job_orchestrator is absent
    return tmp_path


# ------------------------------------------------------------------ identical names in several projects
def test_same_name_in_two_projects_gets_distinct_keys(workspace: Path) -> None:
    jobs = {j.key: j for j in scan(workspace).jobs}
    assert {"job_filter_map@DEV", "job_filter_map@PROD", "job_simple_copy@DEV", "job_simple_copy@PROD"} <= set(jobs)
    assert "job_orchestrator" in jobs and "job_aggregate" in jobs  # a unique name keeps its plain name
    assert jobs["job_filter_map@PROD"].project == "PROD" and jobs["job_filter_map@PROD"].name == "job_filter_map"
    assert len(jobs) == 7


def test_no_job_is_lost_in_the_model_output(workspace: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    out = tmp_path_factory.mktemp("out")
    result = scan(workspace)
    write_models(result, out)
    files = sorted(p.name for p in (out / "model").glob("*.json"))
    assert len(files) == 7 + 1  # every job plus _all.json: nothing overwritten
    assert "job_filter_map_DEV.json" in files and "job_filter_map_PROD.json" in files
    assert len(json.loads((out / "model" / "_all.json").read_text(encoding="utf-8"))) == 7


def test_same_name_in_two_folders_of_one_project(tmp_path: Path) -> None:
    make_project(tmp_path, "DEV", ("job_aggregate",))
    sub = tmp_path / "DEV" / "process" / "sub"
    sub.mkdir()
    for ext in ("item", "properties"):
        shutil.copy(SAMPLE_DIR / f"job_aggregate_0.1.{ext}", sub)
    props = sub / "job_aggregate_0.1.properties"  # the copy declares its folder, as Studio does
    props.write_text(props.read_text(encoding="utf-8").replace('path=""', 'path="sub"'), encoding="utf-8")
    keys = sorted(j.key for j in scan(tmp_path).jobs)
    assert keys == ["job_aggregate@DEV", "job_aggregate@DEV/sub"]


def test_without_a_project_file_the_path_disambiguates(tmp_path: Path) -> None:
    for d in ("a", "b"):
        (tmp_path / d).mkdir()
        write_job(tmp_path / d, "dup", node("x_1", "tLogRow"))
    keys = sorted(j.key for j in scan(tmp_path).jobs)
    assert keys == ["dup@a/dup_0.1", "dup@b/dup_0.1"]


def test_lineage_keeps_same_named_jobs_apart(workspace: Path) -> None:
    jobs = scan(workspace).jobs
    g = builder.build_graph(jobs)
    labels = {d["label"] for _, d in g.nodes(data=True) if d["type"] == "job"}
    assert {"job_filter_map@DEV", "job_filter_map@PROD"} <= labels
    for key in ("job_filter_map@DEV", "job_filter_map@PROD"):
        assert g.has_edge(f"job:{key}", "table:STG.CUSTOMERS_ACTIVE")  # both write it, neither replaced the other
    assert builder.analyze(g).cycles == []






# ------------------------------------------------------------------ tRunJob across copied projects
def test_children_are_resolved_by_project_and_id(workspace: Path) -> None:
    jobs = {j.key: j for j in scan(workspace).jobs}
    kids = {c.name: c for c in jobs["job_orchestrator"].child_jobs}
    assert all(c.resolved for c in kids.values())
    # the orchestrator says "DEV:<id>": it must land on the DEV copy even though PROD repeats the same id
    assert kids["job_filter_map"].key == "job_filter_map@DEV" and kids["job_filter_map"].project == "DEV"
    assert kids["job_simple_copy"].key == "job_simple_copy@DEV"


def test_ambiguous_child_name_is_reported_not_guessed(tmp_path: Path) -> None:
    # The orchestrator says "DEV:<id>" but DEV has no such job; PROD and QA both hold a job with that name AND id.
    make_project(tmp_path, "DEV", ("job_orchestrator",))
    make_project(tmp_path, "PROD", ("job_filter_map",))
    make_project(tmp_path, "QA", ("job_filter_map",))
    orch = next(j for j in scan(tmp_path).jobs if j.name == "job_orchestrator")
    child = next(c for c in orch.child_jobs if c.name == "job_filter_map")
    assert child.resolved is False and child.key is None  # two candidates: refuse to pick one
    assert any("matches 2 scanned jobs" in i.message for i in orch.issues)


def test_unknown_child_is_flagged(tmp_path: Path) -> None:
    make_project(tmp_path, "DEV", ("job_orchestrator",))  # children missing from the scan
    job = scan(tmp_path).jobs[0]
    assert not any(c.resolved for c in job.child_jobs)
    assert sum(i.code is IssueCode.UNRESOLVED_REPOSITORY_REF for i in job.issues) == 3


# ------------------------------------------------------------------ CLI: which job do you mean?
def test_cli_ambiguous_name_lists_the_keys(workspace: Path) -> None:
    for cmd in (["graph", "--master"], ["graph", "--focus"]):
        r = runner.invoke(app, [cmd[0], str(workspace), cmd[1], "job_filter_map"])
        assert r.exit_code == 2 and "ambiguous" in r.output
        assert "job_filter_map@DEV" in r.output and "job_filter_map@PROD" in r.output


def test_cli_accepts_the_key_and_a_unique_plain_name(workspace: Path) -> None:
    by_key = runner.invoke(app, ["graph", str(workspace), "--master", "job_filter_map@PROD"])
    assert by_key.exit_code == 0 and "job_filter_map@PROD" in by_key.stdout
    by_name = runner.invoke(app, ["graph", str(workspace), "--master", "job_aggregate"])
    assert by_name.exit_code == 0 and "job_aggregate" in by_name.stdout
    focus = runner.invoke(app, ["graph", str(workspace), "--focus", "job_filter_map@DEV"])
    assert focus.exit_code == 0 and "job_filter_map@DEV" in focus.stdout


def test_cli_scan_table_never_breaks_a_name(workspace: Path, tmp_path: Path) -> None:
    r = runner.invoke(app, ["scan", str(workspace), "--out", str(tmp_path / "o")])
    assert r.exit_code == 0 and "job_filter_map@DEV" in r.output and "job_filter_map@PROD" in r.output


# ------------------------------------------------------------------ placeholders instead of real values
PLACEHOLDER_QUERY = '"$AGGR__REF.READER.Query"'


def jdbc_job(tmp: Path, query: str = PLACEHOLDER_QUERY, table: str = '"Output for table"'):  # type: ignore[no-untyped-def]
    body = (node("tJDBCInput_1", "JDBCInput", param("configuration.dataSet.sqlQuery", query, "MEMO_SQL"))
            + node("tJDBCOutput_1", "JDBCOutput", param("configuration.dataSet.tableName", table))
            + conn("tJDBCInput_1", "tJDBCOutput_1", "MAIN"))
    write_job(tmp, "ph", body)
    return scan(tmp).jobs[0]


def test_placeholder_query_is_flagged_never_silently_dropped(tmp_path: Path) -> None:
    job = jdbc_job(tmp_path)
    assert job.reads == []  # we really do not know what it reads...
    flags = [i for i in job.issues if i.code is IssueCode.PLACEHOLDER_VALUE]
    assert any("placeholder left by a migration tool" in i.message and i.component_id == "tJDBCInput_1" for i in flags)
    source = next(c for c in job.components if c.id == "tJDBCInput_1")
    assert source.sql[0].parsed is False and source.sql[0].kind == "other"


def test_default_label_is_not_a_table_name(tmp_path: Path) -> None:
    job = jdbc_job(tmp_path)
    (w,) = job.writes
    assert w.name is None and w.resolution is Resolution.UNRESOLVED and w.expression.value == "Output for table"
    assert any(i.code is IssueCode.PLACEHOLDER_VALUE and "Output for table" in i.message for i in job.issues)
    tables = [a for _, a in builder.build_graph([job]).nodes(data=True) if a["type"] == "table"]
    assert len(tables) == 1 and tables[0]["resolved"] is False  # shown, but as an unresolved target, not a real table


def test_placeholders_add_to_the_complexity_score(tmp_path: Path) -> None:
    job = jdbc_job(tmp_path)
    reasons = {r.factor: r for r in job.complexity.reasons}  # type: ignore[union-attr]
    assert reasons["unresolved_placeholder"].points == 4  # two components x the unresolved weight (2)
    assert "tJDBCInput_1" in (reasons["unresolved_placeholder"].detail or "")




def test_real_identifiers_and_quoted_names_are_not_mistaken_for_placeholders(tmp_path: Path) -> None:
    ok = jdbc_job(tmp_path, '"SELECT a FROM S.T"', '"STG.CUSTOMERS"')
    assert not [i for i in ok.issues if i.code is IssueCode.PLACEHOLDER_VALUE]
    assert [(r.db_schema, r.name) for r in ok.reads] == [("S", "T")] and ok.writes[0].name == "CUSTOMERS"


@pytest.mark.parametrize("value", ["Output for table", "Select a table", "tbl; drop", "a b.c", "", "  ", "**"])
def test_implausible_table_names(value: str) -> None:
    assert _implausible_table(value) or not value.strip()


@pytest.mark.parametrize(
    "value", ["T", "S.T", "DB.S.T", "my_table_1", "SCHEMA$.T#", '"My Table"', "`a b`", "[dbo].[T]"]
)
def test_plausible_table_names(value: str) -> None:
    assert not _implausible_table(value)
