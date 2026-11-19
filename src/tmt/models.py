"""Tool-independent intermediate model (see docs/intermediate_model_proposal.md).

Parsers produce :class:`Job` objects; everything downstream (lineage, scoring, reports,
generators) consumes only these models. All models forbid unknown fields so that golden
files catch drift, and none of them may carry secret values.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "0.4"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ schema
class Column(_Model):
    name: str
    type: str
    length: int | None = None
    precision: int | None = None
    nullable: bool = True
    is_key: bool = False
    pattern: str | None = None


class Schema(_Model):
    columns: list[Column] = Field(default_factory=list)


# ------------------------------------------------------------------ expressions
class Resolution(StrEnum):
    RESOLVED = "resolved"  # a plain literal (or constant concatenation)
    CONTEXT_DEPENDENT = "context_dependent"  # uses context.* in a form we can fold or not
    UNRESOLVED = "unresolved"  # anything else (column references, arbitrary Java, placeholders)


class Expression(_Model):
    raw: str
    resolution: Resolution
    value: str | None = None  # literal value when RESOLVED
    evaluated: str | None = None  # CONTEXT_DEPENDENT folded with the selected context group
    referenced_columns: list[str] = Field(default_factory=list)
    referenced_context: list[str] = Field(default_factory=list)
    java_calls: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ issues
class IssueCode(StrEnum):
    CUSTOM_JAVA = "custom_java"
    UNSUPPORTED_COMPONENT = "unsupported_component"
    UNVERIFIED_COMPONENT = "unverified_component"
    UNRESOLVED_CONTEXT = "unresolved_context"
    UNRESOLVED_REPOSITORY_REF = "unresolved_repository_ref"
    UNKNOWN_CONNECTOR = "unknown_connector"
    UNSUPPORTED_CONSTRUCT = "unsupported_construct"
    SQL_NOT_PARSED = "sql_not_parsed"
    MISSING_METADATA = "missing_metadata"
    PARSE_ERROR = "parse_error"
    PLACEHOLDER_VALUE = "placeholder_value"  # e.g. "$Name.READER.Query" left by a migration tool


class Issue(_Model):
    code: IssueCode
    severity: Literal["info", "warning", "error"]
    message: str
    component_id: str | None = None


class SourceRef(_Model):
    file: str
    line: int | None = None


# ------------------------------------------------------------------ tMap
class ColumnMapping(_Model):
    target: str
    expression: Expression


class JoinKey(_Model):
    """``lookup_column <operator> expression`` of a tMap lookup (``expression`` is evaluated on the main flow)."""

    lookup_column: str
    expression: Expression
    operator: str = "="


class TMapInput(_Model):
    name: str
    role: Literal["main", "lookup"]
    join_type: Literal["inner", "left"] | None = None
    matching_mode: str | None = None  # e.g. UNIQUE_MATCH: Talend keeps one match per row
    join_keys: list[JoinKey] = Field(default_factory=list)


class TMapOutput(_Model):
    name: str
    filter: Expression | None = None
    mappings: list[ColumnMapping] = Field(default_factory=list)
    is_reject: bool = False


class TMapLogic(_Model):
    inputs: list[TMapInput] = Field(default_factory=list)
    outputs: list[TMapOutput] = Field(default_factory=list)


# ------------------------------------------------------------------ other transforms
class GroupBy(_Model):
    input_column: str
    output_column: str


class AggOp(_Model):
    output_column: str
    function: str  # sum, count, max, ... as written by Studio
    input_column: str


class AggregateLogic(_Model):
    group_by: list[GroupBy] = Field(default_factory=list)
    operations: list[AggOp] = Field(default_factory=list)


class SortKey(_Model):
    column: str
    sort: str  # num | alpha | date
    order: str  # asc | desc


# ------------------------------------------------------------------ tables & SQL
Action = Literal["insert", "update", "upsert", "delete", "truncate", "select", "unknown"]


class TableRef(_Model):
    database: str | None = None
    db_schema: str | None = None
    name: str | None = None
    expression: Expression
    resolution: Resolution
    access: Literal["read", "write"]
    action: Action
    component_id: str
    via: Literal["table_param", "sql", "file"]


class SqlStatement(_Model):
    raw: Expression
    kind: Literal["select", "insert", "update", "delete", "merge", "ddl", "other"]
    tables_read: list[TableRef] = Field(default_factory=list)
    tables_written: list[TableRef] = Field(default_factory=list)
    parsed: bool


# ------------------------------------------------------------------ components & connections
class Category(StrEnum):
    INPUT = "input"
    OUTPUT = "output"
    SQL = "sql"
    TRANSFORM = "transform"
    ORCHESTRATION = "orchestration"
    CUSTOM_CODE = "custom_code"
    CONNECTION = "connection"
    LOG = "log"
    OTHER = "other"


class DbConnection(_Model):
    """The database connection a component uses. Never carries a user name or password."""

    name: str  # the metadata connection name (ODS_DB), or a description for built-in / unresolved ones
    kind: Literal["repository", "inferred", "built_in", "shared", "unresolved"]
    repo_id: str | None = None
    detail: str | None = None


class Component(_Model):
    id: str
    type: str
    category: Category
    implementation: Literal["classic", "component_kit"]
    label: str | None = None
    subjob_id: str | None = None
    params: dict[str, str] = Field(default_factory=dict)
    schemas: dict[str, Schema] = Field(default_factory=dict)
    tmap: TMapLogic | None = None
    aggregate: AggregateLogic | None = None
    unique_keys: list[str] | None = None  # tUniqRow key columns
    sort_keys: list[SortKey] | None = None  # tSortRow criteria
    sql: list[SqlStatement] = Field(default_factory=list)
    code: str | None = None
    supported: bool = True
    connection: DbConnection | None = None  # database components only
    issues: list[Issue] = Field(default_factory=list)
    source_ref: SourceRef | None = None


class ConnKind(StrEnum):
    FLOW = "flow"
    LOOKUP = "lookup"
    MERGE = "merge"
    UNIQUE = "unique"
    REJECT = "reject"
    ON_SUBJOB_OK = "on_subjob_ok"
    ON_COMPONENT_OK = "on_component_ok"
    RUN_IF = "run_if"
    UNKNOWN = "unknown"


class Connection(_Model):
    name: str
    source: str
    target: str
    kind: ConnKind
    label: str | None = None
    source_output: str | None = None  # tMap output table name
    condition: Expression | None = None  # RUN_IF only
    source_ref: SourceRef | None = None


class Subjob(_Model):
    id: str
    component_ids: list[str]


# ------------------------------------------------------------------ job
class ContextParam(_Model):
    name: str
    type: str
    value: str | None = None  # None for secrets
    is_secret: bool = False
    source: Literal["built_in", "repository"] = "built_in"


class ChildJobRef(_Model):
    name: str
    id: str | None = None  # the child's repository id as written by the calling job (matched against Job.repo_id)
    project: str | None = None  # the project the calling job says the child lives in ("DEV" in "DEV:_abc")
    resolved: bool = False
    key: str | None = None  # Job.key of the child when it was found among the scanned jobs


class Reason(_Model):
    factor: str
    points: int
    detail: str | None = None


class Complexity(_Model):
    score: int
    level: Literal["easy", "medium", "hard"]
    reasons: list[Reason] = Field(default_factory=list)


class Job(_Model):
    schema_version: str = SCHEMA_VERSION
    source_tool: str = "talend"  # BaseParser.name of the parser that produced this job
    tool_version: str | None = None
    name: str
    version: str
    path: str
    key: str = ""  # unique within a scan: the name, or name@project (...) when several jobs share a name
    repo_id: str | None = None  # Studio's id for the job; tRunJob refers to its child by this id
    project: str | None = None  # Studio project the job belongs to, when it can be determined
    folder: str = ""
    default_context: str = "Default"
    contexts: dict[str, dict[str, ContextParam]] = Field(default_factory=dict)
    components: list[Component] = Field(default_factory=list)
    connections: list[Connection] = Field(default_factory=list)
    subjobs: list[Subjob] = Field(default_factory=list)
    reads: list[TableRef] = Field(default_factory=list)
    writes: list[TableRef] = Field(default_factory=list)
    child_jobs: list[ChildJobRef] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    complexity: Complexity | None = None  # filled by the scorer (v0.2)

    @model_validator(mode="after")
    def _default_key(self) -> Job:
        if not self.key:
            self.key = self.name
        return self


class Failure(_Model):
    """A job that could not be parsed at all; collected, never fatal for the whole run."""

    path: str
    error: str
