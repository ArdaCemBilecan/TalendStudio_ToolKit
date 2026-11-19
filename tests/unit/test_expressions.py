from __future__ import annotations

import pytest

from tmt.models import Resolution
from tmt.parsers.talend.expressions import java_calls, parse_expression, sql_text

CTX = {"schema": "STG", "table": "ORDERS", "pwd": None}


def test_plain_literal_is_resolved() -> None:
    e = parse_expression('"STG.CUSTOMERS"')
    assert (e.resolution, e.value) == (Resolution.RESOLVED, "STG.CUSTOMERS")


def test_constant_concatenation_is_resolved() -> None:
    e = parse_expression('"STG" + "." + "T"')
    assert (e.resolution, e.value) == (Resolution.RESOLVED, "STG.T")


def test_escapes_are_decoded() -> None:
    assert parse_expression(r'"a\"b\n"').value == 'a"b\n'


def test_empty_is_resolved_empty() -> None:
    assert parse_expression("   ").value == ""


def test_context_concat_folds_with_selected_group() -> None:
    e = parse_expression('context.schema + "." + context.table', CTX)
    assert e.resolution is Resolution.CONTEXT_DEPENDENT
    assert e.evaluated == "STG.ORDERS"
    assert e.referenced_context == ["schema", "table"]


def test_context_without_value_is_not_folded() -> None:
    e = parse_expression('"x" + context.pwd', CTX)
    assert e.resolution is Resolution.CONTEXT_DEPENDENT and e.evaluated is None


def test_context_in_other_java_is_context_dependent_but_not_folded() -> None:
    e = parse_expression("context.schema.toLowerCase()", CTX)
    assert e.resolution is Resolution.CONTEXT_DEPENDENT and e.evaluated is None


def test_context_inside_string_is_not_a_reference() -> None:
    e = parse_expression('"context.schema"', CTX)
    assert e.resolution is Resolution.RESOLVED and e.referenced_context == []


@pytest.mark.parametrize("raw", ['"$Stage.READER.Query"', "row1.a + row1.b", "foo(x)", '"a" +', '+ "a"'])
def test_everything_else_is_unresolved(raw: str) -> None:
    assert parse_expression(raw).resolution is Resolution.UNRESOLVED


def test_columns_found_for_known_tables_only() -> None:
    e = parse_expression('row1.first_name + " " + row2.last_name + other.x', tables=["row1", "row2"])
    assert e.referenced_columns == ["row1.first_name", "row2.last_name"]


def test_columns_inside_strings_are_ignored() -> None:
    assert parse_expression('"row1.a"', tables=["row1"]).referenced_columns == []


def test_java_calls_static_and_instance() -> None:
    assert java_calls("StringHandling.UPCASE(row1.a)") == ["StringHandling.UPCASE"]
    assert java_calls("row1.email.toLowerCase()") == ["toLowerCase"]
    assert java_calls("String.valueOf(row1.a).trim()") == ["String.valueOf", "trim"]
    assert java_calls("row1.a == null ? null : row1.a") == []


def test_sql_text_uses_markers_for_context() -> None:
    text, marked = sql_text('"SELECT * FROM " + context.schema + ".T"')
    assert text == "SELECT * FROM __CTX_schema__.T" and marked


def test_sql_text_rejects_other_java() -> None:
    assert sql_text("foo(1) + bar") == (None, False)
