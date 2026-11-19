# Talend Migration Toolkit — Project Specification

> A hand-off brief for an AI coding assistant. Read this whole file before writing any code.
> Working name: `talend-migration-toolkit` (CLI name: `tmt`).

---

## 1. Goal

Build an open-source Python CLI that reads **Talend Studio job files** and automatically produces:

1. A structured, tool-independent **intermediate model** (JSON) of every job.
2. A **lineage / dependency graph** between jobs and tables, plus a suggested migration order.
3. A **complexity report** (easy / medium / hard) per job, for effort estimation.
4. A **dbt/SQL skeleton** for jobs simple enough to translate. Hard jobs are flagged for manual review, never silently mistranslated.

**Why it exists:** in ETL migration projects, most time goes to *discovery* (what does each job read, write, transform, and depend on?), not to coding. This tool automates discovery and gives a head start on the target code.

**Portfolio purpose:** this repo will be public on GitHub to show data-engineering depth (parsing, graph analysis, code generation, CLI design, testing, CI). It must be clean, documented, and runnable by a stranger in under 5 minutes.

---

## 2. Scope

### In scope (v1)
- Talend Studio **Data Integration (standard) jobs** stored as `.item` XML files (plus companion `.properties`).
- Context variables and context groups.
- Subjob / component connections and trigger types (OnSubjobOk, OnComponentOk, RunIf, etc.).
- Common components (see section 6).
- `tMap` expression and join extraction.
- `tRunJob` parent→child relationships.
- Output: JSON model, Mermaid/Graphviz graph, Markdown + HTML report, dbt skeleton.

### Out of scope (explicitly)
- Big Data / Spark / streaming Talend jobs, Routes (ESB), Data Services.
- Talend Cloud / TMC artifacts and TAC scheduling export.
- Full, guaranteed-correct automatic translation of arbitrary Java code (`tJava`, `tJavaRow`, custom routines). These are **detected and flagged**, not translated.
- Executing jobs or connecting to real databases. The tool is static analysis only.
- DataStage and other ETL tools in v1. The architecture must allow them later via a plugin interface (see section 5).

---

## 3. What we need before coding (inputs & prerequisites)

The AI assistant must **not guess** the Talend file schema. The `.item` format varies by Talend version. Therefore:

### 3.1 Required from the project owner
| Item | Why | Status |
|---|---|---|
| 10–15 **sample `.item` files** (synthetic, self-authored) | Ground truth for parser development and tests | TO PROVIDE |
| Talend Studio **version** used to create them (e.g. 8.0.1) | Schema differs between versions; documented in README | TO PROVIDE |
| Target SQL dialect for dbt output (Snowflake recommended) | Affects generated SQL | DEFAULT: Snowflake |
| Python version | Compatibility | DEFAULT: 3.11+ |

### 3.2 How to create the sample jobs (no customer data!)
Use **Talend Open Studio / Talend Studio free edition** and a public dataset (e.g. AdventureWorks, or a small CSV set). Build jobs such as:

1. `job_simple_copy`: `tFileInputDelimited → tDBOutput` (straight load)
2. `job_filter_map`: `tDBInput → tMap (filter + column rename) → tDBOutput`
3. `job_join_lookup`: `tDBInput (main) + tDBInput (lookup) → tMap (inner/left join) → tDBOutput`
4. `job_aggregate`: `tDBInput → tAggregateRow → tDBOutput`
5. `job_context_driven`: uses context variables for DB connection and table names
6. `job_sort_unique`: `tSortRow`, `tUniqRow`
7. `job_with_java`: contains a `tJavaRow` (must be flagged as manual)
8. `job_orchestrator`: parent job with `tRunJob` calling 2–3 child jobs, `OnSubjobOk` triggers
9. `job_runif`: conditional flow using `RunIf`
10. `job_multi_output`: tMap with two outputs (valid rows and reject)
11. `job_sql_template`: `tDBRow` / `tDBInput` with a hand-written SQL query
12. `job_prejob_postjob`: with `tPrejob` / `tPostjob`

**Never commit real customer jobs.** All samples must be synthetic. Add a note in the README.

