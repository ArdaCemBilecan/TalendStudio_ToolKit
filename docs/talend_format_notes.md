# Talend `.item` format notes

Observed on **Talend Studio 8.0.1** (Big Data Platform, Data Integration "Standard" jobs).
Everything below was read from files Studio wrote, or confirmed by opening generated files in
Studio. Items marked **UNVERIFIED** are not yet backed by a real Studio file.

## 1. Files

| File | Content |
|---|---|
| `process/<Job>_<ver>.item` | The job, XMI/EMF XML. Root `talendfile:ProcessType`. |
| `process/<Job>_<ver>.properties` | Job identity: `label`, `id`, `version`, author, created/modified product versions. |
| `context/`, `metadata/` | Repository contexts and shared connections/schemas, referenced by id from jobs. |
| `process/**/` subfolders | Job folders. The folder is recorded in `ItemState@path` of the `.properties`. |

- **The job name is not inside the `.item`.** It only lives in `.properties` (`label`) and the file name.
- The `.properties` `id` attribute (not `xmi:id`) is the job's repository id; `tRunJob` references it.
- `item_key` (an integrity hash Studio writes into `.properties`) is **not required**: generated
  files without it import and open fine.

## 2. `.item` skeleton

```
talendfile:ProcessType (defaultContext, jobType="Standard")
  context*            name="Default" ...; children contextParameter (name, type, value, prompt, ...)
  parameters          job-level elementParameter list (log settings, implicit context, ...)
  node*               componentName, componentVersion, posX, posY
    elementParameter* field, name, value[, show="false"]; TABLE params hold elementValue(elementRef, value, id)
    metadata*         connector=FLOW|REJECT|UNIQUE..., name; column(name,type,length,precision,key,nullable,pattern)
    nodeData          only tMap (xsi:type="TalendMapper:MapperData")
  connection*         connectorName, label, lineStyle, metaname, source, target
  subjob*             UNIQUE_NAME of the subjob's first component
```

- A node's identity is the `UNIQUE_NAME` elementParameter (`tMap_1`, `tJDBCInput_2`, ...), not an attribute.
- Parameters with `show="false"` are technical; they are present on most real nodes.
- Namespace prefix `TalendMapper` is used inside an attribute value (`xsi:type`), so a namespace-aware
  serializer may drop its declaration. Parsers are unaffected; generators must re-add it.
- Column types use Talend ids: `id_String`, `id_Integer`, `id_Double`, `id_BigDecimal`, `id_Boolean`,
  `id_Date` (date `pattern` stored as a Java string literal, e.g. `"dd-MM-yyyy"`).

## 3. Values are Java expressions

`elementParameter@value` is the text of a Java expression, not a plain value: string literals keep
their quotes (`"STG.CUSTOMERS"`), context use is `context.name`, and concatenation is Java
(`"SELECT ... FROM " + context.src_schema + "." + context.src_table`). A value is **resolved** only
if it is a single string literal; anything referencing `context.*` or concatenating is not.

## 4. Connections

| `connectorName` | `lineStyle` | Meaning |
|---|---|---|
| `FLOW` or `MAIN` | `0` | main row flow |
| `FLOW` or `MAIN` | `8` | **lookup** into a tMap (shown dashed, "Lookup" in Studio) |
| `FLOW` or `MAIN` | `10` | merge input (`tUnite`) |
| `UNIQUE` | `0` | unique output of `tUniqRow` |
| `SUBJOB_OK` | `1` | `OnSubjobOk` trigger (`label="OnSubjobOk"`) |
| `COMPONENT_OK` | `3` | `OnComponentOk` trigger (`label="OnComponentOk"`); **Studio-written** |
| `RUN_IF` | `1` | `Run if` trigger; Java boolean in nested `CONDITION` param, name `If1`.. (loads in Studio, rendered as `If1 (order:1)`; written by our generator, not yet re-saved by Studio) |

- `MAIN` is written by Component Kit ("tacokit") components such as `JDBCInput`/`JDBCOutput`;
  classic components write `FLOW`. Treat them identically.
- Connection name is the nested `UNIQUE_NAME` parameter (`row1`, ...). `label` usually equals it.
- For a tMap, `source=tMap_1` and `metaname`/`label` equal the **output table name** (`out_active`).
- A `tPrejob`/`tPostjob` can only start a subjob through **OnComponentOk**, not OnSubjobOk
  (Studio flags the OnSubjobOk variant with a warning icon). Trigger connections never merge subjobs:
  every start component still gets its own `<subjob>`.
