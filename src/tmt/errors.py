"""Exception hierarchy. Per-job failures are collected by the caller, never fatal for a run."""


class TmtError(Exception):
    """Base class for all toolkit errors."""


class ParseError(TmtError):
    """A job file could not be parsed (malformed XML, wrong root element, ...)."""

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message
