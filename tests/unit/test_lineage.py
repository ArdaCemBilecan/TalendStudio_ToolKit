from __future__ import annotations

import pytest

from tmt.lineage import builder
from tmt.models import ChildJobRef, Expression, Job, Resolution, TableRef


def ref(name: str | None, access: str, action: str = "insert", raw: str | None = None, via: str = "table_param",
        resolution: Resolution = Resolution.RESOLVED) -> TableRef:
    schema, _, table = (name or "").rpartition(".")
    text = raw or f'"{name}"'
    return TableRef(
        db_schema=schema or None, name=table or None, expression=Expression(raw=text, resolution=resolution),
        resolution=resolution, access=access, action=action, component_id="c_1", via=via,  # type: ignore[arg-type]
    )


def job(name: str, reads: list[str] | None = None, writes: list[str] | None = None,
        children: list[str] | None = None) -> Job:
    return Job(
        name=name, version="0.1", path=f"{name}.item",
        reads=[ref(r, "read", "select") for r in reads or []],
        writes=[ref(w, "write") for w in writes or []],
        child_jobs=[ChildJobRef(name=c, resolved=True) for c in children or []],
    )


def test_nodes_and_edges() -> None:
    g = builder.build_graph([job("a", writes=["S.T"]), job("b", reads=["s.t"], writes=["S.U"])])
    assert {n for n, d in g.nodes(data=True) if d["type"] == "table"} == {"table:S.T", "table:S.U"}  # case-insensitive
    assert g.has_edge("job:a", "table:S.T") and g.has_edge("table:S.T", "job:b")


def test_migration_order_producers_before_consumers() -> None:
    jobs = [job("c", reads=["S.U"]), job("b", reads=["S.T"], writes=["S.U"]), job("a", writes=["S.T"])]
    a = builder.analyze(builder.build_graph(jobs))
    assert a.migration_order == ["a", "b", "c"] and a.cycles == []


def test_children_come_before_parent() -> None:
    jobs = [job("parent", children=["kid1", "kid2"]), job("kid1"), job("kid2")]
    a = builder.analyze(builder.build_graph(jobs))
    assert a.migration_order.index("parent") == 2


def test_cycles_are_reported_not_fatal() -> None:
    jobs = [job("a", reads=["S.B"], writes=["S.A"]), job("b", reads=["S.A"], writes=["S.B"]), job("c", reads=["S.B"])]
    a = builder.analyze(builder.build_graph(jobs))
    assert a.cycles == [["a", "b"]]
    assert a.migration_order == ["a", "b", "c"]


def test_job_reading_and_writing_same_table_is_not_a_cycle() -> None:
    a = builder.analyze(builder.build_graph([job("a", reads=["S.T"], writes=["S.T"])]))
    assert a.cycles == [] and a.migration_order == ["a"]


def test_table_roles_and_statistics() -> None:
    jobs = [job("a", reads=["SRC.X"], writes=["STG.Y"]), job("b", reads=["STG.Y", "SRC.X"], writes=["MART.Z"]),
            job("lonely")]
    a = builder.analyze(builder.build_graph(jobs))
    assert a.tables_never_read == ["MART.Z"]
    assert a.tables_never_written == ["SRC.X"]
    assert a.most_depended_on[0] == {"table": "SRC.X", "readers": 2}
    assert a.jobs_without_inputs == ["lonely"]


def test_unresolved_table_keeps_raw_expression() -> None:
    j = Job(name="a", version="0.1", path="a.item", writes=[
        ref(None, "write", raw='context.s + ".T"', resolution=Resolution.CONTEXT_DEPENDENT)])
    g = builder.build_graph([j])
    (tid,) = [n for n, d in g.nodes(data=True) if d["type"] == "table"]
    assert g.nodes[tid]["resolved"] is False and g.nodes[tid]["expression"] == 'context.s + ".T"'
    assert builder.analyze(g).unresolved_tables == ['context.s + ".T"']
    assert "(unresolved)" in builder.to_mermaid(g) and "-.->" in builder.to_mermaid(g)


def test_one_resolved_reference_makes_the_node_resolved() -> None:
    folded = ref("S.T", "read", "select", raw="context.t", resolution=Resolution.CONTEXT_DEPENDENT)
    plain = ref("S.T", "read", "select")
    a = Job(name="a", version="0.1", path="a", reads=[folded])
    b = Job(name="b", version="0.1", path="b", reads=[plain])
    g = builder.build_graph([a, b])
    assert g.nodes["table:S.T"]["resolved"] is True
    assert g["table:S.T"]["job:a"]["resolved"] is False and g["table:S.T"]["job:b"]["resolved"] is True


def test_focus_keeps_up_and_downstream_only() -> None:
    jobs = [job("a", writes=["S.T"]), job("b", reads=["S.T"], writes=["S.U"]), job("c", reads=["S.U"]),
            job("other", reads=["X.Y"])]
    g = builder.focus(builder.build_graph(jobs), "b")
    assert {d["label"] for _, d in g.nodes(data=True)} == {"a", "b", "c", "S.T", "S.U"}
    with pytest.raises(KeyError):
        builder.focus(builder.build_graph(jobs), "nope")


def test_unknown_child_becomes_external_node() -> None:
    g = builder.build_graph([job("p", children=["ghost"])])
    assert g.nodes["job:ghost"]["external"] is True


def test_renderers_are_deterministic_and_well_formed() -> None:
    jobs = [job("b", reads=["S.T"]), job("a", writes=["S.T"]), job("p", children=["a"])]
    g1, g2 = builder.build_graph(jobs), builder.build_graph(list(reversed(jobs)))
    assert builder.to_mermaid(g1) == builder.to_mermaid(g2)
    assert builder.to_dot(g1) == builder.to_dot(g2)
    assert builder.to_json(g1, builder.analyze(g1)) == builder.to_json(g2, builder.analyze(g2))
    m = builder.to_mermaid(g1)
    assert m.startswith("graph LR\n") and "==>|tRunJob|" in m and 'class ' in m
    assert builder.to_dot(g1).startswith("digraph lineage {") and builder.to_dot(g1).rstrip().endswith("}")


def test_quotes_in_labels_are_escaped() -> None:
    m = builder.to_mermaid(builder.build_graph([job('we"ird')]))
    assert '"we#quot;ird"' in m


def test_focus_includes_inputs_of_jobs_started_by_trunjob() -> None:
    jobs = [job("parent", children=["kid"]), job("kid", reads=["SRC.X"], writes=["STG.Y"]), job("other", reads=["Z.Z"])]
    g = builder.focus(builder.build_graph(jobs), "parent")
    assert {d["label"] for _, d in g.nodes(data=True)} == {"parent", "kid", "SRC.X", "STG.Y"}