- Trigger kinds `SUBJOB_ERROR`, `COMPONENT_ERROR`: **UNVERIFIED**.
- Studio tolerates nodes that lack technical parameters and **adds them on save** (`COMPONENT_NAME`,
  `VERSION`, `FAMILY`, `START`, `STARTABLE`, `SUBTREE_START`, `END_OF_FLOW`, `HELP`, `IREPORT_PATH`,
  `JAVA_LIBRARY_PATH`; subjobs get `SHOW_SUBJOB_TITLE`). A parser must therefore not depend on them being present.

## 5. Components

Component names are in `node@componentName`. Observed in real files:
`tFileInputExcel`, `tMap`, `tLogRow`, `tRunJob`, `tOracleRow`, `tDBRow`, `tJava`, `tFixedFlowInput`,
`tAggregateRow`, `tUniqRow`, `tUnite`, `tLogicGate`, `tSleep`, `JDBCInput`, `JDBCOutput`, `tHDFSConnection`.

- **Note the naming:** Component Kit JDBC components are `JDBCInput`/`JDBCOutput` (no `t` prefix),
  with unique names `tJDBCInput_1`. The `t<DB>Input` pattern from the spec (e.g. `tOracleInput`)
  has **not** been observed yet; only `tOracleRow` has.
- `tDBRow` (unique-name prefix) can appear with `componentName="tOracleRow"`: the unique name is a
  user-visible id and must not be used to infer the component type.

### JDBCInput / JDBCOutput (Component Kit)
Parameters are dotted `configuration.*` names.
- Input: `configuration.dataSet.sqlQuery` (query), `...dataStore.jdbcUrl`, `...dataStore.userId`.
- Output: `configuration.dataSet.tableName` (target table expression), `configuration.dataAction`
  (`INSERT` observed), `configuration.clearData`.
- A job exported from DataStage migration tooling may carry placeholder values such as
  `"$Name.READER.Query"`; treat any value starting with `$` inside quotes as unresolved.

### tOracleRow
Query in `QUERY` (Java expression, often concatenated with `context.*`). Connection either
`PROPERTY:PROPERTY_TYPE=REPOSITORY` (+ `REPOSITORY_PROPERTY_TYPE` id, details in `metadata/connections`)
or `BUILT_IN` with `LOCAL_SERVICE_NAME`/`SCHEMA_DB`/`USER`/`PASS` set inline.
Has `START` technical parameter (true when it has no incoming trigger).

### tFileInputExcel
`FILENAME`, `HEADER`, schema via `SCHEMA:SCHEMA_TYPE` (`REPOSITORY` -> `metadata/fileExcel`, or `BUILT_IN`).
Has a `REJECT` metadata block (schema + `errorCode`, `errorMessage`).

### tAggregateRow / tUniqRow / tUnite
- `tAggregateRow`: `GROUPBYS` table (`INPUT_COLUMN`, `OUTPUT_COLUMN`), `OPERATIONS` table
  (`OUTPUT_COLUMN`, `FUNCTION`, `INPUT_COLUMN`); functions observed: `sum`, `count`, `max`.
- `tUniqRow`: `UNIQUE_KEY` table with one triple per column (`SCHEMA_COLUMN`, `KEY_ATTRIBUTE`,
  `CASE_SENSITIVE`); output metadata connector is `UNIQUE`.
- `tUnite`: no parameters of interest; inputs arrive with `lineStyle="10"`.

### tRunJob
Child name in `PROCESS`; child id in `PROCESS:PROCESS_TYPE_PROCESS` as `<ProjectLabel>:<propertyId>`.
Child version in `PROCESS:PROCESS_TYPE_VERSION` (`Latest`). Context passing in `CONTEXTPARAMS` table.

### tJava / tJavaRow
Code in `CODE`, imports in `IMPORT`. `tJavaRow` has the same two params plus an output schema
(`metadata FLOW`); it reads `input_row.*` / writes `output_row.*`.

### tPrejob / tPostjob (Studio-written, componentVersion 0.102)
No functional parameters, only technical ones. `FAMILY=Orchestration`, `START=true`. `tPostjob` carries
an empty `metadata FLOW`, `tPrejob` carries none.

### tFileInputDelimited / tFileOutputDelimited / tSortRow
Opened in Studio 8.0.1 without errors; the files are still the generated ones (Studio has not rewritten them), so the
parameter names below are not yet confirmed by a Studio-written file:
`FILENAME`, `FIELDSEPARATOR`, `ROWSEPARATOR`, `HEADER`, `FOOTER`, `ENCODING` (input; `INCLUDEHEADER`,
`APPEND` on output); `tSortRow.CRITERIA` table of (`COLNAME`, `SORT`=`num|alpha`, `ORDER`=`asc|desc`).
`tFilterRow`, `tJavaFlex`: **UNVERIFIED**.

