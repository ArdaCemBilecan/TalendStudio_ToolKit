"""Golden-file tests: JSON models and graphs for the sample jobs must stay byte-identical.

Regenerate deliberately with ``UPDATE_GOLDEN=1 pytest tests/integration/test_golden.py`` and review the diff.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tmt.lineage import builder
from tmt.scan import dumps, scan, write_models

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "golden"
SAMPLES = ROOT / "samples" / "talend"
UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"


def check(actual: str, golden: Path) -> None:
    if UPDATE:
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(actual, encoding="utf-8", newline="\n")
        return
    assert golden.exists(), f"missing golden file {golden}; run with UPDATE_GOLDEN=1"
    assert actual == golden.read_text(encoding="utf-8"), f"{golden.name} differs from its golden file"


def test_model_json_matches_golden(tmp_path: Path) -> None:
    result = scan(SAMPLES)
    write_models(result, tmp_path)
    produced = sorted((tmp_path / "model").glob("*.json"))
    assert len(produced) == 18  # 17 jobs + _all.json
    for p in produced:
        check(p.read_text(encoding="utf-8"), GOLDEN / "model" / p.name)
    if not UPDATE:
        stale = {p.name for p in (GOLDEN / "model").glob("*.json")} - {p.name for p in produced}
        assert not stale, f"golden files without a job: {stale}"


@pytest.mark.parametrize(
    ("fmt", "name"), [("mermaid", "lineage.mmd"), ("dot", "lineage.dot"), ("json", "lineage.json")]
)
def test_graph_matches_golden(fmt: str, name: str) -> None:
    g = builder.build_graph(scan(SAMPLES).jobs)
    text = {
        "mermaid": lambda: builder.to_mermaid(g),
        "dot": lambda: builder.to_dot(g),
        "json": lambda: dumps(builder.to_json(g, builder.analyze(g))),
    }[fmt]()
    check(text, GOLDEN / "graph" / name)








def test_focus_graph_matches_golden() -> None:
    g = builder.focus(builder.build_graph(scan(SAMPLES).jobs), "job_orchestrator")
    check(builder.to_mermaid(g), GOLDEN / "graph" / "focus_job_orchestrator.mmd")


def test_output_is_byte_identical_across_runs(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    write_models(scan(SAMPLES), a)
    write_models(scan(SAMPLES), b)
    for p in (a / "model").glob("*.json"):
        assert p.read_bytes() == (b / "model" / p.name).read_bytes()


def test_output_does_not_depend_on_discovery_order(tmp_path: Path) -> None:
    result = scan(SAMPLES)
    result.jobs.reverse()
    g1 = builder.to_mermaid(builder.build_graph(result.jobs))
    assert g1 == (GOLDEN / "graph" / "lineage.mmd").read_text(encoding="utf-8") or UPDATE
