"""Fuzz-style tests: malformed, truncated and hostile XML must never crash a scan."""

from __future__ import annotations

import contextlib
import random
from pathlib import Path

from tests.helpers import node, write_job
from tmt.errors import ParseError
from tmt.parsers.talend.parser import TalendParser
from tmt.scan import scan

SAMPLE = Path(__file__).resolve().parents[2] / "samples" / "talend" / "process" / "job_join_lookup_0.1.item"


def test_every_truncation_point_is_handled(tmp_path: Path) -> None:
    data = SAMPLE.read_bytes()
    parser = TalendParser()
    rng = random.Random(7)
    cuts = sorted({0, 1, 10, len(data) - 1, *(rng.randrange(len(data)) for _ in range(150))})
    for cut in cuts:
        p = tmp_path / "t_0.1.item"
        p.write_bytes(data[:cut])
        with contextlib.suppress(ParseError):  # expected for truncated XML; anything else fails the test
            parser.parse(p)


def test_random_byte_corruption_is_handled(tmp_path: Path) -> None:
    data = bytearray(SAMPLE.read_bytes())
    parser = TalendParser()
    rng = random.Random(11)
    for _ in range(60):
        mutated = bytearray(data)
        for _ in range(rng.randrange(1, 8)):
            mutated[rng.randrange(len(mutated))] = rng.randrange(256)
        p = tmp_path / "m_0.1.item"
        p.write_bytes(bytes(mutated))
        with contextlib.suppress(ParseError):
            parser.parse(p)


def test_scan_collects_failures_and_keeps_going(tmp_path: Path) -> None:
    write_job(tmp_path, "good", node("a_1", "tLogRow"))
    (tmp_path / "bad_0.1.item").write_text("<talendfile:ProcessType", encoding="utf-8")
    result = scan(tmp_path)
    assert [j.name for j in result.jobs] == ["good"]
    assert len(result.failures) == 1 and result.failures[0].path == "bad_0.1.item"


def test_external_entity_is_not_expanded(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET-VALUE", encoding="utf-8")
    item = tmp_path / "xxe_0.1.item"
    item.write_text(
        '<?xml version="1.0"?>\n'
        f'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "{secret.as_uri()}">]>\n'
        '<talendfile:ProcessType xmlns:talendfile="x" defaultContext="Default">'
        '<node componentName="tLogRow"><elementParameter field="TEXT" name="UNIQUE_NAME" value="a_1"/>'
        '<elementParameter field="TEXT" name="LABEL" value="&xxe;"/></node></talendfile:ProcessType>',
        encoding="utf-8",
    )
    try:
        job = TalendParser().parse(item)
    except ParseError:
        return  # rejecting the file is also acceptable
    assert "TOPSECRET" not in job.model_dump_json()


def test_entity_expansion_bomb_is_harmless(tmp_path: Path) -> None:
    lol = "".join(f'<!ENTITY l{i} "&l{i - 1};&l{i - 1};&l{i - 1};&l{i - 1};">' for i in range(1, 9))
    item = tmp_path / "bomb_0.1.item"
    item.write_text(
        f'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY l0 "lol">{lol}]>'
        '<talendfile:ProcessType xmlns:talendfile="x"><node componentName="tLogRow">'
        '<elementParameter field="TEXT" name="UNIQUE_NAME" value="a_1"/>'
        '<elementParameter field="TEXT" name="LABEL" value="&l8;"/></node></talendfile:ProcessType>',
        encoding="utf-8",
    )
    try:
        job = TalendParser().parse(item)
    except ParseError:
        return
    assert len(job.model_dump_json()) < 100_000  # not expanded


def test_unicode_and_special_characters_survive(tmp_path: Path) -> None:
    body = node("a_1", "tLogRow", '<elementParameter field="TEXT" name="LABEL" value="Müşteri &amp; Sipariş ✓"/>')
    job = TalendParser().parse(write_job(tmp_path, "j", body))
    assert job.components[0].label == "Müşteri & Sipariş ✓"


def test_very_large_job_parses(tmp_path: Path) -> None:
    body = "".join(node(f"tLogRow_{i}", "tLogRow") for i in range(3000))
    job = TalendParser().parse(write_job(tmp_path, "big", body))
    assert len(job.components) == 3000
