from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import conn, node, param, write_job
from tmt.errors import ParseError
from tmt.models import ConnKind, IssueCode, Resolution
from tmt.parsers.talend.parser import TalendParser

P = TalendParser()


def codes(job) -> set[str]:  # type: ignore[no-untyped-def]
    return {i.code.value for i in job.issues}


def test_unknown_component_is_recorded_not_fatal(tmp_path: Path) -> None:
    job = P.parse(write_job(tmp_path, "j", node("tFooBar_1", "tFooBar", param("X", "1"))))
    c = job.components[0]
    assert c.type == "tFooBar" and c.supported is False
    assert IssueCode.UNSUPPORTED_COMPONENT.value in codes(job)


def test_missing_parameters_do_not_crash(tmp_path: Path) -> None:
    body = node("t_1", "JDBCInput") + node("t_2", "JDBCOutput") + node("t_3", "tOracleRow") + node("t_4", "tMap")
    job = P.parse(write_job(tmp_path, "j", body))
    assert len(job.components) == 4 and job.reads == [] and job.writes == []


def test_node_without_unique_name_gets_generated_id(tmp_path: Path) -> None:
    item = write_job(tmp_path, "j", '<node componentName="tLogRow"/>\n')
    job = P.parse(item)
    assert job.components[0].id.startswith("tLogRow_unnamed")
    assert IssueCode.MISSING_METADATA.value in codes(job)


def test_name_and_version_fall_back_to_file_name(tmp_path: Path) -> None:
    job = P.parse(write_job(tmp_path, "my_job", node("a_1", "tLogRow")))
    assert (job.name, job.version) == ("my_job", "0.1")
    assert IssueCode.MISSING_METADATA.value in codes(job)  # no .properties next to it


def test_passwords_are_never_copied(tmp_path: Path) -> None:
    body = node("t_1", "tOracleRow", param("PASS", "SuperSecret1", "PASSWORD") + param("USER", "bob"))
    ctx = (
        '<context name="Default"><contextParameter name="pw" type="id_Password" value="enc:system.encryption'
        '.key.v1:abc"/><contextParameter name="u" type="id_String" value="bob"/></context>\n'
    )
    job = P.parse(write_job(tmp_path, "j", body, ctx))
    dumped = job.model_dump_json()
    assert "SuperSecret1" not in dumped and "enc:system" not in dumped
    pw = job.contexts["Default"]["pw"]
    assert pw.is_secret and pw.value is None
    assert job.contexts["Default"]["u"].value == "bob"


def test_context_group_selection_changes_folded_value(tmp_path: Path) -> None:
    ctx = (
        '<context name="Default"><contextParameter name="t" type="id_String" value="DEV_T"/></context>\n'
        '<context name="Prod"><contextParameter name="t" type="id_String" value="PROD_T"/></context>\n'
    )
    body = node("o_1", "JDBCOutput", param("configuration.dataSet.tableName", "context.t"))
    item = write_job(tmp_path, "j", body, ctx)
    assert P.parse(item).writes[0].name == "DEV_T"
    prod = P.parse(item, context="Prod")
    assert prod.writes[0].name == "PROD_T" and prod.writes[0].resolution is Resolution.CONTEXT_DEPENDENT
    missing = P.parse(item, context="Nope")
    assert missing.writes[0].name == "DEV_T"
    assert IssueCode.MISSING_METADATA.value in codes(missing)


def test_unresolved_context_without_value_is_flagged(tmp_path: Path) -> None:
    body = node("o_1", "JDBCOutput", param("configuration.dataSet.tableName", 'context.s + ".T"'))
    job = P.parse(write_job(tmp_path, "j", body))
    w = job.writes[0]
    assert w.name is None and w.resolution is Resolution.CONTEXT_DEPENDENT
    assert any(i.code is IssueCode.UNRESOLVED_CONTEXT and i.severity == "warning" for i in job.issues)