### 3.3 Tooling / dependencies
- Python 3.11+, `uv` or `pip` + `venv`
- `lxml` (XML parsing), `networkx` (graph), `typer` (CLI), `pydantic` v2 (models), `jinja2` (report and dbt templates), `sqlglot` (SQL parsing / dialect handling), `rich` (CLI output)
- Dev: `pytest`, `pytest-cov`, `ruff`, `mypy`, `pre-commit`
- CI: GitHub Actions (lint + type check + tests on Python 3.11 and 3.12)
- Optional: Graphviz binary (for PNG export), Mermaid output needs no binary

---

## 4. Talend file format — what to expect (VERIFY against the samples)

> These notes are a starting hypothesis. **Confirm every item by reading the real sample files first**, and write a short `docs/talend_format_notes.md` documenting what was actually found.

- A job lives under `process/` as `<JobName>_<version>.item` (e.g. `MyJob_0.1.item`) with a sibling `.properties` file (name, version, status, author).
- The `.item` is XML. The root is a process element containing:
  - `node` elements: one per component, with `componentName` (e.g. `tMap`, `tOracleInput`) and child `elementParameter` entries (`name`, `field`, `value`). The unique name is usually in a parameter called `UNIQUE_NAME`.
  - `metadata` blocks inside nodes: schema definitions with `column` entries (name, type, length, nullable, key).
  - `connection` elements: `source`, `target`, `connectorName` (e.g. `FLOW`, `LOOKUP`, `REJECT`, `SUBJOB_OK`, `COMPONENT_OK`, `RUN_IF`), and `label`.
  - `context` blocks with `contextParameter` entries (name, type, value) per context group (e.g. `Default`).
  - `subjob` elements grouping components.
- `tMap` stores its mapping logic inside a nested mapper-data structure with input tables, output tables, entries holding Java expressions, join model (inner/left), filters, and lookup settings.
- Database components follow a naming pattern: `t<DB>Input`, `t<DB>Output`, `t<DB>Row`, `t<DB>Connection` (Oracle, Postgresql, Mysql, MSSql, DB2, Snowflake…). Table name, query, host, schema are in `elementParameter` entries and are often **context variables** (`context.xxx`) or Java expressions (`"SELECT ... " + context.x`).

Edge cases the parser must handle gracefully: missing parameters, unknown components (record as `unsupported`, do not crash), encoded characters, very large jobs, Talend version differences.

---

## 5. Architecture

```
 .item files
     │
     ▼
 [Parser plugin]  ── talend/  (v1)        ┐  implements `BaseParser`
                     datastage/ (future)  ┘
     │
     ▼
 [Intermediate Model]  (pydantic, JSON-serializable, tool-independent)
     │
     ├──► [Lineage builder]  → networkx graph → Mermaid / Graphviz / JSON
     ├──► [Complexity scorer] → Markdown / HTML report
     └──► [dbt generator]     → models/*.sql + sources.yml + schema.yml
```

**Key design rules**
- Parsers only produce the intermediate model. Everything downstream is tool-agnostic.
- A `BaseParser` abstract interface (`can_parse(path)`, `parse(path) -> Job`) so other ETL tools can be added later.
- Pure functions where possible; no hidden global state; no network or DB access.
- Unknown / unsupported content is **preserved and flagged**, never dropped silently.

### 5.1 Intermediate model (initial draft, refine after reading samples)

```python
class Column:      name, type, length, nullable, is_key
class Schema:      columns: list[Column]
class Expression:  raw: str, referenced_columns: list[str], referenced_context: list[str], is_java: bool
class Component:   id, type, label, params: dict, schema_in, schema_out, supported: bool, warnings: list[str]
class Connection:  source, target, kind  # flow | lookup | reject | on_subjob_ok | on_component_ok | run_if
class TableRef:    database, schema, name, raw_expression, resolved: bool   # resolved=False if it depends on context at runtime
class Job:         name, version, path, contexts: dict, components, connections, reads: list[TableRef], writes: list[TableRef],
                   child_jobs: list[str], flags: list[str], complexity: Complexity
class Complexity:  score: int, level: easy|medium|hard, reasons: list[str]
```

