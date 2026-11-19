"""Both the real Talend parser and the dummy second parser must satisfy the same contract."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests.dummy_parser import DummyParser
from tests.helpers import node, write_job
from tmt.models import Connection, ConnKind
from tmt.parsers.base import BaseParser
from tmt.parsers.talend.parser import TalendParser
from tmt.testing.contract import ParserContract, validate_job

SAMPLE_DIR = Path(__file__).resolve().parents[2] / "samples" / "talend" / "process"


class TestTalendParserContract(ParserContract):
    secret_markers = ("enc:system", "SuperSecret1")

    @pytest.fixture
    def parser(self) -> BaseParser:
        return TalendParser()

    @pytest.fixture
    def valid_files(self, tmp_path: Path) -> list[Path]:
        out = []
        for name in ("job_join_lookup", "job_context_driven", "job_orchestrator"):
            for ext in ("item", "properties"):
                shutil.copy(SAMPLE_DIR / f"{name}_0.1.{ext}", tmp_path)
            out.append(tmp_path / f"{name}_0.1.item")
        return out

    @pytest.fixture
    def broken_file(self, tmp_path: Path) -> Path:
        shutil.copy(SAMPLE_DIR / "job_join_lookup_0.1.item", tmp_path)  # same folder as valid_files
        p = tmp_path / "broken_0.1.item"
        p.write_text(
            '<talendfile:ProcessType xmlns:talendfile="x"><node componentName="tMap"', encoding="utf-8")
        return p

    @pytest.fixture
    def foreign_file(self, tmp_path: Path) -> Path:
        p = tmp_path / "meta.item"
        p.write_text("<?xml version='1.0'?><xmi:XMI xmlns:xmi='x'/>", encoding="utf-8")
        return p


class TestDummyParserContract(ParserContract):
    @pytest.fixture
    def parser(self) -> BaseParser:
        return DummyParser()

    @pytest.fixture
    def valid_files(self, tmp_path: Path) -> list[Path]:
        a = tmp_path / "a.dummyjob.json"
        a.write_text(json.dumps({"name": "load_x", "steps": [
            {"id": "r1", "kind": "read", "table": "SRC.X"},
            {"id": "t1", "kind": "transform", "after": ["r1"]},
            {"id": "w1", "kind": "write", "table": "STG.X", "after": ["t1"]}]}), encoding="utf-8")
        return [a]

    @pytest.fixture
    def broken_file(self, tmp_path: Path) -> Path:
        p = tmp_path / "b.dummyjob.json"
        p.write_text('{"name": "x", "steps": [{"id": "s", "kind": "teleport"}]}', encoding="utf-8")
        return p

    @pytest.fixture
    def foreign_file(self, tmp_path: Path) -> Path:
        p = tmp_path / "c.txt"
        p.write_text("nope", encoding="utf-8")
        return p


# ---------------------------------------------------------------- validate_job itself
def test_validate_job_accepts_real_jobs() -> None:
    from tmt.scan import scan

    assert all(validate_job(j) == [] for j in scan(SAMPLE_DIR.parent).jobs)


def test_validate_job_catches_each_kind_of_corruption(tmp_path: Path) -> None:
    job = TalendParser().parse(write_job(tmp_path, "j", node("a_1", "tLogRow") + node("b_1", "tLogRow")))
    assert validate_job(job) == []

    dup = job.model_copy(deep=True)
    dup.components.append(dup.components[0].model_copy())
    assert "component ids are not unique" in validate_job(dup)

    bad_conn = job.model_copy(deep=True)
    bad_conn.connections.append(Connection(name="x", source="ghost", target="a_1", kind=ConnKind.FLOW))
    assert any("ghost" in p for p in validate_job(bad_conn))

    bad_sj = job.model_copy(deep=True)
    bad_sj.subjobs = bad_sj.subjobs[:1]
    assert "every component must belong to exactly one subjob" in validate_job(bad_sj)

    nameless = job.model_copy(deep=True)
    nameless.name = ""
    assert any("non-empty" in p for p in validate_job(nameless))