@pytest.mark.parametrize(
    ("connector", "style", "kind"),
    [
        ("FLOW", "0", ConnKind.FLOW), ("MAIN", "0", ConnKind.FLOW), ("MAIN", "8", ConnKind.LOOKUP),
        ("FLOW", "10", ConnKind.MERGE), ("UNIQUE", "0", ConnKind.UNIQUE), ("REJECT", "2", ConnKind.REJECT),
        ("SUBJOB_OK", "1", ConnKind.ON_SUBJOB_OK), ("COMPONENT_OK", "3", ConnKind.ON_COMPONENT_OK),
        ("RUN_IF", "1", ConnKind.RUN_IF), ("ITERATE", "4", ConnKind.UNKNOWN), ("FLOW", "99", ConnKind.UNKNOWN),
    ],
)
def test_connection_kinds(tmp_path: Path, connector: str, style: str, kind: ConnKind) -> None:
    body = node("a_1", "tLogRow") + node("b_1", "tLogRow") + conn("a_1", "b_1", connector, style)
    job = P.parse(write_job(tmp_path, "j", body))
    assert job.connections[0].kind is kind
    assert (IssueCode.UNKNOWN_CONNECTOR.value in codes(job)) == (kind is ConnKind.UNKNOWN)


def test_subjobs_follow_data_flow_not_triggers(tmp_path: Path) -> None:
    body = (
        node("a_1", "tLogRow") + node("b_1", "tLogRow") + node("c_1", "tLogRow")
        + conn("a_1", "b_1", label="row1") + conn("b_1", "c_1", "SUBJOB_OK", "1", "OnSubjobOk")
    )
    job = P.parse(write_job(tmp_path, "j", body))
    assert [s.component_ids for s in job.subjobs] == [["a_1", "b_1"], ["c_1"]]
    assert {c.id: c.subjob_id for c in job.components}["b_1"] == "a_1"


def test_run_if_condition_is_kept(tmp_path: Path) -> None:
    c = (
        '<connection connectorName="RUN_IF" label="If1" lineStyle="1" metaname="a_1" source="a_1" target="b_1">'
        '<elementParameter field="TEXT" name="CONDITION" value="((Boolean)globalMap.get(&quot;x&quot;))"/>'
        '<elementParameter field="TEXT" name="UNIQUE_NAME" value="If1"/></connection>\n'
    )
    job = P.parse(write_job(tmp_path, "j", node("a_1", "tJava") + node("b_1", "tJava") + c))
    cond = job.connections[0].condition
    assert cond is not None and cond.raw == '((Boolean)globalMap.get("x"))'


def test_custom_java_is_flagged_and_code_kept(tmp_path: Path) -> None:
    body = node("j_1", "tJava", param("CODE", 'System.out.println("hi");', "MEMO_EDITOR_JAVA"))
    job = P.parse(write_job(tmp_path, "j", body))
    assert job.components[0].code == 'System.out.println("hi");'
    assert IssueCode.CUSTOM_JAVA.value in codes(job)


def test_component_named_tdbrow_is_classified_by_component_name(tmp_path: Path) -> None:
    body = node("tDBRow_1", "tOracleRow", param("QUERY", '"UPDATE S.T SET a = 1"', "MEMO_SQL"))
    job = P.parse(write_job(tmp_path, "j", body))
    assert job.components[0].category.value == "sql"
    assert [(w.name, w.action) for w in job.writes] == [("T", "update")]


def test_non_talend_xml_and_garbage_raise_parse_error(tmp_path: Path) -> None:
    bad = tmp_path / "x_0.1.item"
    bad.write_text("<root/>", encoding="utf-8")
    with pytest.raises(ParseError):
        P.parse(bad)
    bad.write_text("not xml at all", encoding="utf-8")
    with pytest.raises(ParseError):
        P.parse(bad)
    bad.write_bytes(b"")
    with pytest.raises(ParseError):
        P.parse(bad)


def test_can_parse_is_cheap_and_selective(tmp_path: Path) -> None:
    good = write_job(tmp_path, "j", node("a_1", "tLogRow"))
    other = tmp_path / "meta.item"
    other.write_text("<?xml version='1.0'?><xmi:XMI xmlns:xmi='x'/>", encoding="utf-8")
    assert P.can_parse(good) and not P.can_parse(other) and not P.can_parse(tmp_path / "missing.item")
    assert not P.can_parse(tmp_path / "j_0.1.properties")