## 6. tMap (`nodeData`, `TalendMapper:MapperData`)

```
nodeData
  varTables name="Var"
  outputTables name=<out>  [expressionFilter, activateExpressionFilter="true"]
    mapperTableEntries name expression type nullable
  inputTables name=<connection label>  matchingMode lookupMode [innerJoin="true"]
    mapperTableEntries name type [expression = join key]
```

- Input table `name` = the incoming connection **label** (`row1`), and expressions reference it
  (`row1.customer_id`). The first input is the main flow; later ones are lookups.
- Join type: lookup with `innerJoin="true"` is an **inner** join; absence means **left outer**.
- Join key = `expression` on a lookup input's entry (`row1.customer_id`); a lookup may key on
  another lookup (`row2.country_code`).
- A filter is `expressionFilter` on an output table (a Java boolean expression).
- Each output has a matching `metadata connector="FLOW" name=<out>` on the node with its schema.
- Reject outputs (`Catch output reject`, `Catch lookup inner join reject`): **UNVERIFIED**.

## 7. Contexts

`<context name="Default">` repeated per context group, each listing `contextParameter`
(`name`, `type`, `value`; passwords as `enc:system.encryption.key.v1:...`). Repository contexts
carry `repositoryContextId`. A job without parameters has a single self-closing `<context>`.

## 8. Parser consequences (to carry into the design)

1. Identify components by `componentName`, never by `UNIQUE_NAME`.
2. Normalize `FLOW`/`MAIN` and use `lineStyle` to tell main / lookup / merge apart.
3. Parse parameter values as Java expressions; classify as resolved / context-dependent / unresolved.
4. Resolve repository-referenced connections and schemas when the referenced item exists, otherwise
   record `resolved=false` with the reference id.
5. Support both `t<DB>Input/Output/Row` and Component Kit `JDBCInput/JDBCOutput`.
6. Job name and version come from `.properties`; fall back to the file name when it is missing.

## 9. Observed in a real workspace (three projects, 36 jobs)

These were not visible in the synthetic samples.

- **A project copied from another repeats every job name and every Studio id.** Studio ids are unique only
  *within* a project. A job is therefore identified by `(project, id)`, and by project plus folder plus name for
  humans. The project's technical label is in the nearest `talend.project` (`technicalLabel`).
- **`tRunJob` references are project-qualified:** `PROCESS:PROCESS_TYPE_PROCESS = "<Project>:<id>"`. In a PROD
  project cloned from DEV the reference still says `DEV:...`, so PROD's orchestrator calls DEV's child jobs.
- **Jobs produced by a migration tool (DataStage to Talend) carry placeholders instead of real values:** Component Kit
  `JDBCInput` has `configuration.dataSet.sqlQuery = "$Stage_Link.READER.Query"` and
  `...dataStore.jdbcUrl`/`DBNAME` placeholders; the real SQL lives outside the job file. `JDBCOutput` keeps Studio's
  default label `"Output for table"` as `configuration.dataSet.tableName`. Neither is a table or a query.
- Such a job can be large: 72 components (23 `JDBCInput`, 38 `tMap`, 3 `tAggregateRow`, 6 `tUnite`, 1 `tUniqRow`),
  14 lookup joins, parsed in well under a second.
- Component names not handled in v1 that appeared: `tMapPython`, `tLogicGate`, `tSleep`, `tHDFSConnection`
  (recorded as unsupported).

## 10. Metadata connections (which database a component uses)

* A reusable connection is a repository item: `<project>/metadata/connections/<Name>_0.1.item` with a
  `<Name>_0.1.properties` next to it. The **name is the `label`** of that `.properties`; its `id` is what jobs refer to.
* A component that uses it has `PROPERTY:PROPERTY_TYPE=REPOSITORY` and `PROPERTY:REPOSITORY_PROPERTY_TYPE=<that id>`.
  Ids repeat between a project and its clone, so an id that is missing in the job's own project is looked up in the
  sibling projects of the workspace.
* Components with `USE_EXISTING_CONNECTION=true` share a `t<DB>Connection` component of the same job: they take
  its connection. The connection component itself is not counted as a use.
* When the repository link is gone, Studio still leaves context variables named after the connection
  (`context.ODS_DB_Host`, `context.ODS_DB_Sid`...). The name is then **inferred** from them and marked `inferred`.
* Otherwise: built-in settings (host/database shown, never user or password) or a placeholder that cannot be resolved.
