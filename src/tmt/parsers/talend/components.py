"""Per-component extractors (registry pattern: adding a component is one decorated function).

Classification is by ``componentName`` only, never by ``UNIQUE_NAME`` (a ``tOracleRow`` can be named
``tDBRow_1``). Components never seen in a real Studio file are tagged UNVERIFIED_COMPONENT.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from lxml import etree

from tmt.models import (
    Action,
    AggOp,
    AggregateLogic,
    Category,
    ChildJobRef,
    Expression,
    GroupBy,
    Issue,
    IssueCode,
    Resolution,
    SortKey,
    SqlStatement,
    TableRef,
    TMapLogic,
)
from tmt.parsers.talend.expressions import parse_expression, sql_text
from tmt.parsers.talend.tmap import extract_tmap
from tmt.sqlanalysis import TableName, analyze_sql, split_qualified

Implementation = str  # "classic" | "component_kit"


# ------------------------------------------------------------------ classification
@dataclass(frozen=True)
class Classification:
    category: Category
    implementation: Implementation = "classic"
    supported: bool = True
    verified: bool = True


_EXACT: dict[str, Classification] = {
    **{n: Classification(Category.TRANSFORM) for n in
       ("tMap", "tFilterRow", "tSortRow", "tUniqRow", "tAggregateRow", "tJoin", "tUnite")},
    **{n: Classification(Category.ORCHESTRATION) for n in ("tRunJob", "tPrejob", "tPostjob", "tWarn", "tDie")},
    **{n: Classification(Category.CUSTOM_CODE) for n in ("tJava", "tJavaRow", "tJavaFlex")},
    "tLogRow": Classification(Category.LOG),
    "tFileInputExcel": Classification(Category.INPUT),
    "tFileInputDelimited": Classification(Category.INPUT),
    "tFixedFlowInput": Classification(Category.INPUT),
    "tFileOutputDelimited": Classification(Category.OUTPUT),
    "JDBCInput": Classification(Category.INPUT, "component_kit"),
    "JDBCOutput": Classification(Category.OUTPUT, "component_kit"),
    "JDBCRow": Classification(Category.SQL, "component_kit", verified=False),
    "tOracleRow": Classification(Category.SQL),
    "tDBRow": Classification(Category.SQL),
}

_DB_VENDORS = (
    "Oracle|Postgresql|Mysql|MSSql|DB2|Snowflake|Teradata|Redshift|Vertica|Sybase|Informix|Netezza|"
    "Greenplum|MariaDB|SQLite|Exasol|AS400|JDBC|Access|Firebird|HSQLDb|Ingres|Interbase|ParAccel|SAPHana|DB"
)
_DB_RE = re.compile(rf"^t(?:{_DB_VENDORS})(Input|Output|Row|Connection|Commit|Close|Rollback)$")
_DB_CATEGORY = {"Input": Category.INPUT, "Output": Category.OUTPUT, "Row": Category.SQL}


def classify(component_name: str) -> Classification:
    if component_name in _EXACT:
        return _EXACT[component_name]
    m = _DB_RE.match(component_name)
    if m:
        # the t<DB>Input/Output pattern is handled generically but has no real Studio sample yet
        return Classification(_DB_CATEGORY.get(m.group(1), Category.CONNECTION), verified=False)
    if re.match(r"^tFile(Input|Output)\w+$", component_name):
        cat = Category.INPUT if component_name.startswith("tFileInput") else Category.OUTPUT
        return Classification(cat, supported=False)
    return Classification(Category.OTHER, supported=False)


# ------------------------------------------------------------------ extraction plumbing
@dataclass
class Ctx:
    uid: str
    ctype: str
    node: etree._Element
    params: Mapping[str, str]  # every elementParameter, hidden ones included (TABLE fields excluded)
    context_values: Mapping[str, str | None]
    dialect: str | None
    lookup_labels: set[str] = field(default_factory=set)
    tables: Mapping[str, list[dict[str, str]]] = field(default_factory=dict)  # TABLE params as rows


@dataclass
class Extraction:
    sql: list[SqlStatement] = field(default_factory=list)
    reads: list[TableRef] = field(default_factory=list)
    writes: list[TableRef] = field(default_factory=list)
    tmap: TMapLogic | None = None
    aggregate: AggregateLogic | None = None
    unique_keys: list[str] | None = None
    sort_keys: list[SortKey] | None = None
    code: str | None = None
    child: ChildJobRef | None = None
    issues: list[Issue] = field(default_factory=list)


Extractor = Callable[[Ctx], Extraction]
_BY_TYPE: dict[str, Extractor] = {}
_BY_CATEGORY: dict[Category, Extractor] = {}


def extractor(*types: str) -> Callable[[Extractor], Extractor]:
    def deco(fn: Extractor) -> Extractor:
        for t in types:
            _BY_TYPE[t] = fn
        return fn

    return deco


def category_extractor(category: Category) -> Callable[[Extractor], Extractor]:
    def deco(fn: Extractor) -> Extractor:
        _BY_CATEGORY[category] = fn
        return fn

    return deco


def extract(ctx: Ctx, category: Category) -> Extraction:
    fn = _BY_TYPE.get(ctx.ctype) or _BY_CATEGORY.get(category)
    ext = fn(ctx) if fn else Extraction()
    _flag_placeholders(ctx, ext)
    return ext


# ------------------------------------------------------------------ helpers
def _first(ctx: Ctx, *names: str) -> str | None:
    for n in names:
        v = ctx.params.get(n)
        if v is not None and v.strip() not in ("", '""'):
            return v
    return None


def _expr_value(e: Expression) -> str | None:
    return e.value if e.resolution is Resolution.RESOLVED else e.evaluated


def _table_from_expression(ctx: Ctx, raw: str, access: str, action: Action, via: str) -> TableRef:
    e = parse_expression(raw, ctx.context_values)
    value = _expr_value(e)
    if value and via == "file":  # a path is not a qualified name; do not split on '.'
        tn: TableName | None = TableName(None, None, value)
    elif value and via == "table_param" and _implausible_table(value):
        # e.g. Studio's default label "Output for table": not a table name, so do not pretend it is one
        e = e.model_copy(update={"resolution": Resolution.UNRESOLVED})
        tn = None
    else:
        tn = split_qualified(value) if value else None
    return TableRef(
        database=tn.database if tn else None,
        db_schema=tn.schema if tn else None,
        name=tn.name if tn else None,
        expression=e,
        resolution=e.resolution,
        access=access,
        action=action,
        component_id=ctx.uid,
        via=via,
    )


_IDENT_PATH = re.compile(r"^[\w$#]+(\.[\w$#]+){0,2}$")


def _implausible_table(value: str) -> bool:
    """True for text that cannot be a table name (spaces, symbols), unless it is explicitly quoted."""
    if '"' in value or "`" in value or "[" in value:
        return False
    return not _IDENT_PATH.match(value.strip())


def _flag_placeholders(ctx: Ctx, ext: Extraction) -> None:
    for ref in (*ext.reads, *ext.writes):
        if ref.via == "table_param" and ref.name is None and ref.expression.value is not None:
            ext.issues.append(Issue(
                code=IssueCode.PLACEHOLDER_VALUE, severity="warning", component_id=ctx.uid,
                message=f"{ref.access} table name {ref.expression.value!r} is a default label or placeholder, "
                        "not a table name; the real target is not in the job file"))


_MARKER_RE = re.compile(r"__CTX_([A-Za-z_]\w*?)__")


def _fill(name: str, values: Mapping[str, str | None]) -> tuple[str | None, list[str]]:
    """Replace context markers in a table name; returns (evaluated or None, names of used context vars)."""
    used: list[str] = []
    ok = True

    def sub(m: re.Match[str]) -> str:
        nonlocal ok
        used.append(m.group(1))
        v = values.get(m.group(1))
        if v is None:
            ok = False
            return m.group(0)
        return v

    out = _MARKER_RE.sub(sub, name)
    return (out if ok else None), used


def _to_java(name: str) -> str:
    """Rebuild a Java-ish expression for a table name that contains context markers."""
    parts, pos = [], 0
    for m in _MARKER_RE.finditer(name):
        if m.start() > pos:
            parts.append('"' + name[pos:m.start()] + '"')
        parts.append(f"context.{m.group(1)}")
        pos = m.end()
    if pos < len(name):
        parts.append('"' + name[pos:] + '"')
    return " + ".join(parts) if parts else '""'


def _sql_table(ctx: Ctx, tn: TableName, access: str, action: Action) -> TableRef:
    qualified = tn.qualified()
    value, used = _fill(qualified, ctx.context_values)
    expr = parse_expression(_to_java(qualified), ctx.context_values)
    target = split_qualified(value) if value else None
    return TableRef(
        database=target.database if target else None,
        db_schema=target.schema if target else None,
        name=target.name if target else None,
        expression=expr,
        resolution=Resolution.CONTEXT_DEPENDENT if used else Resolution.RESOLVED,
        access=access,
        action=action,
        component_id=ctx.uid,
        via="sql",
    )


def sql_statement(ctx: Ctx, raw: str) -> Extraction:
    out = Extraction()
    expr = parse_expression(raw, ctx.context_values)
    text, _ = sql_text(raw)
    if text is not None and expr.resolution is Resolution.UNRESOLVED:
        out.sql.append(SqlStatement(raw=expr, kind="other", parsed=False))
        out.issues.append(Issue(
            code=IssueCode.PLACEHOLDER_VALUE, severity="warning", component_id=ctx.uid,
            message=f"query {text!r} is a placeholder left by a migration tool; the real SQL is not in the job file"))
        return out
    if text is None:
        out.sql.append(SqlStatement(raw=expr, kind="other", parsed=False))
        out.issues.append(Issue(code=IssueCode.SQL_NOT_PARSED, severity="warning", component_id=ctx.uid,
                                message="query is not a plain string / context concatenation; tables unknown"))
        return out
    info = analyze_sql(text, ctx.dialect)
    stmt = SqlStatement(
        raw=expr,
        kind=info.kind,
        tables_read=sorted((_sql_table(ctx, t, "read", "select") for t in info.reads),
                           key=lambda r: r.expression.raw),
        tables_written=sorted((_sql_table(ctx, t, "write", a) for t, a in info.writes),
                              key=lambda r: r.expression.raw),
        parsed=info.parsed and info.kind != "other",
    )
    out.sql.append(stmt)
    out.reads.extend(stmt.tables_read)
    out.writes.extend(stmt.tables_written)
    if not stmt.parsed:
        out.issues.append(Issue(code=IssueCode.SQL_NOT_PARSED, severity="warning", component_id=ctx.uid,
                                message="SQL could not be parsed or is not a recognised statement; "
                                        "tables read/written unknown"))
    return out


_DATA_ACTION: dict[str, Action] = {
    "INSERT": "insert", "UPDATE": "update", "DELETE": "delete",
    "INSERT_OR_UPDATE": "upsert", "UPDATE_OR_INSERT": "upsert",
}


# ------------------------------------------------------------------ extractors
@extractor("tFileInputExcel", "tFileInputDelimited")
def _file_input(ctx: Ctx) -> Extraction:
    out = Extraction()
    path = _first(ctx, "FILENAME")
    if path:
        out.reads.append(_table_from_expression(ctx, path, "read", "select", "file"))
    return out


@extractor("tFileOutputDelimited")
def _file_output(ctx: Ctx) -> Extraction:
    out = Extraction()
    path = _first(ctx, "FILENAME")
    if path:
        out.writes.append(_table_from_expression(ctx, path, "write", "insert", "file"))
    return out


@extractor("JDBCInput")
def _jdbc_input(ctx: Ctx) -> Extraction:
    query = _first(ctx, "configuration.dataSet.sqlQuery")
    if query:
        return sql_statement(ctx, query)
    out = Extraction()
    table = _first(ctx, "configuration.dataSet.tableName")
    if table:
        out.reads.append(_table_from_expression(ctx, table, "read", "select", "table_param"))
    return out


@extractor("JDBCOutput")
def _jdbc_output(ctx: Ctx) -> Extraction:
    out = Extraction()
    table = _first(ctx, "configuration.dataSet.tableName")
    action = _DATA_ACTION.get(ctx.params.get("configuration.dataAction", "INSERT"), "unknown")
    if table:
        out.writes.append(_table_from_expression(ctx, table, "write", action, "table_param"))
    return out


@category_extractor(Category.SQL)
def _sql_component(ctx: Ctx) -> Extraction:
    query = _first(ctx, "QUERY")
    return sql_statement(ctx, query) if query else Extraction()


@category_extractor(Category.INPUT)
def _db_input(ctx: Ctx) -> Extraction:
    if not _DB_RE.match(ctx.ctype):
        return Extraction()
    query = _first(ctx, "QUERY")
    if query:
        return sql_statement(ctx, query)
    out = Extraction()
    table = _first(ctx, "TABLE")
    if table:
        out.reads.append(_table_from_expression(ctx, table, "read", "select", "table_param"))
    return out


@category_extractor(Category.OUTPUT)
def _db_output(ctx: Ctx) -> Extraction:
    out = Extraction()
    if not _DB_RE.match(ctx.ctype):
        return out
    table = _first(ctx, "TABLE")
    if table:
        action = _DATA_ACTION.get(ctx.params.get("DATA_ACTION", "INSERT"), "unknown")
        out.writes.append(_table_from_expression(ctx, table, "write", action, "table_param"))
    return out


@extractor("tRunJob")
def _run_job(ctx: Ctx) -> Extraction:
    out = Extraction()
    name = (_first(ctx, "PROCESS") or "").strip()
    ref = ctx.params.get("PROCESS:PROCESS_TYPE_PROCESS", "")
    child_project, _, child_id = ref.partition(":") if ":" in ref else (None, "", None)
    if name or child_id:
        out.child = ChildJobRef(name=name, id=child_id or None, project=child_project or None, resolved=False)
    return out


@extractor("tMap")
def _tmap(ctx: Ctx) -> Extraction:
    out = Extraction()
    out.tmap, has_vars = extract_tmap(ctx.node, ctx.lookup_labels, ctx.context_values)
    if has_vars:
        out.issues.append(Issue(code=IssueCode.UNSUPPORTED_CONSTRUCT, severity="warning", component_id=ctx.uid,
                                message="tMap variables (Var table) are not modelled; expressions may "
                                        "reference them"))
    return out


@category_extractor(Category.CUSTOM_CODE)
def _custom_code(ctx: Ctx) -> Extraction:
    out = Extraction(code=ctx.params.get("CODE"))
    out.issues.append(Issue(code=IssueCode.CUSTOM_JAVA, severity="warning", component_id=ctx.uid,
                            message=f"{ctx.ctype} contains custom Java; flagged for manual review"))
    return out


@extractor("tAggregateRow")
def _aggregate(ctx: Ctx) -> Extraction:
    groups = [GroupBy(input_column=r.get("INPUT_COLUMN", ""), output_column=r.get("OUTPUT_COLUMN", ""))
              for r in ctx.tables.get("GROUPBYS", [])]
    ops = [AggOp(output_column=r.get("OUTPUT_COLUMN", ""), function=r.get("FUNCTION", ""),
                 input_column=r.get("INPUT_COLUMN", "")) for r in ctx.tables.get("OPERATIONS", [])]
    return Extraction(aggregate=AggregateLogic(group_by=groups, operations=ops))


@extractor("tUniqRow")
def _uniq(ctx: Ctx) -> Extraction:
    keys = [r["SCHEMA_COLUMN"] for r in ctx.tables.get("UNIQUE_KEY", [])
            if r.get("KEY_ATTRIBUTE") == "true" and "SCHEMA_COLUMN" in r]
    return Extraction(unique_keys=keys)


@extractor("tSortRow")
def _sort(ctx: Ctx) -> Extraction:
    return Extraction(sort_keys=[
        SortKey(column=r.get("COLNAME", ""), sort=r.get("SORT", ""), order=r.get("ORDER", ""))
        for r in ctx.tables.get("CRITERIA", [])
    ])
