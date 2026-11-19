# Intermediate model: proposal (revision of spec §5.1)

Status: **approved and implemented** (design record). The authoritative definition is
[src/tmt/models.py](../src/tmt/models.py); where the two differ, the code wins. Later additions: `JoinKey`
(lookup column and operator of a tMap join), `TMapInput.matching_mode`, `AggregateLogic`, `unique_keys`, `sort_keys`,
`Expression.evaluated`, and `Job.source_tool` as a free string so other tools' parsers can be added.

Every change from the §5.1 draft is justified by something observed in real Talend 8.0.1 files
(see `talend_format_notes.md`). Types are pydantic v2 models, JSON-serializable, tool-independent.

## What changed vs §5.1, and why

| # | §5.1 draft | Proposed | Reason (observed) |
|---|---|---|---|
| 1 | `Component.schema_in/schema_out` | `schemas: dict[str, Schema]` keyed by connector/output name; input schema derived from incoming connections | A tMap has several outputs, each with its own schema (`job_multi_output`); `tUniqRow` has `UNIQUE`, `tFileInputExcel` has `REJECT`. |
| 2 | `Expression.is_java: bool` | `resolution` + `java_calls: list[str]` | Every Talend value is a Java expression, so `is_java` is always true. What matters: is it a plain literal, context-dependent, or does it call methods (`.toLowerCase()`, `StringHandling.UPCASE`)? Drives both TableRef resolution and complexity scoring. |
| 3 | `Connection.kind` = flow/lookup/reject/on_subjob_ok/on_component_ok/run_if | adds `merge`, `unique`, `unknown`; adds `source_output`, `condition` | `FLOW` vs `MAIN` is vendor noise; `lineStyle` 0/8/10 decides main/lookup/merge. `UNIQUE` exists. `RUN_IF` has a Java `CONDITION`. tMap connections need the output name. Unseen kinds become `unknown`, never a crash. |
| 4 | `Component.type` only | `type` (raw `componentName`) + `category` (normalized) + `implementation` (`classic` / `component_kit`) | `JDBCInput`/`JDBCOutput` (Component Kit, no `t` prefix, `MAIN` connectors, `configuration.*` params) coexist with classic `tOracleRow`. Extractors register on `type`; downstream code uses `category`. |
| 5 | `Job.reads/writes: list[TableRef]` | kept, but `TableRef` gets `expression: Expression`, `resolution`, `access`, `component_id`, `via` (`table_param` / `sql`), `action` | Table names are Java expressions (`context.tgt_schema + "." + context.tgt_table`); SQL in `tOracleRow` yields reads *and* writes from one statement. |
| 6 | no tMap model | `TMapLogic` on the component: inputs (role, join type, join keys), outputs (filter, mappings) | Needed for lineage-free logic extraction, complexity (joins, Java calls) and dbt generation. |
| 7 | no SQL model | `SqlStatement` (raw, kind, tables read/written, `parsed: bool`) | `job_sql_template`: INSERT…SELECT with join, UPDATE, TRUNCATE. sqlglot may fail; keep raw and set `parsed=false`. |
| 8 | `Job.child_jobs: list[str]` | `list[ChildJobRef(name, id, resolved)]` | `tRunJob` references the child by repository id and name; the child may not be in the scanned folder. |
| 9 | `contexts: dict` | `dict[group, dict[name, ContextParam]]` with `type`, `value`, `is_secret`, `source` (`built_in`/`repository`) | `Default` and `Prod` groups; passwords are `enc:` values and must not be echoed into reports. |
| 10 | none | `Subjob`, `source_ref` (file + line) on every component/connection, `schema_version`, `source_tool` | Traceability in reports; `subjob` is a first-class Talend concept (triggers never merge subjobs); versioned JSON for goldens and future parsers. |
| 11 | `Job.flags: list[str]` | `list[Issue(code, component_id, message)]` with a closed `IssueCode` enum | Flags must be countable and testable: `custom_java`, `unsupported_component`, `unresolved_context`, `unresolved_repository_ref`, `unknown_connector`, `parse_error`. |

## Draft (pseudo-code)

