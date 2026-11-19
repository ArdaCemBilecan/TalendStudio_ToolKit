from __future__ import annotations

from tmt.config import Config, Weights
from tmt.models import (
    Category,
    ChildJobRef,
    ColumnMapping,
    Component,
    Expression,
    Job,
    Resolution,
    TableRef,
    TMapInput,
    TMapLogic,
    TMapOutput,
)
from tmt.scoring.complexity import score

CFG = Config()


def comp(i: int, ctype: str = "tLogRow", category: Category = Category.LOG, supported: bool = True,
         tmap: TMapLogic | None = None) -> Component:
    return Component(id=f"c_{i}", type=ctype, category=category, implementation="classic", supported=supported,
                     tmap=tmap)


def expr(raw: str, calls: list[str] | None = None) -> Expression:
    return Expression(raw=raw, resolution=Resolution.UNRESOLVED, java_calls=calls or [])


def job(**kw) -> Job:  # type: ignore[no-untyped-def]
    return Job(name="j", version="0.1", path="j.item", **kw)


def factors(j: Job, cfg: Config = CFG) -> dict[str, int]:
    return {r.factor: r.points for r in score(j, cfg).reasons}


def test_each_component_is_one_point() -> None:
    c = score(job(components=[comp(i) for i in range(4)]), CFG)
    assert (c.score, c.level) == (4, "easy")


def test_empty_job_scores_zero() -> None:
    assert score(job(), CFG).score == 0


def test_tmap_joins_and_java_expressions() -> None:
    tm = TMapLogic(
        inputs=[TMapInput(name="row1", role="main"), TMapInput(name="row2", role="lookup", join_type="inner"),
                TMapInput(name="row3", role="lookup", join_type="left")],
        outputs=[TMapOutput(name="out", filter=expr("row1.a.equals(1)", ["equals"]), mappings=[
            ColumnMapping(target="a", expression=expr("StringHandling.UPCASE(row1.a)", ["StringHandling.UPCASE"])),
            ColumnMapping(target="b", expression=expr("row1.b")),
        ])],
    )
    f = factors(job(components=[comp(1, "tMap", Category.TRANSFORM, tmap=tm)]))
    assert f == {"component": 1, "tmap_join": 4, "tmap_java_expression": 6}


def test_custom_java_adds_ten_and_forces_hard() -> None:
    j = job(components=[comp(1, "tJava", Category.CUSTOM_CODE)])
    c = score(j, CFG)
    assert c.score == 11 and c.level == "hard"  # 11 would be "medium" without the override
    assert any(r.factor == "forced_hard" for r in c.reasons)


def test_custom_routine_call_counts_as_custom_code() -> None:
    tm = TMapLogic(outputs=[TMapOutput(name="o", mappings=[ColumnMapping(
        target="a", expression=expr("routines.MyUtil.clean(row1.a)", ["clean"]))])])
    c = score(job(components=[comp(1, "tMap", Category.TRANSFORM, tmap=tm)]), CFG)
    assert c.level == "hard"
    f = {r.factor: r.points for r in c.reasons}
    assert f["custom_code"] == 10 and "tmap_java_expression" not in f  # not double counted


def test_unsupported_children_and_unresolved_context() -> None:
    ref = TableRef(name=None, expression=Expression(raw="context.t", resolution=Resolution.CONTEXT_DEPENDENT),
                   resolution=Resolution.CONTEXT_DEPENDENT, access="write", action="insert", component_id="c_1",
                   via="table_param")
    plain = ref.model_copy(update={"resolution": Resolution.RESOLVED, "name": "T"})
    j = job(components=[comp(1, "tFoo", Category.OTHER, supported=False)],
            child_jobs=[ChildJobRef(name="a"), ChildJobRef(name="b")], writes=[ref, plain])
    assert factors(j) == {"component": 1, "unsupported_component": 5, "run_job_child": 4, "unresolved_context": 2}


def test_level_boundaries() -> None:
    def level(n: int) -> str:
        return score(job(components=[comp(i) for i in range(n)]), CFG).level

    assert [level(10), level(11), level(25), level(26)] == ["easy", "medium", "medium", "hard"]


def test_weights_and_thresholds_are_configurable() -> None:
    cfg = Config.model_validate({"scoring": {"weights": {"component": 5}, "thresholds": {"easy_max": 3,
                                                                                         "medium_max": 6}}})
    c = score(job(components=[comp(1), comp(2)]), cfg)
    assert (c.score, c.level) == (10, "hard")
    assert score(job(components=[comp(1)]), cfg).level == "medium"


def test_zero_weight_removes_the_factor_from_reasons() -> None:
    cfg = Config(scoring={"weights": Weights(component=0)})  # type: ignore[arg-type]
    assert score(job(components=[comp(1)]), cfg).reasons == []


def test_reasons_sum_to_score_and_are_reproducible() -> None:
    j = job(components=[comp(1, "tJava", Category.CUSTOM_CODE), comp(2)], child_jobs=[ChildJobRef(name="a")])
    a, b = score(j, CFG), score(j, CFG)
    assert a == b and a.score == sum(r.points for r in a.reasons)
