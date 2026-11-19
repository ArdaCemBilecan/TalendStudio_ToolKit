from __future__ import annotations

import pytest

from tmt.models import Category
from tmt.parsers.talend.components import classify


@pytest.mark.parametrize(
    ("name", "category", "supported", "verified"),
    [
        ("tMap", Category.TRANSFORM, True, True),
        ("tSortRow", Category.TRANSFORM, True, True),  # ends with "Row" but is not SQL
        ("tLogRow", Category.LOG, True, True),
        ("tJavaRow", Category.CUSTOM_CODE, True, True),
        ("tOracleRow", Category.SQL, True, True),
        ("tDBRow", Category.SQL, True, True),
        ("JDBCInput", Category.INPUT, True, True),
        ("tPostgresqlInput", Category.INPUT, True, False),  # pattern only, no real sample
        ("tMysqlOutput", Category.OUTPUT, True, False),
        ("tOracleConnection", Category.CONNECTION, True, False),
        ("tFileInputJSON", Category.INPUT, False, True),  # out of scope
        ("tHDFSConnection", Category.OTHER, False, True),  # not a DB vendor
        ("tLogicGate", Category.OTHER, False, True),
    ],
)
def test_classification(name: str, category: Category, supported: bool, verified: bool) -> None:
    c = classify(name)
    assert (c.category, c.supported, c.verified) == (category, supported, verified)


def test_component_kit_is_flagged() -> None:
    assert classify("JDBCOutput").implementation == "component_kit"
    assert classify("tMap").implementation == "classic"
