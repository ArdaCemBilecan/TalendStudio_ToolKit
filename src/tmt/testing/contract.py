"""Contract tests every parser must pass. Reusable by third-party parser packages.

Use it from your own test suite::

    from tmt.testing.contract import ParserContract

    class TestMyParser(ParserContract):
        @pytest.fixture
        def parser(self): return MyParser()
        @pytest.fixture
        def valid_files(self, tmp_path): ...      # list[Path] of jobs your parser must accept
        @pytest.fixture
        def broken_file(self, tmp_path): ...      # a file it recognises but cannot parse
        @pytest.fixture
        def foreign_file(self, tmp_path): ...     # a file that belongs to some other tool

``validate_job`` is a plain function (no pytest needed) that returns a list of problems, so you can also
call it from your own checks.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from tmt.config import Config
from tmt.errors import ParseError
from tmt.lineage import builder
from tmt.lineage.explore import explore
from tmt.lineage.svg import to_svg
from tmt.models import Job
from tmt.parsers.base import BaseParser
from tmt.scan import dumps, scan
from tmt.scoring.complexity import score_jobs


def validate_job(job: Job) -> list[str]:
    """Structural invariants of a well-formed :class:`Job`. Empty list means valid."""
    problems: list[str] = []
    ids = [c.id for c in job.components]
    if len(ids) != len(set(ids)):
        problems.append("component ids are not unique")
    known = set(ids)
    if not job.name or not job.version or not job.path:
        problems.append("name, version and path must be non-empty")
    if not job.source_tool:
        problems.append("source_tool must be set")
    for c in job.connections:
        for end, value in (("source", c.source), ("target", c.target)):
            if value not in known:
                problems.append(f"connection {c.name!r}: {end} {value!r} is not a component")
    names = [c.name for c in job.connections]
    if len(names) != len(set(names)):
        problems.append("connection names are not unique")
    in_subjob: list[str] = []
    for sj in job.subjobs:
        if sj.id not in known:
            problems.append(f"subjob {sj.id!r} is not a component")
        in_subjob += sj.component_ids
    if job.subjobs and sorted(in_subjob) != sorted(known):
        problems.append("every component must belong to exactly one subjob")
    subjob_ids = {s.id for s in job.subjobs}
    for comp in job.components:
        if comp.subjob_id is not None and comp.subjob_id not in subjob_ids:
            problems.append(f"component {comp.id!r} refers to unknown subjob {comp.subjob_id!r}")
    for ref in (*job.reads, *job.writes):
        if ref.component_id not in known:
            problems.append(f"table reference points at unknown component {ref.component_id!r}")
    for issue in job.issues:
        if issue.component_id is not None and issue.component_id not in known:
            problems.append(f"issue {issue.code.value} points at unknown component {issue.component_id!r}")
    if any(not c.name for c in job.child_jobs):
        problems.append("child job without a name")
    return problems


class ParserContract:
    """Subclass and override the fixtures; every ``test_*`` below then runs against your parser."""

    #: strings that must never appear in the serialised job (passwords, tokens used in your fixtures)
    secret_markers: Sequence[str] = ()

    # ---- fixtures to override
    @pytest.fixture
    def parser(self) -> BaseParser:  # pragma: no cover - overridden
        raise NotImplementedError

    @pytest.fixture
    def valid_files(self, tmp_path: Path) -> list[Path]:  # pragma: no cover - overridden
        raise NotImplementedError

    @pytest.fixture
    def broken_file(self, tmp_path: Path) -> Path:  # pragma: no cover - overridden
        raise NotImplementedError

    @pytest.fixture
    def foreign_file(self, tmp_path: Path) -> Path:  # pragma: no cover - overridden
        raise NotImplementedError

    # ---- identity
    def test_has_a_name(self, parser: BaseParser) -> None:
        assert isinstance(parser.name, str) and parser.name and parser.name == parser.name.strip()

    # ---- can_parse
    def test_can_parse_accepts_valid_files(self, parser: BaseParser, valid_files: list[Path]) -> None:
        assert valid_files, "the contract needs at least one valid file"
        assert all(parser.can_parse(p) for p in valid_files)

    def test_can_parse_rejects_foreign_files(self, parser: BaseParser, foreign_file: Path) -> None:
        assert parser.can_parse(foreign_file) is False

    def test_can_parse_never_raises_for_missing_files(self, parser: BaseParser, tmp_path: Path) -> None:
        assert parser.can_parse(tmp_path / "does_not_exist") is False

    # ---- parse
    def test_parse_returns_a_valid_job(self, parser: BaseParser, valid_files: list[Path]) -> None:
        for p in valid_files:
            job = parser.parse(p, base=p.parent)
            assert isinstance(job, Job)
            assert validate_job(job) == [], validate_job(job)
            assert job.source_tool == parser.name

    def test_path_is_relative_to_base(self, parser: BaseParser, valid_files: list[Path]) -> None:
        p = valid_files[0]
        job = parser.parse(p, base=p.parent)
        assert job.path == p.name and ":" not in job.path and "\\" not in job.path

    def test_parse_is_deterministic(self, parser: BaseParser, valid_files: list[Path]) -> None:
        for p in valid_files:
            a, b = parser.parse(p, base=p.parent), parser.parse(p, base=p.parent)
            assert dumps(a.model_dump(mode="json")) == dumps(b.model_dump(mode="json"))

    def test_job_survives_a_json_round_trip(self, parser: BaseParser, valid_files: list[Path]) -> None:
        job = parser.parse(valid_files[0], base=valid_files[0].parent)
        assert Job.model_validate_json(job.model_dump_json()) == job

    def test_context_argument_is_accepted(self, parser: BaseParser, valid_files: list[Path]) -> None:
        parser.parse(valid_files[0], context="Default", base=valid_files[0].parent)
        parser.parse(valid_files[0], context="a-context-that-does-not-exist", base=valid_files[0].parent)

    def test_secrets_are_not_serialised(self, parser: BaseParser, valid_files: list[Path]) -> None:
        blob = " ".join(parser.parse(p, base=p.parent).model_dump_json() for p in valid_files)
        for marker in self.secret_markers:
            assert marker not in blob

    # ---- failures
    def test_broken_file_raises_parse_error_only(self, parser: BaseParser, broken_file: Path) -> None:
        with pytest.raises(ParseError):
            parser.parse(broken_file, base=broken_file.parent)

    def test_scan_collects_a_failure_and_keeps_going(
        self, parser: BaseParser, valid_files: list[Path], broken_file: Path
    ) -> None:
        folder = valid_files[0].parent
        assert broken_file.parent == folder, "put valid_files and broken_file in the same folder"
        result = scan(folder, parsers=[parser])
        assert result.jobs and result.failures

    # ---- the rest of the pipeline only sees the model
    def test_downstream_pipeline_works_on_the_model(self, parser: BaseParser, valid_files: list[Path]) -> None:
        result = scan(valid_files[0].parent, parsers=[parser])
        score_jobs(result.jobs, Config())
        graph = builder.build_graph(result.jobs)
        builder.analyze(graph)
        assert builder.to_mermaid(graph).startswith("graph LR")
        view = explore(result.jobs, None)
        assert view.summary["jobs"] == len(result.jobs) and "<svg" in to_svg(view.graph, interactive=True)
