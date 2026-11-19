"""Every sample job must parse with zero crashes, and the facts we rely on must be extracted."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tmt.models import Category, ConnKind, IssueCode, Job, Resolution
from tmt.scan import ScanResult, scan


@pytest.fixture(scope="module")
def result() -> ScanResult:
    return scan(Path(__file__).resolve().parents[2] / "samples" / "talend")


@pytest.fixture(scope="module")
def jobs(result: ScanResult) -> dict[str, Job]:
    return {j.name: j for j in result.jobs}


def test_all_samples_parse_without_failures(result: ScanResult) -> None:
    assert result.failures == []
    assert len(result.jobs) == 17


def test_job_identity_comes_from_properties(jobs: dict[str, Job]) -> None:
    j = jobs["job_filter_map"]
    assert (j.version, j.tool_version) == ("0.1", "8.0.1")


def test_tmap_roles_join_types_and_keys(jobs: dict[str, Job]) -> None:
    tm = next(c for c in jobs["job_join_lookup"].components if c.type == "tMap").tmap
    assert tm is not None
    by_name = {i.name: i for i in tm.inputs}
    assert by_name["row1"].role == "main" and by_name["row1"].join_type is None
    assert (by_name["row2"].role, by_name["row2"].join_type) == ("lookup", "inner")
    assert (by_name["row3"].role, by_name["row3"].join_type) == ("lookup", "left")
    assert [(k.lookup_column, k.expression.raw, k.operator) for k in by_name["row2"].join_keys] == [
        ("customer_id", "row1.customer_id", "=")]
    assert [(k.lookup_column, k.expression.raw) for k in by_name["row3"].join_keys] == [
        ("country_code", "row2.country_code")]
    assert by_name["row2"].matching_mode == "UNIQUE_MATCH" and by_name["row1"].matching_mode is None
    (out,) = tm.outputs
    assert out.filter is not None and out.filter.java_calls == ["equals"]
    assert {m.target for m in out.mappings} == {"order_id", "order_date", "amount", "customer_name", "country_name"}


def test_tmap_expressions_expose_java_calls(jobs: dict[str, Job]) -> None:
    tm = next(c for c in jobs["job_filter_map"].components if c.type == "tMap").tmap
    assert tm is not None
    calls = {m.target: m.expression.java_calls for m in tm.outputs[0].mappings}
    assert calls["country_code"] == ["StringHandling.UPCASE"]
    assert calls["email_lower"] == ["toLowerCase"]
    assert calls["load_date"] == ["TalendDate.getCurrentDate"]
    assert calls["given_name"] == []


def test_multi_output_tmap(jobs: dict[str, Job]) -> None:
    tm = next(c for c in jobs["job_multi_output"].components if c.type == "tMap").tmap
    assert tm is not None
    assert [o.name for o in tm.outputs] == ["out_active", "out_inactive"]
    assert [o.filter.raw if o.filter else None for o in tm.outputs] == ["row1.is_active", "!row1.is_active"]
    assert {c.source_output for c in jobs["job_multi_output"].connections if c.source == "tMap_1"} == {
        "out_active", "out_inactive"}


def test_lookup_connections_are_recognised(jobs: dict[str, Job]) -> None:
    kinds = [c.kind for c in jobs["job_join_lookup"].connections]
    assert kinds.count(ConnKind.LOOKUP) == 2 and kinds.count(ConnKind.FLOW) == 2


def test_merge_and_unique(jobs: dict[str, Job]) -> None:
    kinds = {c.kind for c in jobs["job_union_uniq"].connections}
    assert {ConnKind.MERGE, ConnKind.UNIQUE, ConnKind.FLOW} == kinds


def test_reads_and_writes(jobs: dict[str, Job]) -> None:
    j = jobs["job_join_lookup"]
    assert sorted(r.db_schema + "." + str(r.name) for r in j.reads) == [
        "REF.COUNTRIES", "SRC.CUSTOMERS", "SRC.ORDERS"]
    assert [(w.name, w.action) for w in j.writes] == [("ORDERS_ENRICHED", "insert")]
    agg = jobs["job_aggregate"]
    assert [r.name for r in agg.reads] == ["ORDERS"] and [w.name for w in agg.writes] == ["CUSTOMER_TOTALS"]


def test_file_sources_are_not_split_like_table_names(jobs: dict[str, Job]) -> None:
    (r,) = jobs["job_simple_copy"].reads
    assert r.via == "file" and r.name == "C:/data/customers.xlsx" and r.db_schema is None


def test_sql_template_reads_writes_and_context(jobs: dict[str, Job]) -> None:
    j = jobs["job_sql_template"]
    assert {(w.name, w.action) for w in j.writes} == {("ORDERS_DAILY", "truncate"), ("ORDERS_DAILY", "insert"),
                                                      ("ORDERS_DAILY", "update")}
    assert {r.db_schema + "." + str(r.name) for r in j.reads} == {"SRC.ORDERS", "SRC.CUSTOMERS"}
    target = next(w for w in j.writes if w.action == "truncate")
    assert target.resolution is Resolution.CONTEXT_DEPENDENT and target.db_schema == "STG_DEV"  # Default group
    assert target.expression.referenced_context == ["db_schema"]
    plain = next(r for r in j.reads if r.name == "ORDERS")
    assert plain.resolution is Resolution.RESOLVED  # only the context-built target is context dependent


def test_context_driven_job(jobs: dict[str, Job]) -> None:
    j = jobs["job_context_driven"]
    assert set(j.contexts) == {"Default", "Prod"}
    assert j.contexts["Default"]["db_password"].value is None and j.contexts["Default"]["db_password"].is_secret
    assert j.contexts["Prod"]["tgt_schema"].value == "STG"
    (w,) = j.writes
    assert (w.db_schema, w.name, w.resolution) == ("STG_DEV", "CUSTOMERS_COPY", Resolution.CONTEXT_DEPENDENT)
    assert {i.code for i in j.issues} == {IssueCode.UNRESOLVED_CONTEXT}


def test_context_override_changes_table_names() -> None:
    prod = scan(Path(__file__).resolve().parents[2] / "samples" / "talend", context="Prod")
    j = next(x for x in prod.jobs if x.name == "job_context_driven")
    assert j.writes[0].db_schema == "STG"


def test_orchestrator_children_are_resolved(jobs: dict[str, Job]) -> None:
    ch = jobs["job_orchestrator"].child_jobs
    assert [c.name for c in ch] == ["job_filter_map", "job_join_lookup", "job_simple_copy"]
    assert all(c.resolved and c.id for c in ch)
    kinds = [c.kind for c in jobs["job_orchestrator"].connections]
    assert kinds == [ConnKind.ON_SUBJOB_OK] * 2


def test_tracked_child_id_matches_child_properties(jobs: dict[str, Job]) -> None:
    root = Path(__file__).resolve().parents[2] / "samples" / "talend" / "process"
    for c in jobs["job_orchestrator"].child_jobs:
        assert f'id="{c.id}"' in (root / f"{c.name}_0.1.properties").read_text(encoding="utf-8")


def test_custom_java_jobs_are_flagged_with_code(jobs: dict[str, Job]) -> None:
    for name, comp_type in (("job_with_java", "tJava"), ("job_with_javarow", "tJavaRow")):
        j = jobs[name]
        assert IssueCode.CUSTOM_JAVA in {i.code for i in j.issues}
        c = next(c for c in j.components if c.type == comp_type)
        assert c.category is Category.CUSTOM_CODE and c.code


def test_triggers(jobs: dict[str, Job]) -> None:
    assert {c.kind for c in jobs["job_prejob_postjob"].connections} == {ConnKind.FLOW, ConnKind.ON_COMPONENT_OK}
    conds = [c.condition.raw for c in jobs["job_runif"].connections if c.kind is ConnKind.RUN_IF]
    assert sorted(conds) == ['!((Boolean)globalMap.get("load_full"))', '((Boolean)globalMap.get("load_full"))']
    assert len(jobs["job_prejob_postjob"].subjobs) == 5  # triggers never merge subjobs


def test_no_secrets_or_personal_data_in_models(result: ScanResult) -> None:
    blob = " ".join(j.model_dump_json() for j in result.jobs)
    assert "enc:system" not in blob  # no encrypted secrets
    assert not re.search(r"[A-Za-z]:[/\\]+Users[/\\]", blob)  # no local user-profile paths


def test_samples_use_no_unexpected_issue_codes(result: ScanResult) -> None:
    seen = {i.code for j in result.jobs for i in j.issues}
    assert seen <= {IssueCode.CUSTOM_JAVA, IssueCode.UNRESOLVED_CONTEXT, IssueCode.UNRESOLVED_REPOSITORY_REF}


def test_aggregate_unique_and_sort_settings(jobs: dict[str, Job]) -> None:
    agg = next(c for c in jobs["job_aggregate"].components if c.type == "tAggregateRow").aggregate
    assert agg is not None
    assert [(g.input_column, g.output_column) for g in agg.group_by] == [("customer_id", "customer_id")]
    assert [(o.output_column, o.function, o.input_column) for o in agg.operations] == [
        ("total_amount", "sum", "amount"), ("order_count", "count", "order_id"),
        ("last_order_date", "max", "order_date")]
    uniq = next(c for c in jobs["job_union_uniq"].components if c.type == "tUniqRow")
    assert uniq.unique_keys == ["customer_id"]
    srt = next(c for c in jobs["job_sort_row"].components if c.type == "tSortRow")
    assert [(k.column, k.sort, k.order) for k in (srt.sort_keys or [])] == [
        ("last_name", "alpha", "asc"), ("customer_id", "num", "desc")]
