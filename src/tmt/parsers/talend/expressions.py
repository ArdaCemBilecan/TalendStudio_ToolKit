"""Classify Talend parameter values, which are Java expressions, never executing them.

``"STG.CUSTOMERS"``                         -> RESOLVED (literal)
``"SELECT " + context.cols + " FROM t"``    -> CONTEXT_DEPENDENT (folded into ``evaluated`` when the
                                              selected context group has all values)
``row1.a + row1.b`` / ``foo(x)``            -> UNRESOLVED (but columns / Java calls are still extracted)
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from tmt.models import Expression, Resolution

_STR = r'"(?:[^"\\]|\\.)*"'
_STR_RE = re.compile(_STR)
_CTX_RE = re.compile(r"\bcontext\.([A-Za-z_]\w*)")
_TOKEN_RE = re.compile(rf'\s*(?:(?P<str>{_STR})|(?P<ctx>context\.[A-Za-z_]\w*)|(?P<plus>\+))')
_STATIC_CALL_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]*)\.([A-Za-z_]\w*)\s*\(")
_INSTANCE_CALL_RE = re.compile(r"\.\s*([a-z_]\w*)\s*\(")
_PLACEHOLDER_RE = re.compile(r"^\$[A-Za-z_][\w.]*$")  # e.g. "$Stage.READER.Query" left by migration tools

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}


def _unescape(literal: str) -> str:
    return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(1)), literal[1:-1])


def _tokens(raw: str) -> list[tuple[str, str]] | None:
    """Tokenise ``raw`` as ``operand (+ operand)*`` over string literals and ``context.x``.

    Returns None when anything else is present (other Java), or the grammar is violated.
    """
    pos, out = 0, []
    while pos < len(raw):
        if not raw[pos:].strip():
            break
        m = _TOKEN_RE.match(raw, pos)
        if not m:
            return None
        kind = m.lastgroup or ""
        out.append((kind, m.group(kind).strip()))
        pos = m.end()
    if not out:
        return None
    for i, (kind, _) in enumerate(out):
        is_operand = kind in ("str", "ctx")
        if is_operand != (i % 2 == 0):
            return None
    return out if out[-1][0] != "plus" else None


def _strip_strings(raw: str) -> str:
    return _STR_RE.sub('""', raw)


def java_calls(raw: str) -> list[str]:
    code = _strip_strings(raw)
    calls = {f"{m.group(1)}.{m.group(2)}" for m in _STATIC_CALL_RE.finditer(code)}
    remainder = _STATIC_CALL_RE.sub("(", code)
    calls |= {m.group(1) for m in _INSTANCE_CALL_RE.finditer(remainder)}
    return sorted(calls)


def parse_expression(
    raw: str,
    context_values: Mapping[str, str | None] | None = None,
    tables: Iterable[str] = (),
) -> Expression:
    """Build an :class:`Expression`. ``tables`` are tMap table names used to find column references."""
    code = _strip_strings(raw)
    ctx_refs = sorted(set(_CTX_RE.findall(code)))
    columns = sorted(
        {f"{t}.{c}" for t in tables for c in re.findall(rf"\b{re.escape(t)}\.([A-Za-z_]\w*)", code)}
    )
    calls = java_calls(raw)
    base = {"raw": raw, "referenced_columns": columns, "referenced_context": ctx_refs, "java_calls": calls}

    stripped = raw.strip()
    if not stripped:
        return Expression(**base, resolution=Resolution.RESOLVED, value="")

    toks = _tokens(raw)
    if toks and all(k != "ctx" for k, _ in toks):
        value = "".join(_unescape(t) for k, t in toks if k == "str")
        if _PLACEHOLDER_RE.match(value):
            return Expression(**base, resolution=Resolution.UNRESOLVED)
        return Expression(**base, resolution=Resolution.RESOLVED, value=value)
    if toks:
        evaluated = _fold(toks, context_values or {})
        return Expression(**base, resolution=Resolution.CONTEXT_DEPENDENT, evaluated=evaluated)
    if ctx_refs:
        return Expression(**base, resolution=Resolution.CONTEXT_DEPENDENT)
    return Expression(**base, resolution=Resolution.UNRESOLVED)


def _fold(toks: list[tuple[str, str]], values: Mapping[str, str | None]) -> str | None:
    parts: list[str] = []
    for kind, text in toks:
        if kind == "str":
            parts.append(_unescape(text))
        elif kind == "ctx":
            v = values.get(text.split(".", 1)[1])
            if v is None:
                return None
            parts.append(v)
    return "".join(parts)


def sql_text(raw: str) -> tuple[str | None, bool]:
    """SQL text from a (possibly concatenated) Java expression, or ``(None, False)``.

    Every ``context.x`` becomes the marker ``__CTX_x__`` so the statement can be parsed and the
    tables that depend on context can be told apart from plain ones; the caller substitutes the
    values afterwards. The flag says whether any marker was inserted.
    """
    toks = _tokens(raw)
    if not toks:
        return None, False
    parts, has_marker = [], False
    for kind, text in toks:
        if kind == "str":
            parts.append(_unescape(text))
        elif kind == "ctx":
            parts.append(f"__CTX_{text.split('.', 1)[1]}__")
            has_marker = True
    return "".join(parts), has_marker