Every `Job` serializes to `out/model/<job>.json` and a combined `out/model/_all.json`.

---

## 6. Components to support in v1

| Category | Components | Handling |
|---|---|---|
| Inputs | `tFileInputDelimited`, `tFileInputExcel`, `t<DB>Input` | Extract source (file or table/query) + schema |
| Outputs | `tFileOutputDelimited`, `t<DB>Output` | Extract target table + action (insert/update/upsert) |
| Raw SQL | `t<DB>Row`, query field of `t<DB>Input` | Extract SQL string, parse with `sqlglot` for tables read/written |
| Transform | `tMap`, `tFilterRow`, `tSortRow`, `tUniqRow`, `tAggregateRow`, `tJoin`, `tUnite` | Extract logic into model; translate where simple |
| Control | `tRunJob`, `tPrejob`, `tPostjob`, `tWarn`, `tDie` | Build orchestration graph |
| Custom code | `tJava`, `tJavaRow`, `tJavaFlex`, routines | **Detect, flag as manual, keep raw code in report** |
| Connections | `t<DB>Connection`, `t<DB>Commit`, `t<DB>Close` | Resolve shared connection settings |
| Anything else | — | Record as `supported=False` with a warning |

---

## 7. Features & CLI

```
tmt scan   <path>                 # parse all jobs under path → out/model/*.json
tmt graph  <path> --format mermaid|dot|json [--focus <job>]
tmt report <path> --format md|html          # complexity + inventory report
tmt dbt    <path> --target snowflake        # generate dbt skeleton for eligible jobs
tmt all    <path> -o out/                   # run everything
```

Common options: `--out`, `--context Default`, `--fail-on-unsupported`, `-v`.

### 7.1 Lineage rules
- Node types: **job** and **table**. Edges: `job → table` (writes), `table → job` (reads), `parent job → child job` (tRunJob).
- Compute: execution/migration order (topological sort), cycles (report, do not crash), orphan tables, most-depended-on tables, jobs with no inputs.
- When a table name depends on an unresolved context variable, mark the node `resolved=false` and show the raw expression.

### 7.2 Complexity scoring (transparent, configurable)
Score = weighted sum, defaults editable in `config.yaml`:

| Factor | Points |
|---|---|
| Each component | +1 |
| Each tMap join | +2 |
| Each tMap expression containing Java method calls | +3 |
| `tJava` / `tJavaRow` / `tJavaFlex` / custom routine | +10 each (forces "hard") |
| Unsupported component | +5 each |
| `tRunJob` children | +2 each |
| Unresolved context in table/SQL | +2 each |

Levels: **easy** ≤ 10, **medium** 11–25, **hard** > 25 (or any custom Java). Report must list the *reasons* for each score.

### 7.3 dbt generation (conservative)
Generate only when a job is **eligible**: one logical target, no custom Java, joins/filters/aggregations all translatable.
- `sources.yml` from input tables.
- `models/<job>.sql` with CTEs: one CTE per input → joins → filters → select with column mappings.
- Simple tMap expression translation table (e.g. `StringHandling.UPCASE(x)` → `upper(x)`, `row1.a + row1.b`, null checks, `TalendDate` basics). Anything not in the table becomes a `-- TODO(manual): <original expression>` comment and the job is downgraded to "needs review".
- `schema.yml` with columns and basic tests (`not_null` for non-nullable, `unique` for key columns).
- Every generated file starts with a header comment: source job name, tool version, generation timestamp, and a warning that it is a skeleton.
- Generated SQL must pass `sqlglot` parsing for the chosen dialect.

**Rule:** correctness over coverage. A flagged job is better than a wrong translation.

---

## 8. Repository layout