```text
class Column(BaseModel):    name; type; length: int|None; precision: int|None; nullable: bool; is_key: bool; pattern: str|None
class Schema(BaseModel):    columns: list[Column]

class Resolution(StrEnum):  RESOLVED; CONTEXT_DEPENDENT; UNRESOLVED     # literal / uses context.* or concatenation / unknown
class Expression(BaseModel):
    raw: str
    resolution: Resolution
    value: str | None                      # the string if RESOLVED (quotes stripped), else None
    referenced_columns: list[str]          # row1.customer_id -> "row1.customer_id"
    referenced_context: list[str]          # context.db_host -> "db_host"
    java_calls: list[str]                  # ["StringHandling.UPCASE", "toLowerCase", "TalendDate.getCurrentDate"]

class Category(StrEnum):    INPUT; OUTPUT; SQL; TRANSFORM; ORCHESTRATION; CUSTOM_CODE; CONNECTION; LOG; OTHER
class ColumnMapping(BaseModel): target: str; expression: Expression
class TMapInput(BaseModel): name; role: Literal["main","lookup"]; join_type: Literal["inner","left"] | None; join_keys: list[Expression]
class TMapOutput(BaseModel): name; filter: Expression | None; mappings: list[ColumnMapping]; is_reject: bool
class TMapLogic(BaseModel): inputs: list[TMapInput]; outputs: list[TMapOutput]

class SqlStatement(BaseModel): raw: Expression; kind: Literal["select","insert","update","delete","merge","ddl","other"]
                               tables_read: list[TableRef]; tables_written: list[TableRef]; parsed: bool

class Component(BaseModel):
    id: str                                # UNIQUE_NAME
    type: str                              # raw componentName
    category: Category
    implementation: Literal["classic","component_kit"]
    label: str | None
    subjob_id: str
    params: dict[str, str]                 # raw, only parameters extractors care about
    schemas: dict[str, Schema]
    tmap: TMapLogic | None
    sql: list[SqlStatement]
    code: str | None                       # tJava / tJavaRow / tJavaFlex source, kept verbatim
    supported: bool
    issues: list[Issue]
    source_ref: SourceRef

class ConnKind(StrEnum):    FLOW; LOOKUP; MERGE; UNIQUE; REJECT; ON_SUBJOB_OK; ON_COMPONENT_OK; RUN_IF; UNKNOWN
class Connection(BaseModel): name; source; target; kind: ConnKind; label; source_output: str | None; condition: Expression | None; source_ref

class TableRef(BaseModel):
    database: str | None; schema_: str | None; name: str | None
    expression: Expression                 # how it was written
    resolution: Resolution
    access: Literal["read","write"]
    action: Literal["insert","update","upsert","delete","truncate","select","unknown"]
    component_id: str
    via: Literal["table_param","sql","file"]

class ContextParam(BaseModel): name; type; value: str | None; is_secret: bool; source: Literal["built_in","repository"]
class ChildJobRef(BaseModel): name; id: str | None; resolved: bool
class Subjob(BaseModel): id; component_ids: list[str]

class Complexity(BaseModel): score: int; level: Literal["easy","medium","hard"]; reasons: list[Reason]
class Job(BaseModel):
    schema_version: str; source_tool: Literal["talend"]; tool_version: str | None
    name; version; path; folder: str
    contexts: dict[str, dict[str, ContextParam]]; default_context: str
    components: list[Component]; connections: list[Connection]; subjobs: list[Subjob]
    reads: list[TableRef]; writes: list[TableRef]; child_jobs: list[ChildJobRef]
    issues: list[Issue]; complexity: Complexity | None
```

Invariants the parser/serializer will enforce: stable ordering (components/connections sorted by id,
dict keys sorted) so output is byte-identical; no secret values in JSON; unknown content is kept as
an `Issue`, never dropped.

## Decisions needed from you

1. **Repository references.** In v0.1, jobs that point to `metadata/connections` or `metadata/fileExcel`
   items (like the old `DataFlow` job) need those files to resolve. Options:
   **(a, recommended)** resolve *context* repository items (needed for `context.*` values) but only
   *flag* repository schemas/connections as `unresolved_repository_ref`; **(b)** resolve all in v0.1.
2. **`tOracleInput`/`tOracleOutput`.** Never observed. Plan: classic `t<DB>Input/Output/Row` handled by
   pattern, and flagged `unverified` in the report until a real sample exists. OK?
3. **Strictness.** Models `extra="forbid"` (safer, goldens catch drift) or `extra="ignore"`? Recommended: forbid.
4. **Secrets.** Never put passwords (`enc:...`, or `context.*_password` values) in JSON/reports.
   Context values marked `id_Password` are emitted as `null` with `is_secret=true`. OK?

Everything else above I plan to implement as written, unless you object.
