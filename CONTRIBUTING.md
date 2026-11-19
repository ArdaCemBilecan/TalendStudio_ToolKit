# Contributing

Thanks for helping. A few rules keep the project trustworthy.

## Ground rules

1. **No real customer data, ever.** Sample jobs must be synthetic (see `samples/talend/README.md`). Do not paste
   real job files, hostnames, schemas or credentials into issues or PRs either.
2. **Flags, never guesses.** If `tmt` cannot be sure, it records a flag. A flagged job is better than a wrong
   translation. This applies most strongly to anything that names a database connection or a table.
3. **Don't invent file formats.** Talend behaviour must be backed by a file Studio wrote. Mark anything else
   `UNVERIFIED` in `docs/talend_format_notes.md`.

## Setup and checks

```bash
pip install -e ".[dev]"
ruff check src tests tools
mypy                       # strict
pytest --cov=tmt           # must stay above 80%
```

Golden files (`tests/golden/`) pin the exact output. If your change legitimately alters it:

```bash
UPDATE_GOLDEN=1 pytest tests/integration/test_golden.py
```

then **read the diff** before committing it.

## What a good change looks like

* Small and focused, with a test. Bug fixes start with a failing test.
* Output stays deterministic (sorted, no timestamps).
* New Talend component: one decorated function in `src/tmt/parsers/talend/components.py`, a unit test, and a
  sample job in `tools/generate_samples.py` only if the component's parameters were observed in a real Studio file.
* Commit messages follow Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`).

## Supporting another ETL tool

Write a parser plugin and pass the contract tests: [docs/adding_a_parser.md](docs/adding_a_parser.md).

## Reporting a problem

Include the `tmt --version`, the command, and the *shape* of the job (component types and connections), not its
contents. For a parse failure, `out/model/_errors.json` has the reason.
