"""The dependency explorer on the included demo project: the numbers a customer asks for, end to end."""

from __future__ import annotations

from pathlib import Path

import pytest

from tmt.config import load_config
from tmt.lineage.explore import explore, hierarchy, masters
from tmt.lineage.svg import to_svg
from tmt.scan import scan

DEMO = Path(__file__).resolve().parents[2] / "samples" / "talend_demo"


@pytest.fixture(scope="module")
def jobs() -> list:  # type: ignore[type-arg]
    result = scan(DEMO, config=load_config(None))
    assert not result.failures
    return result.jobs


def test_masters_are_listed_first(jobs: list) -> None:  # type: ignore[type-arg]
    rows = masters(jobs)
    assert [r["key"] for r in rows if r["is_master"]] == ["job_master_daily", "job_master_weekly"]
    assert rows[0]["key"] == "job_master_daily" and rows[0]["starts"] == 3


def test_hierarchy_follows_run_jobs_without_duplicates(jobs: list) -> None:  # type: ignore[type-arg]
    keys = [j.key for j in hierarchy(jobs, "job_master_daily")]
    assert keys[0] == "job_master_daily" and len(keys) == len(set(keys)) == 4
    with pytest.raises(KeyError):
        hierarchy(jobs, "nope")


def test_the_customer_example_numbers(jobs: list) -> None:  # type: ignore[type-arg]
    s = explore(jobs, "job_master_daily").summary
    assert (s["sources"], s["targets"], s["jobs"], s["sub_jobs"]) == (12, 10, 4, 3)
    assert [(c["name"], c["uses"]) for c in s["connections"]] == [("ODS_DB", 10), ("AKY_DB", 2)]
    assert s["distinct_connections"] == 2
    w = explore(jobs, "job_master_weekly").summary
    assert [(c["name"], c["uses"]) for c in w["connections"]] == [("ODS_DB", 5), ("AKY_DB", 1)]


def test_hover_details_count_sources_and_targets_per_node(jobs: list) -> None:  # type: ignore[type-arg]
    ex = explore(jobs, "job_master_daily")
    d = ex.details["job:job_stage_orders"]
    assert (d["own"]["sources"], d["own"]["targets"]) == (4, 4)
    assert {c["name"] for c in d["connections"]} == {"ODS_DB", "AKY_DB"}
    table = ex.details["table:ODS.CUSTOMERS"]
    assert table["type"] == "table" and table["n_readers"] == 1 and table["n_writers"] == 0


def test_related_jobs_are_added_only_when_asked(jobs: list) -> None:  # type: ignore[type-arg]
    assert len(explore(jobs, "job_master_daily").jobs) == 4
    more = explore(jobs, "job_master_daily", related=True)
    assert len(more.jobs) == 5 and more.summary["related_jobs"] == 1


def test_whole_workspace_has_no_master(jobs: list) -> None:  # type: ignore[type-arg]
    s = explore(jobs, None).summary
    assert s["master"] is None and s["jobs"] == len(jobs)


def test_interactive_svg_marks_nodes_and_edges(jobs: list) -> None:  # type: ignore[type-arg]
    ex = explore(jobs, "job_master_daily")
    svg = to_svg(ex.graph, interactive=True, highlight="job:job_master_daily")
    assert 'data-node="job:job_stage_orders"' in svg and 'tabindex="0"' in svg and "tmt-master" in svg
    assert "data-u=" in svg and "data-v=" in svg
    assert "data-node" not in to_svg(ex.graph)  # the static export stays plain


def test_master_rows_list_their_direct_children_for_the_repository_tree(jobs: list) -> None:  # type: ignore[type-arg]
    rows = {r["key"]: r for r in masters(jobs)}
    daily = rows["job_master_daily"]["children"]
    assert "job_stage_orders" in daily and "job_master_daily" not in daily
    assert rows["job_stage_orders"]["children"] == []
