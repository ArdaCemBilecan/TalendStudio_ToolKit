"""The documentation must not drift from the code: every Python snippet compiles, every link resolves."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = sorted([*(ROOT / "docs").glob("*.md"), ROOT / "README.md", ROOT / "CHANGELOG.md", ROOT / "CONTRIBUTING.md",
              ROOT / "samples" / "talend" / "README.md"])


def blocks(text: str, lang: str) -> list[str]:
    return re.findall(rf"```{lang}\n(.*?)```", text, re.S)


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_python_snippets_are_valid_syntax(doc: Path) -> None:
    for code in blocks(doc.read_text(encoding="utf-8"), "python"):
        ast.parse(code.replace("...  # build components, connections, table references", "pass").replace("...", "pass"))


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_relative_links_point_at_existing_files(doc: Path) -> None:
    for target in re.findall(r"\]\((?!https?:|#|mailto:)([^)\s]+)\)", doc.read_text(encoding="utf-8")):
        path = target.split("#")[0]
        assert (doc.parent / path).exists(), f"{doc.name}: broken link {target}"


def test_documented_commands_exist() -> None:
    from typer.main import get_command

    from tmt.cli import app

    registered = set(get_command(app).commands)  # type: ignore[attr-defined]
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for cmd in set(re.findall(r"`tmt (\w+)[ `]", text)):
        assert cmd in registered, f"README mentions `tmt {cmd}` which does not exist"
    assert registered == {"scan", "graph", "ui", "parsers"}  # the product is the dependency graph: nothing else


def test_adding_a_parser_skeleton_matches_the_real_interface() -> None:
    import inspect

    from tmt.parsers.base import BaseParser

    text = (ROOT / "docs" / "adding_a_parser.md").read_text(encoding="utf-8")
    params = inspect.signature(BaseParser.parse).parameters
    assert {"context", "base"} <= set(params)
    assert "def parse(self, path: Path, *, context: str | None = None, base: Path | None = None) -> Job:" in text
    assert "tmt.parsers" in text and "tmt.testing.contract" in text


def test_dummy_parser_example_in_the_docs_is_the_tested_one() -> None:
    text = (ROOT / "docs" / "adding_a_parser.md").read_text(encoding="utf-8")
    assert "tests/dummy_parser.py" in text and (ROOT / "tests" / "dummy_parser.py").exists()
