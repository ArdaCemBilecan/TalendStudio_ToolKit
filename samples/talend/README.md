# Synthetic Talend sample jobs

All jobs here are **synthetic**. They contain no customer data: generic customers/orders tables,
placeholder hosts (`*.example.local`), and no real credentials. Never commit real customer jobs.

- Talend Studio version: **8.0.1** (Talend Big Data Platform, Data Integration "Standard" jobs)
- Folder layout mirrors a Studio project: `process/<Job>_0.1.item` + `<Job>_0.1.properties`

## Two kinds of samples

| Kind | Jobs | Origin |
|---|---|---|
| **Generated** | `job_simple_copy`, `job_filter_map`, `job_join_lookup`, `job_aggregate`, `job_context_driven`, `job_union_uniq`, `job_with_java`, `job_orchestrator`, `job_multi_output`, `job_sql_template`, `job_join_aggregate`, `job_union_filter` | `tools/generate_samples.py`. Every component node is cloned from a node Studio itself wrote (`tools/templates/`); only scenario values change. Re-run to regenerate (output is byte-identical). |
| **Generated, opens in Studio, not re-saved by Studio** | `job_delimited_copy`, `job_sort_row`, `job_with_javarow`, `job_runif`, `job_join_aggregate`, `job_union_filter` | Also generated, but these components (`tFileInputDelimited`, `tFileOutputDelimited`, `tSortRow`, `tJavaRow`, `tPrejob`, `tPostjob`, `RUN_IF` triggers) were **not** cloned from a Studio-written file: parameter names come from knowledge of Talend. All of them open without errors in Studio 8.0.1 (confirmed by the repository owner); Studio has not rewritten them, so parameter names are still ours, not Studio's. To make one ground truth, nudge a component, save (Studio only rewrites a dirty editor) and replace our file. |
| **Studio-written** | `job_prejob_postjob` | Corrected and saved by Studio 8.0.1 (trigger had to be `OnComponentOk`); personal paths, author id and encrypted passwords scrubbed. |

> **Status of generated jobs:** structurally validated (`tools/validate_samples.py`), but each must
> still be opened once in Studio. Anything Studio rewrites on first save is the ground truth and
> should replace the generated file.

## Scenario catalogue

| Job | What it exercises |
|---|---|
| `job_simple_copy` | `tFileInputExcel` → `tJDBCOutput` straight load |
| `job_filter_map` | `tJDBCInput` → `tMap` (output filter, rename, `StringHandling`, null check, Java method call, `TalendDate`) → `tJDBCOutput` |
| `job_join_lookup` | 1 main + 2 lookups in one `tMap`: inner join and left join (lookup chained on another lookup) |
| `job_aggregate` | `tAggregateRow` (group by + sum/count/max) |
| `job_context_driven` | Context groups `Default`/`Prod`; JDBC URL, query and target table built from `context.*` (unresolved at static-analysis time) |
| `job_union_uniq` | `tUnite` (two inputs) → `tUniqRow` |
| `job_with_java` | `tJava` (must be flagged manual) + `OnSubjobOk` + `tFixedFlowInput` → `tLogRow` |
| `job_orchestrator` | three `tRunJob` chained with `OnSubjobOk` |
| `job_multi_output` | one `tMap`, two filtered outputs → two tables |
| `job_delimited_copy` | `tFileInputDelimited` → `tFileOutputDelimited` (unverified) |
| `job_sort_row` | `tSortRow` with two criteria (unverified) |
| `job_with_javarow` | `tJavaRow` (must be flagged manual) |
| `job_prejob_postjob` | `tPrejob` / `tPostjob` each followed by `tJava` via `OnComponentOk` |
| `job_runif` | two `RUN_IF` triggers with Java conditions (unverified) |
| `job_join_aggregate` | `tMap` (inner join + filter) feeding a `tAggregateRow` (sum/count) |
| `job_union_filter` | `tUnite` feeding a `tMap` with a filter and expressions |
| `job_sql_template` | three `tOracleRow` with hand-written DML (INSERT…SELECT with join, UPDATE, TRUNCATE), context-parameterised |

## Optional extras (build by hand in Studio)

These would strengthen the test set; not required for v0.1:

- `job_tmap_reject`: `tMap` with an output marked **Catch output reject** and a lookup with
  **Catch lookup inner join reject**, each wired to a `tLogRow`.
- `job_oracle_io`: `tOracleInput` → `tOracleOutput` (built-in connection, any table) to confirm the
  `t<DB>Input/Output` naming pattern against a native (non-JDBC) component.
- `job_filter_row`: `tFixedFlowInput` → `tFilterRow` → `tLogRow`.

## Regenerating

```bash
python tools/generate_samples.py --out samples/talend/process
python tools/validate_samples.py samples/talend/process
```

`tools/extract_templates.py` re-creates `tools/templates/` from a local Studio workspace
(scrubbing labels, passwords, schemas and mapper data). It is only needed when upgrading the
Studio version.
