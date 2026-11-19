"""Command line interface.

Design rules (CLI UX): results go to stdout and diagnostics to stderr so output can be piped; every error says
what went wrong *and* what to try; status is always written in words, never colour alone (``NO_COLOR`` is
honoured); exit codes are stable and documented: 0 ok, 1 some jobs could not be parsed, 2 usage or
configuration error (or ``--fail-on-unsupported``).
"""

from __future__ import annotations

import functools
import logging
from collections import Counter
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, NoReturn, ParamSpec, TypeVar

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from tmt import __version__
from tmt.config import Config, load_config
from tmt.errors import TmtError
from tmt.lineage import builder
from tmt.lineage.explore import explore
from tmt.lineage.svg import to_svg
from tmt.models import IssueCode
from tmt.parsers.base import BaseParser
from tmt.parsers.registry import load_parsers, select
from tmt.scan import ScanResult, dumps, scan, write_models, write_text
from tmt.text import plural

EPILOG = """Examples:

\b
  tmt ui
  tmt graph my_project --master job_master_daily --format svg --out out
  tmt scan my_project --out out

Exit codes: 0 success; 1 some jobs could not be parsed (the rest was still processed); \
2 usage or configuration error, or --fail-on-unsupported found an unsupported component."""
app = typer.Typer(add_completion=False, no_args_is_help=True, epilog=EPILOG, rich_markup_mode=None,
                  help="Dependency explorer for ETL jobs (Talend today): what a master job starts, reads and writes.")
console = Console(highlight=False)
err = Console(stderr=True, highlight=False)

PathArg = Annotated[Path, typer.Argument(exists=True, help="A job file, or a folder searched recursively for jobs.")]
OutOpt = Annotated[Path | None, typer.Option("--out", "-o", help="Output directory (default: ./out).")]
ContextOpt = Annotated[str | None, typer.Option("--context", help="Context group used to fold context.* values.")]
VerboseOpt = Annotated[int, typer.Option("--verbose", "-v", count=True, help="-v shows progress, -vv debug detail.")]
QuietOpt = Annotated[bool, typer.Option("--quiet", "-q", help="Print only errors (results are still written).")]
ConfigOpt = Annotated[
    Path | None, typer.Option("--config", help="config.yaml (default: ./config.yaml if present).", dir_okay=False)
]
ParserOpt = Annotated[
    str | None, typer.Option("--parser", help="Only use this parser (see 'tmt parsers'); default: all installed.")
]
FailOpt = Annotated[bool, typer.Option("--fail-on-unsupported", help="Exit 2 if an unsupported component is found.")]


class GraphFormat(StrEnum):
    mermaid = "mermaid"
    dot = "dot"
    json = "json"
    svg = "svg"


P = ParamSpec("P")
R = TypeVar("R")


