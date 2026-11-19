"""Parser plugin interface. A parser turns one tool-specific file into a tool-independent Job."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from tmt.models import Job


class BaseParser(ABC):
    """Implement this to add support for another ETL tool (see docs/adding_a_parser.md)."""

    #: short identifier, also used as ``Job.source_tool``
    name: str

    @abstractmethod
    def can_parse(self, path: Path) -> bool:
        """Cheap check (no full parse) whether ``path`` is a job file this parser understands."""

    @abstractmethod
    def parse(self, path: Path, *, context: str | None = None, base: Path | None = None) -> Job:
        """Parse one job file. Raises :class:`tmt.errors.ParseError` if it is unusable.

        ``context`` selects the context group used to fold ``context.*`` expressions.
        ``base`` is the scan root; ``Job.path`` is reported relative to it so output is
        identical on every machine.
        """
