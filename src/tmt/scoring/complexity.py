"""Transparent, configurable complexity score per job (spec 7.2).

Score = weighted sum over countable facts in the intermediate model; every point is listed in
``Complexity.reasons``. Any custom Java forces level "hard" regardless of the score.
"""

from __future__ import annotations

import re

from tmt.config import Config
from tmt.models import Category, Complexity, Expression, IssueCode, Job, Reason, Resolution
from tmt.text import plural

_ROUTINE_RE = re.compile(r"\broutines\.[A-Za-z_]\w*\.[A-Za-z_]\w*\s*\(")


def _tmap_expressions(job: Job) -> list[tuple[str, str, Expression]]:
    """(component id, where, expression) for every expression inside every tMap."""
    out: list[tuple[str, str, Expression]] = []
    for c in job.components:
        if c.tmap is None:
            continue
        for o in c.tmap.outputs:
            if o.filter is not None:
                out.append((c.id, f"{o.name} filter", o.filter))
            out += [(c.id, f"{o.name}.{m.target}", m.expression) for m in o.mappings]
        for i in c.tmap.inputs:
            out += [(c.id, f"{i.name} join key", k.expression) for k in i.join_keys]
    return out


def score(job: Job, cfg: Config) -> Complexity:
    w, t = cfg.scoring.weights, cfg.scoring.thresholds
    reasons: list[Reason] = []

    def add(factor: str, count: int, weight: int, detail: str | None = None) -> None:
        if count and weight:
            reasons.append(Reason(factor=factor, points=count * weight, detail=detail or f"{count} x {weight}"))

    add("component", len(job.components), w.component, f"{plural(len(job.components), 'component')} x {w.component}")

    joins = [(c.id, i.name) for c in job.components if c.tmap for i in c.tmap.inputs if i.role == "lookup"]
    add("tmap_join", len(joins), w.tmap_join, f"{plural(len(joins), 'lookup join')} x {w.tmap_join}: "
        + ", ".join(f"{c}:{n}" for c, n in joins))

    exprs = _tmap_expressions(job)
    java = [(c, where) for c, where, e in exprs if e.java_calls and not _ROUTINE_RE.search(e.raw)]
    add("tmap_java_expression", len(java), w.tmap_java_expression,
        f"{plural(len(java), 'expression')} with Java calls x {w.tmap_java_expression}: "
        + ", ".join(f"{c}:{where}" for c, where in java))

    code = [c.id for c in job.components if c.category is Category.CUSTOM_CODE]
    routines = [f"{c}:{where}" for c, where, e in exprs if _ROUTINE_RE.search(e.raw)]
    custom = [*code, *routines]
    add("custom_code", len(custom), w.custom_code,
        f"{plural(len(custom), 'custom Java item')} x {w.custom_code}: " + ", ".join(custom))

    unsupported = [c.id for c in job.components if not c.supported]
    add("unsupported_component", len(unsupported), w.unsupported_component,
        f"{plural(len(unsupported), 'unsupported component')} x {w.unsupported_component}: " + ", ".join(unsupported))

    add("run_job_child", len(job.child_jobs), w.run_job_child,
        f"{plural(len(job.child_jobs), 'tRunJob child', 'tRunJob children')} x {w.run_job_child}: "
        + ", ".join(c.name for c in job.child_jobs))

    unresolved = [r for r in (*job.reads, *job.writes) if r.resolution is not Resolution.RESOLVED]
    add("unresolved_context", len(unresolved), w.unresolved_context,
        f"{plural(len(unresolved), 'context-dependent target')} x {w.unresolved_context}: "
        + ", ".join(sorted({f'{r.component_id}:{r.expression.raw}' for r in unresolved})))

    placeholders = sorted({i.component_id or "?" for i in job.issues if i.code is IssueCode.PLACEHOLDER_VALUE})
    shown = ", ".join(placeholders[:6]) + (f", ... (+{len(placeholders) - 6} more)" if len(placeholders) > 6 else "")
    add("unresolved_placeholder", len(placeholders), w.unresolved_context,
        f"{plural(len(placeholders), 'component')} with a placeholder instead of a table or query "
        f"x {w.unresolved_context}: {shown}")

    total = sum(r.points for r in reasons)
    if custom:
        level = "hard"
        reasons.append(Reason(factor="forced_hard", points=0, detail="custom Java present: always hard"))
    elif total <= t.easy_max:
        level = "easy"
    elif total <= t.medium_max:
        level = "medium"
    else:
        level = "hard"
    return Complexity(score=total, level=level, reasons=reasons)


def score_jobs(jobs: list[Job], cfg: Config) -> None:
    for job in jobs:
        job.complexity = score(job, cfg)
