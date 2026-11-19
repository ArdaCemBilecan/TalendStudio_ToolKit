"""Discover job files under a path, parse them, collect per-job failures, link ``tRunJob`` children."""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from tmt.config import Config
from tmt.errors import ParseError
from tmt.models import ChildJobRef, Failure, Issue, IssueCode, Job
from tmt.parsers.base import BaseParser
from tmt.parsers.registry import load_parsers
from tmt.scoring.complexity import score_jobs

log = logging.getLogger(__name__)
_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".metadata", "recycle_bin"}


@dataclass
class ScanResult:
    jobs: list[Job] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)


def discover(root: Path) -> list[Path]:
    """Every file under ``root`` (or ``root`` itself), skipping tool/VCS folders. Sorted for determinism.

    What counts as a job is decided by the parsers (``BaseParser.can_parse``), not here, so a parser for another
    tool needs no change in this module.
    """
    if root.is_file():
        return [root]
    found = [
        p for p in root.rglob("*")
        if p.is_file() and not any(part in _SKIP_DIRS for part in p.relative_to(root).parts[:-1])
    ]
    return sorted(found, key=lambda p: p.as_posix())


def scan(
    root: Path,
    *,
    context: str | None = None,
    parsers: list[BaseParser] | None = None,
    config: Config | None = None,
) -> ScanResult:
    """Parse every job under ``root`` and score it. One broken job never stops the run."""
    config = config or Config()
    parsers = parsers or load_parsers(config.dialect)
    base = root if root.is_dir() else root.parent
    result = ScanResult()
    for path in discover(root):
        parser = next((p for p in parsers if p.can_parse(path)), None)
        if parser is None:
            log.debug("skipping %s (no parser accepts it)", path)
            continue
        try:
            result.jobs.append(parser.parse(path, context=context, base=base))
        except ParseError as exc:
            log.warning("failed: %s", exc)
            result.failures.append(Failure(path=exc.path, error=exc.message))
        except Exception as exc:  # a parser bug must not take down the whole run
            log.exception("unexpected error in %s", path)
            result.failures.append(Failure(path=path.name, error=f"{type(exc).__name__}: {exc}"))
    assign_keys(result.jobs)
    link_children(result.jobs)
    score_jobs(result.jobs, config)
    result.jobs.sort(key=lambda j: (j.key, j.version, j.path))
    return result


def assign_keys(jobs: list[Job]) -> None:
    """Give every job a key that is unique within the scan.

    A job keeps its plain name when nobody else has it. Real workspaces repeat names (the same job in DEV and PROD,
    in two folders, in two versions), so those get ``name@project``, then ``name@project/folder``, then the path.
    """
    groups: dict[str, list[Job]] = defaultdict(list)
    for j in jobs:
        groups[j.name].append(j)
    for name, group in groups.items():
        if len(group) == 1:
            group[0].key = name
            continue
        candidates: list[Callable[[Job], str]] = [
            lambda j: j.project or "",
            lambda j: f"{j.project or ''}/{j.folder}".strip("/") if j.project else j.folder,
            lambda j: j.path.rsplit(".", 1)[0],
        ]
        for pick in candidates:
            suffixes = [pick(j) for j in group]
            if all(suffixes) and len(set(suffixes)) == len(suffixes):
                for j, suffix in zip(group, suffixes, strict=True):
                    j.key = f"{name}@{suffix}"
                break
        else:  # identical project, folder and path cannot happen for distinct files, but never collide silently
            for n, j in enumerate(sorted(group, key=lambda j: j.path), start=1):
                j.key = f"{name}#{n}"


def link_children(jobs: list[Job]) -> None:
    """Resolve ``tRunJob`` children: by Studio id first (exact), then by name when the name is unambiguous."""
    # Ids are only unique inside a project: a copied project (PROD cloned from DEV) repeats every id.
    by_pid = {(j.project, j.repo_id): j for j in jobs if j.repo_id}
    ids: dict[str, list[Job]] = defaultdict(list)
    for j in jobs:
        if j.repo_id:
            ids[j.repo_id].append(j)
    by_name: dict[str, list[Job]] = defaultdict(list)
    for j in jobs:
        by_name[j.name].append(j)
    for job in jobs:
        linked: list[ChildJobRef] = []
        for child in job.child_jobs:
            cid = child.id or ""
            target = by_pid.get((child.project, cid)) or by_pid.get((job.project, cid))
            if target is None and len(ids.get(child.id or "", [])) == 1:
                target = ids[child.id or ""][0]
            why = "was not found in the scanned path"
            if target is None and len(by_name.get(child.name, [])) == 1:
                target = by_name[child.name][0]
            elif target is None and len(by_name.get(child.name, [])) > 1:
                why = f"matches {len(by_name[child.name])} scanned jobs and its id does not identify one"
            linked.append(ChildJobRef(name=child.name, id=child.id, project=child.project,
                                      resolved=target is not None, key=target.key if target else None))
            if target is None:
                job.issues.append(Issue(
                    code=IssueCode.UNRESOLVED_REPOSITORY_REF, severity="warning",
                    message=f"child job {child.name!r} called by tRunJob {why}"))
        job.child_jobs = linked
        job.issues.sort(key=lambda i: (i.component_id or "", i.code.value, i.message))


def file_stem(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", key)


def write_text(path: Path, text: str) -> None:
    """Write UTF-8 with LF line endings on every platform (byte-identical output)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def dumps(obj: object) -> str:
    """Deterministic JSON: sorted keys, 2-space indent, trailing newline."""
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_models(result: ScanResult, out_dir: Path) -> list[Path]:
    """``out/model/<job>.json``, ``_all.json`` and (if any) ``_errors.json``."""
    model_dir = out_dir / "model"
    model_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    stems: dict[str, int] = {}
    for job in result.jobs:
        stem = file_stem(job.key)
        stems[stem] = stems.get(stem, 0) + 1
        p = model_dir / f"{stem}{'' if stems[stem] == 1 else '_' + str(stems[stem])}.json"
        p.write_text(dumps(job.model_dump(mode="json")), encoding="utf-8", newline="\n")
        written.append(p)
    allp = model_dir / "_all.json"
    allp.write_text(dumps([j.model_dump(mode="json") for j in result.jobs]), encoding="utf-8", newline="\n")
    written.append(allp)
    errp = model_dir / "_errors.json"
    if result.failures:
        errp.write_text(dumps([f.model_dump(mode="json") for f in result.failures]), encoding="utf-8", newline="\n")
        written.append(errp)
    elif errp.exists():
        errp.unlink()
    return written