def command(name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Register a CLI command. File-system problems become a clean message instead of a stack trace."""

    def deco(fn: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return fn(*args, **kwargs)
            except OSError as exc:
                where = f" ({exc.filename})" if exc.filename else ""
                _fail(f"cannot read or write files: {exc.strerror or exc}{where}",
                      "check that --out is a writable folder path (not an existing file) and short enough for "
                      "your OS")

        app.command(name)(wrapper)
        return fn

    return deco


# ------------------------------------------------------------------ output helpers
def _fail(message: str, hint: str | None = None, code: int = 2) -> NoReturn:
    """Print ``error: ...`` (+ what to try) on stderr and exit. Never a stack trace for a user mistake."""
    err.print(Text("error: ", style="bold red") + Text(message))
    if hint:
        err.print(Text("hint:  ", style="bold") + Text(hint))
    raise typer.Exit(code)


def _say(text: str, quiet: bool = False) -> None:
    if not quiet:
        console.print(Text(text), soft_wrap=True)  # soft_wrap: never break a path across lines


def _setup_logging(verbose: int) -> None:
    level = logging.WARNING if verbose == 0 else logging.INFO if verbose == 1 else logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")


def _version(value: bool) -> None:
    if value:
        typer.echo(f"tmt {__version__}")
        raise typer.Exit()


@app.callback()
def main(version: Annotated[bool, typer.Option("--version", callback=_version, is_eager=True,
                                               help="Show the version and exit.")] = False) -> None:
    """Static analysis of ETL jobs."""


def _config(path: Path | None) -> Config:
    try:
        return load_config(path)
    except TmtError as exc:
        _fail(str(exc), "check the file against config.example.yaml")


def _parsers(cfg: Config, name: str | None) -> list[BaseParser]:
    try:
        return select(load_parsers(cfg.dialect), name)
    except KeyError as known:
        _fail(f"unknown parser {name!r}", f"installed parsers: {known.args[0]} (run 'tmt parsers')")


def _load(path: Path, context: str | None, config: Path | None, parser: str | None, verbose: int
          ) -> tuple[Config, ScanResult]:
    _setup_logging(verbose)
    cfg = _config(config)
    parsers = _parsers(cfg, parser)
    result = scan(path, context=context, parsers=parsers, config=cfg)
    if not result.jobs and not result.failures:
        _fail(f"no job files found under {path}",
              "pass a project folder (Talend: it contains process/*.item) or a single job file; "
              "'tmt parsers' lists what is recognised")
    return cfg, result


def _finish(result: ScanResult, fail_on_unsupported: bool = False) -> None:
    for f in result.failures:
        err.print(Text("FAILED ", style="bold red") + Text(f"{f.path}: {f.error}"))
    if result.failures:
        raise typer.Exit(1)
    if fail_on_unsupported and _unsupported(result):
        _fail("unsupported components found", "see the 'unsupported_component' flags in the report")


def _unsupported(result: ScanResult) -> bool:
    return any(i.code is IssueCode.UNSUPPORTED_COMPONENT for j in result.jobs for i in j.issues)


def _summary(result: ScanResult, quiet: bool) -> None:
    if quiet:
        return
    codes: Counter[str] = Counter(i.code.value for j in result.jobs for i in j.issues)
    t = Table(title=f"{plural(len(result.jobs), 'job')} parsed, {len(result.failures)} failed")
    longest = max((len(j.key) for j in result.jobs), default=3)
    job_width = max(16, min(longest, console.width - 60))
    # Short headers keep the table inside 80 columns; the job name is never broken across lines.
    for col in ("job", "level", "score", "comps", "reads", "writes", "kids", "flags"):
        if col == "job":
            t.add_column(col, no_wrap=True, min_width=job_width, max_width=job_width, overflow="ellipsis")
        else:
            t.add_column(col, justify="left" if col == "level" else "right", no_wrap=True, min_width=len(col))
    for j in result.jobs:
        c = j.complexity
        t.add_row(j.key, c.level if c else "-", str(c.score) if c else "-", str(len(j.components)),
                  str(len(j.reads)), str(len(j.writes)), str(len(j.child_jobs)), str(len(j.issues)))
    console.print(t)
    if codes:
        _say("issues: " + ", ".join(f"{k}={v}" for k, v in sorted(codes.items())))


def _print_workflow(s: dict[str, Any]) -> None:
    """The numbers of one master job's workflow, on stderr so the graph on stdout stays pipeable."""
    err.print(Text(f"{s['master']}: {plural(s['jobs'], 'job')}, {plural(s['sources'], 'source')}, "
                   f"{plural(s['targets'], 'target')}"))
    conns = ", ".join(f"{c['uses']}x {c['name']}" for c in s["connections"]) or "none"
    err.print(Text(f"database connections ({s['distinct_connections']} distinct): {conns}"))


def _resolve_job(result: ScanResult, wanted: str) -> str:
    """A job key, or a plain name when exactly one job has it. Otherwise say which keys exist."""
    keys = [j.key for j in result.jobs]
    if wanted in keys:
        return wanted
    named = [j.key for j in result.jobs if j.name == wanted]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        _fail(f"job name {wanted!r} is ambiguous", "use one of: " + ", ".join(sorted(named)))
    _fail(f"job {wanted!r} not found", "known jobs: " + ", ".join(keys))


# ------------------------------------------------------------------ commands
@command("scan")
def scan_cmd(path: PathArg, out: OutOpt = None, context: ContextOpt = None, config: ConfigOpt = None,
             parser: ParserOpt = None, verbose: VerboseOpt = 0, quiet: QuietOpt = False,
             fail_on_unsupported: FailOpt = False) -> None:
    """Parse jobs and write JSON models.

    Writes out/model/<job>.json for every job under PATH, plus _all.json.
    """
    out = out or Path("out")
    _, result = _load(path, context, config, parser, verbose)
    written = write_models(result, out)
    _summary(result, quiet)
    _say(f"wrote {plural(len(written), 'file')} to {out / 'model'}", quiet)
    _finish(result, fail_on_unsupported)


@command("graph")
def graph_cmd(
    path: PathArg,
    fmt: Annotated[GraphFormat, typer.Option("--format", "-f", help="Output format.")] = GraphFormat.mermaid,
    focus: Annotated[str | None, typer.Option("--focus", help="Only this job and its up/downstream.")] = None,
    master: Annotated[str | None, typer.Option(
        "--master", help="Only this master job and every job it starts, with source/target/connection counts.",
    )] = None,
    out: OutOpt = None,
    context: ContextOpt = None,
    config: ConfigOpt = None,
    parser: ParserOpt = None,
    verbose: VerboseOpt = 0,
    quiet: QuietOpt = False,
    fail_on_unsupported: FailOpt = False,
) -> None:
    """Lineage graph and migration order.

    Prints the graph to stdout (or writes it with --out); the suggested order goes to stderr.
    """
    _, result = _load(path, context, config, parser, verbose)
    if master and focus:
        _fail("use either --master or --focus, not both", "--master shows a workflow; --focus shows neighbours")
    if master:
        ex = explore(result.jobs, _resolve_job(result, master))
        g = ex.graph
        _print_workflow(ex.summary)
    else:
        g = builder.build_graph(result.jobs)
    if focus:
        g = builder.focus(g, _resolve_job(result, focus))
    analysis = builder.analyze(g)
    if fmt is GraphFormat.mermaid:
        text, ext = builder.to_mermaid(g), "mmd"
    elif fmt is GraphFormat.dot:
        text, ext = builder.to_dot(g), "dot"
    elif fmt is GraphFormat.svg:
        text, ext = to_svg(g), "svg"
    else:
        text, ext = dumps(builder.to_json(g, analysis)), "json"
    if out is None:
        typer.echo(text, nl=False)
    else:
        target = out / "graph" / f"lineage.{ext}"
        write_text(target, text)
        _say(f"wrote {target}", quiet)
    if fmt is not GraphFormat.json and not quiet:
        err.print(Text("migration order: " + (" -> ".join(analysis.migration_order) or "(none)")))
        for cyc in analysis.cycles:
            err.print(Text("cycle: ", style="bold yellow") + Text(" <-> ".join(cyc)))
    _finish(result, fail_on_unsupported)


@command("ui")
def ui_cmd(
    workspace: Annotated[Path | None, typer.Argument(
        help="Talend workspace to open straight away (default: the last one used, or the one found for this user).",
    )] = None,
    port: Annotated[int | None, typer.Option(
        "--port", min=0, max=65535, help="Port to listen on (default: 8765, or a free one if that is taken).",
    )] = None,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Print the address but do not open a browser.")
    ] = False,
) -> None:
    """Open the dependency explorer in your browser.

    Starts a small server that only answers on this computer (127.0.0.1) and needs the private address it prints.
    Pick a workspace and a master job to see its dependency graph; press Ctrl+C here to stop it.
    """
    from tmt.ui.server import serve

    if workspace is not None and not workspace.is_dir():
        _fail(f"{workspace} is not a folder", "pass the folder that contains your Talend projects")

    def announce(url: str) -> None:
        _say("tmt interface running. Open this address if your browser does not open by itself:")
        _say(url)
        _say("It only works on this computer and the address contains a private token. Press Ctrl+C to stop.")

    serve(port=port, open_browser=not no_browser, announce=announce,
          workspace=str(workspace.resolve()) if workspace else None)


@command("parsers")
def parsers_cmd() -> None:
    """List installed parsers."""
    t = Table(title="parsers")
    for col in ("name", "origin", "class"):
        t.add_column(col, overflow="fold")
    for p in load_parsers():
        cls = type(p)
        origin = "built-in" if cls.__module__.startswith("tmt.") else "plugin"
        t.add_row(p.name, origin, f"{cls.__module__}.{cls.__name__}")
    console.print(t)
