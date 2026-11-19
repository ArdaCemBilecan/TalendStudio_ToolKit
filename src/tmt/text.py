"""Small wording helpers so user-facing text never contains "(s)" placeholders."""

from __future__ import annotations


def plural(n: int, singular: str, plural_form: str | None = None) -> str:
    """``plural(1, "job")`` -> ``1 job``; ``plural(3, "job")`` -> ``3 jobs``; ``plural(2, "child", "children")``."""
    word = singular if n == 1 else (plural_form or singular + "s")
    return f"{n} {word}"
