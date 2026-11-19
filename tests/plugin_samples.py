"""Deliberately broken or unusual plugins, used by the registry tests."""

from __future__ import annotations

from pathlib import Path

from tmt.models import Job
from tmt.parsers.base import BaseParser


class _Stub(BaseParser):
    name = "stub"

    def can_parse(self, path: Path) -> bool:
        return False

    def parse(self, path: Path, *, context: str | None = None, base: Path | None = None) -> Job:
        raise NotImplementedError


class SecondDummy(_Stub):
    name = "second"


class NotAParser:
    name = "not-a-parser"


class NamelessParser(_Stub):
    name = ""


class ImpostorParser(_Stub):
    name = "talend"  # clashes with the built-in parser


class ExplodingParser(_Stub):
    name = "exploding"

    def __init__(self) -> None:
        raise RuntimeError("boom")


class DialectParser(_Stub):
    name = "dialectual"

    def __init__(self, dialect: str | None = None) -> None:
        self.dialect = dialect