```
talend-migration-toolkit/
├── README.md                    # see section 11
├── LICENSE                      # MIT
├── pyproject.toml
├── config.example.yaml
├── src/tmt/
│   ├── __init__.py
│   ├── cli.py
│   ├── models.py                # pydantic intermediate model
│   ├── parsers/
│   │   ├── base.py              # BaseParser interface
│   │   └── talend/
│   │       ├── parser.py
│   │       ├── components.py    # per-component extractors (registry pattern)
│   │       ├── tmap.py
│   │       └── context.py
│   ├── lineage/builder.py
│   ├── scoring/complexity.py
│   ├── generators/
│   │   ├── dbt.py
│   │   ├── expressions.py       # Talend/Java → SQL expression map
│   │   └── templates/*.j2
│   └── reports/ (md.py, html.py, templates/)
├── samples/talend/              # synthetic .item files + README on how they were made
├── tests/
│   ├── unit/ ...
│   ├── integration/ ...
│   └── golden/                  # expected JSON / report / dbt outputs
├── docs/
│   ├── talend_format_notes.md
│   ├── architecture.md          # with diagram
│   └── adding_a_parser.md
└── .github/workflows/ci.yml
```

---

## 9. Milestones & acceptance criteria

| Version | Deliverable | Done when |
|---|---|---|
| **v0.1** | Talend parser + JSON model + Mermaid lineage | All 12 sample jobs parse with zero crashes; JSON matches golden files; `tmt scan` and `tmt graph` work |
| **v0.2** | Complexity report (MD + HTML), CLI polish | Scores reproducible; reasons listed; report renders cleanly; coverage ≥ 80% |
| **v0.3** | dbt skeleton generator | ≥ 6 sample jobs generate SQL that parses with `sqlglot`; ineligible jobs flagged with reasons |
| **v0.4** | Plugin interface hardened, docs, optional AI explanation hook | `docs/adding_a_parser.md` complete; a dummy second parser passes the contract tests |

Work **one milestone at a time**. Do not start v0.2 before v0.1's acceptance criteria are met.

---

## 10. Quality requirements

- **Testing:** unit tests per extractor; integration test per sample job; golden-file tests for JSON, graph, report and dbt output; fuzz-style tests with malformed/truncated XML (must not crash).
- **Typing & lint:** `mypy --strict` on `src/`, `ruff` clean.
- **Errors:** custom exception classes; per-job failures are collected and reported, not fatal for the whole run.
- **Logging:** `logging` module, verbosity via `-v`.
- **Determinism:** same input → byte-identical output (stable ordering), except timestamps, which must be injectable/overridable in tests.
- **Performance:** handle 1,000 jobs in under ~1 minute on a laptop; stream-parse large files if needed.
- **Security:** parse XML safely (disable external entities / DTD resolution in `lxml`); never execute any extracted code or expression.
- **Privacy:** no real customer data anywhere in the repo, tests, or docs.

---

## 11. README requirements (the public face)

1. One-sentence pitch + a hero diagram or screenshot of the lineage graph.
2. The problem it solves (2–3 sentences about migration discovery cost).
3. Quick start: install + run on `samples/` in ≤ 3 commands, with real output shown.
4. Supported Talend version(s) and supported components table.
5. Example outputs (JSON snippet, Mermaid graph, report excerpt, generated dbt model).
6. Limitations and design philosophy ("flags, never guesses").
7. Roadmap (DataStage plugin, AI explanations).
8. Contributing + license + note that samples are synthetic.

---

## 12. Instructions to the AI assistant

1. **Start by asking for / reading the sample `.item` files.** Do not invent the XML schema.
2. Write `docs/talend_format_notes.md` from what you actually observe before writing the parser.
3. Propose the final intermediate model (section 5.1 is a draft) and wait for the owner's approval if anything is ambiguous.
4. Implement milestone by milestone (section 9). After each: run tests, lint, type check; summarize what works, what is flagged, and what is unknown.
5. Prefer small, readable modules and a registry pattern for components so adding a component is a one-file change.
6. When uncertain about Talend behaviour, **flag it in the output and in a comment/ADR**, don't silently assume.
7. Keep commits small and meaningful (Conventional Commits style).
8. Do not add features outside the scope in section 2.

---

## 13. Open questions (owner to decide / answer)

- [ ] Talend Studio version used for samples?
- [ ] Which SQL dialect is the primary dbt target (Snowflake assumed)?
- [ ] Should the HTML report be a single self-contained file (assumed yes)?
- [ ] Should there be an optional LLM-assist command (`tmt explain`) in v0.4, and with which provider?
- [ ] License: MIT (assumed).
