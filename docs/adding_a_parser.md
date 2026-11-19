# Adding a parser for another ETL tool

A parser reads one tool's job files and returns the tool-independent `Job` model. Lineage, scoring, the explorer and
the migration order then work unchanged. You do **not** edit `tmt` itself: a parser is its own package,
discovered through an entry point.

The smallest complete example is [tests/dummy_parser.py](../tests/dummy_parser.py) (about 60 lines, passes the
full contract). The skeleton below is its essence.

## 1. The interface

```python
from pathlib import Path

from tmt.errors import ParseError
from tmt.models import Job
from tmt.parsers.base import BaseParser


class MyToolParser(BaseParser):
    name = "mytool"  # unique; becomes Job.source_tool

    def can_parse(self, path: Path) -> bool:
        # Cheap and total: never raise, never read the whole file. Extension plus a few bytes is enough.
        return path.is_file() and path.suffix == ".mytool"

    def parse(self, path: Path, *, context: str | None = None, base: Path | None = None) -> Job:
        try:
            raw = path.read_text(encoding="utf-8")
            ...  # build components, connections, table references
        except (OSError, ValueError) as exc:
            raise ParseError(path.name, f"cannot read job: {exc}") from exc
        return Job(name="...", version="...", path=path.name, source_tool=self.name)
```

| Rule | Why |
|---|---|
| `can_parse` never raises and is cheap | `scan` calls it for **every** file under the folder |
| `parse` raises only `ParseError` | anything else is reported as an unexpected error; `ParseError` is the clean per-job failure |
| `Job.path` is relative to `base` (use `/`) | output must be identical on every machine |
| Sort every list and dict you produce | output must be byte-identical across runs |
| Never copy passwords or tokens | the model is written to JSON and reports; mark secrets `is_secret=True`, value `None` |
| Keep unknown content, flag it | `Component(supported=False)` plus an `Issue`; never drop it silently |
| Don't execute anything you read | static analysis only |

## 2. What to fill in the model

You do not have to fill everything; downstream features use what is there.

| You provide | Enables |
|---|---|
| `Job.name`, `version`, `path`, `source_tool` | everything (required) |
| `components` with `category` (`input`/`output`/`sql`/`transform`/...) and a unique `id` | counts, scoring |
| `connections` (`ConnKind.FLOW`, `LOOKUP`, triggers, ...) | subjobs, execution order |
| `subjobs`, `Component.subjob_id` | `validate_job` |
| `reads` / `writes` (`TableRef`) | **lineage and migration order** (the main payoff) |
| `child_jobs` | parent/child edges in the graph |
| `Component.tmap`, `aggregate`, `sql`, `code` | richer scoring |
| `Issue` records | flags in the explorer |

`TableRef.resolution` tells the rest of the tool how much to trust a name: `RESOLVED` for a literal,
`CONTEXT_DEPENDENT` when a variable is involved, `UNRESOLVED` otherwise. If you are unsure, say so.

## 3. Register it

In your package's `pyproject.toml`:

```toml
[project]
name = "tmt-mytool"
dependencies = ["talend-migration-toolkit>=0.4"]

[project.entry-points."tmt.parsers"]
mytool = "tmt_mytool:MyToolParser"
```

After `pip install`, check it is picked up:

```console
$ tmt parsers
parsers
 name     origin    class
 talend   built-in  tmt.parsers.talend.parser.TalendParser
 mytool   plugin    tmt_mytool.MyToolParser
```

`tmt scan <folder>` now handles Talend and MyTool files in one run, and the lineage graph spans both. Use
`--parser mytool` to restrict a run. If your parser fails to import, has no `name`, or reuses an existing name,
`tmt` logs a warning and carries on without it.

If your constructor takes a `dialect` argument, it receives the SQL dialect from `config.yaml`; otherwise it is
called with no arguments.

## 4. Prove it with the contract tests

`tmt.testing.contract.ParserContract` is a ready-made pytest class. Subclass it and provide four fixtures:

```python
import json
from pathlib import Path

import pytest

from tmt.testing.contract import ParserContract
from tmt_mytool import MyToolParser


class TestMyToolParser(ParserContract):
    secret_markers = ("hunter2",)  # strings from your fixtures that must never reach the output

    @pytest.fixture
    def parser(self):
        return MyToolParser()

    @pytest.fixture
    def valid_files(self, tmp_path: Path) -> list[Path]:
        p = tmp_path / "a.mytool"
        p.write_text(json.dumps({"name": "load_x"}), encoding="utf-8")
        return [p]

    @pytest.fixture
    def broken_file(self, tmp_path: Path) -> Path:  # recognised, but unusable; same folder as valid_files
        p = tmp_path / "b.mytool"
        p.write_text("not valid", encoding="utf-8")
        return p

    @pytest.fixture
    def foreign_file(self, tmp_path: Path) -> Path:  # belongs to some other tool
        p = tmp_path / "c.txt"
        p.write_text("x", encoding="utf-8")
        return p
```

The contract checks, for every parser:

* it has a `name`; `can_parse` accepts valid files, rejects foreign ones and never raises for missing paths;
* `parse` returns a structurally valid `Job` (`validate_job`: unique component ids, connections between existing
  components, every component in exactly one subjob, references to existing components);
* `Job.path` is relative to `base`; output is deterministic; the job survives a JSON round trip;
* secrets are not serialised; a broken file raises `ParseError` and nothing else;
* `scan` collects the failure and keeps going, and **the rest of the pipeline works on your model**
  (graph, scoring, explorer).

`validate_job(job)` is also a plain function that returns a list of problems, handy in your own tests.

## 5. Checklist

- [ ] `name` is unique and stable (it ends up in every `Job`)
- [ ] `can_parse` is cheap; `parse` raises only `ParseError`
- [ ] lists and dicts are sorted; paths are relative to `base`
- [ ] no secrets in the model; unknown content is flagged, not dropped
- [ ] contract tests pass; entry point registered; `tmt parsers` shows you as `plugin`
- [ ] synthetic sample jobs only (never commit a customer's jobs)

## Why a second parser is a good test of the design

`tests/contract/` runs the **same** contract against the Talend parser and `DummyParser`. While building it,
the contract caught a real leak: `scan` discovered files by the Talend extension (`*.item`), so no other tool's
files were ever seen. Discovery is now the parsers' job (`can_parse`), which is why the rule "cheap and total"
above matters.
