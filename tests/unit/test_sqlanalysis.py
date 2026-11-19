from __future__ import annotations

from tmt.sqlanalysis import StatementInfo, TableName, analyze_sql, split_qualified


def reads(info: StatementInfo) -> list[str]:
    return sorted(t.qualified() for t in info.reads)


def writes(info: StatementInfo) -> list[tuple[str, str]]:
    return [(t.qualified(), a) for t, a in info.writes]


def test_select_reads_tables_not_ctes() -> None:
    info = analyze_sql("WITH c AS (SELECT 1 x FROM a.t) SELECT * FROM c JOIN b.u ON 1=1")
    assert info.kind == "select" and reads(info) == ["a.t", "b.u"]


def test_insert_select_writes_and_reads() -> None:
    info = analyze_sql("INSERT INTO S.T (a) SELECT x FROM R.U JOIN R.V ON 1=1")
    assert info.kind == "insert"
    assert writes(info) == [("S.T", "insert")]
    assert reads(info) == ["R.U", "R.V"]


def test_update_delete_merge_truncate() -> None:
    assert writes(analyze_sql("UPDATE S.T SET a=1 WHERE a<0")) == [("S.T", "update")]
    assert writes(analyze_sql("DELETE FROM S.T WHERE a=1")) == [("S.T", "delete")]
    m = analyze_sql("MERGE INTO S.T USING S.U ON (T.id=U.id) WHEN MATCHED THEN UPDATE SET T.a=U.a")
    assert writes(m) == [("S.T", "upsert")] and reads(m) == ["S.U"]
    assert writes(analyze_sql("TRUNCATE TABLE S.T")) == [("S.T", "truncate")]


def test_unparseable_sql_is_reported_not_raised() -> None:
    info = analyze_sql("this is ((( not sql")
    assert not info.parsed and info.reads == [] and info.writes == []


def test_split_qualified() -> None:
    assert split_qualified("T").qualified() == "T"
    assert split_qualified('"S"."T"').qualified() == "S.T"
    assert split_qualified("DB.S.T") == TableName("DB", "S", "T")
