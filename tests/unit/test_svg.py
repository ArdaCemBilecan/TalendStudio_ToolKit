from __future__ import annotations

import time
import xml.dom.minidom as minidom
from pathlib import Path

from typer.testing import CliRunner

from tmt.cli import app
from tmt.lineage import builder
from tmt.lineage.svg import to_svg
from tmt.models import ChildJobRef, Expression, Job, Resolution, TableRef
from tmt.scan import scan

SAMPLES = Path(__file__).resolve().parents[2] / "samples" / "talend"


def ref(name: str, access: str, resolved: bool = True) -> TableRef:
    res = Resolution.RESOLVED if resolved else Resolution.CONTEXT_DEPENDENT
    schema, _, table = name.rpartition(".")
    return TableRef(db_schema=schema or None, name=table, expression=Expression(raw=f'"{name}"', resolution=res),
                    resolution=res, access=access, action="select" if access == "read" else "insert",  # type: ignore[arg-type]
                    component_id="c", via="table_param")


def job(name: str, reads: list[str] = (), writes: list[str] = (), children: list[str] = (), unresolved: str | None = None) -> Job:  # type: ignore[assignment]
    w = [ref(x, "write") for x in writes]
    if unresolved:
        w.append(ref(unresolved, "write", resolved=False))
    return Job(name=name, version="1", path=name, reads=[ref(x, "read") for x in reads], writes=w,
               child_jobs=[ChildJobRef(name=c, resolved=True, key=c) for c in children])


def render(*jobs: Job) -> str:
    return to_svg(builder.build_graph(list(jobs)))


def test_output_is_well_formed_xml_with_one_shape_per_node() -> None:
    g = builder.build_graph(scan(SAMPLES).jobs)
    doc = minidom.parseString(to_svg(g))
    shapes = len(doc.getElementsByTagName("rect")) + len(doc.getElementsByTagName("polygon"))
    assert shapes == len(g) + 3  # + the legend swatches for job, table and file (the dashed sample is a path)
    assert len(doc.getElementsByTagName("path")) >= g.number_of_edges()


def test_it_is_self_contained_and_accessible() -> None:
    svg = render(job("a", writes=["S.T"]))
    assert "<script" not in svg and "href=" not in svg and "http://www.w3.org/2000/svg" in svg
    assert 'role="img"' in svg and "<title>lineage graph" in svg
    assert "prefers-color-scheme: dark" in svg  # light and dark


def test_every_node_has_a_full_text_tooltip() -> None:
    svg = render(job("a_very_long_job_name_" * 4, reads=["S.T"]))
    assert "…" in svg  # shortened on the picture ...
    assert svg.count("a_very_long_job_name_a_very_long_job_name_a_very_long_job_name_a_very_long_job_name_") >= 1  # ... full in <title>


def test_dependencies_flow_left_to_right() -> None:
    svg = render(job("a", writes=["S.T"]), job("b", reads=["S.T"], writes=["S.U"]), job("c", reads=["S.U"]))
    doc = minidom.parseString(svg)
    xs = {}
    for g in doc.getElementsByTagName("g"):
        title = g.getElementsByTagName("title")[0].firstChild.data  # type: ignore[union-attr]
        shape = (g.getElementsByTagName("rect") or g.getElementsByTagName("polygon"))[0]
        xs[title] = float(shape.getAttribute("x")) if shape.tagName == "rect" else float(shape.getAttribute("points").split(",")[0])
    assert xs["a"] < xs["S.T"] < xs["b"] < xs["S.U"] < xs["c"]


def test_unresolved_target_is_marked_in_words_and_fits_its_box() -> None:
    svg = render(job("a", unresolved="STG.X"))
    assert "(unresolved)" in svg and "tmt-unresolved" in svg
    doc = minidom.parseString(svg)
    node = next(r for r in doc.getElementsByTagName("rect") if "tmt-unresolved" in r.getAttribute("class"))
    assert float(node.getAttribute("width")) >= 7.2 * len("STG.X (unresolved)")


def test_hostile_labels_are_escaped() -> None:
    evil = '<script>alert(1)</script>&"\''
    svg = render(job(evil, writes=["S.T"]))
    assert "<script>" not in svg
    minidom.parseString(svg)  # still valid XML


def test_trunjob_edges_are_labelled_and_heavier() -> None:
    svg = render(job("parent", children=["kid"]), job("kid"))
    assert "tmt-runs" in svg and ">tRunJob<" in svg


def test_a_cycle_is_drawn_not_crashed_on() -> None:
    svg = render(job("a", reads=["S.B"], writes=["S.A"]), job("b", reads=["S.A"], writes=["S.B"]))
    minidom.parseString(svg)
    assert "tmt-dash" in svg  # the back edge is dashed


def test_empty_graph() -> None:
    svg = to_svg(builder.build_graph([]))
    minidom.parseString(svg)
    assert "No jobs to draw" in svg


def test_deterministic_and_independent_of_input_order() -> None:
    jobs = scan(SAMPLES).jobs
    assert to_svg(builder.build_graph(jobs)) == to_svg(builder.build_graph(list(reversed(jobs))))


def test_a_large_graph_is_fast() -> None:
    jobs = [job(f"j{i}", reads=[f"S.T{i}"], writes=[f"S.T{i + 1}"]) for i in range(150)]
    t0 = time.perf_counter()
    svg = render(*jobs)
    assert time.perf_counter() - t0 < 5 and minidom.parseString(svg)


def test_cli_graph_svg_to_stdout_and_file(tmp_path: Path) -> None:
    runner = CliRunner()
    r = runner.invoke(app, ["graph", str(SAMPLES), "--format", "svg"])
    assert r.exit_code == 0 and r.stdout.startswith("<svg ")
    r = runner.invoke(app, ["graph", str(SAMPLES), "--format", "svg", "--out", str(tmp_path), "-q"])
    assert r.exit_code == 0 and (tmp_path / "graph" / "lineage.svg").read_text(encoding="utf-8").startswith("<svg ")
