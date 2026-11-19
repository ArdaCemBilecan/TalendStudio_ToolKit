"""Which metadata connection does a component use? Resolved by repository id, inferred from context variables, shared."""

from __future__ import annotations

from pathlib import Path

from tmt.models import Category, Component
from tmt.parsers.talend.connections import RepositoryIndex, describe, is_db_component, resolve_shared

PROPS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<xmi:XMI xmi:version="2.0" xmlns:xmi="http://www.omg.org/XMI" xmlns:TalendProperties="http://www.talend.org/properties">'
    '<TalendProperties:Property id="{rid}" label="{label}" version="0.1"/></xmi:XMI>\n'
)


def make_project(root: Path, name: str, connections: dict[str, str]) -> Path:
    project = root / name
    (project / "process").mkdir(parents=True)
    (project / "talend.project").write_text("<x/>", encoding="utf-8")
    meta = project / "metadata" / "connections"
    meta.mkdir(parents=True)
    for label, rid in connections.items():
        (meta / f"{label}_0.1.properties").write_text(PROPS.format(rid=rid, label=label), encoding="utf-8")
    return project


def item_in(project: Path) -> Path:
    item = project / "process" / "job_0.1.item"
    item.write_text("<x/>", encoding="utf-8")
    return item


def test_repository_link_gives_the_metadata_connection_name(tmp_path: Path) -> None:
    item = item_in(make_project(tmp_path, "P", {"ODS_DB": "id-1"}))
    c = describe("JDBCInput", {"PROPERTY:PROPERTY_TYPE": "REPOSITORY", "PROPERTY:REPOSITORY_PROPERTY_TYPE": "id-1"},
                 item, RepositoryIndex(), {})
    assert c is not None and (c.name, c.kind, c.repo_id) == ("ODS_DB", "repository", "id-1")


def test_unknown_repository_id_is_flagged_not_guessed(tmp_path: Path) -> None:
    item = item_in(make_project(tmp_path, "P", {"ODS_DB": "id-1"}))
    c = describe("JDBCInput", {"PROPERTY:PROPERTY_TYPE": "REPOSITORY", "PROPERTY:REPOSITORY_PROPERTY_TYPE": "nope"},
                 item, RepositoryIndex(), {})
    assert c is not None and c.name.startswith("unknown repository connection") and "ODS_DB" not in c.name


def test_a_cloned_project_finds_the_item_in_its_sibling(tmp_path: Path) -> None:
    make_project(tmp_path, "DEV", {"AKY_DB": "id-9"})
    item = item_in(make_project(tmp_path, "PROD", {}))
    c = describe("JDBCOutput", {"PROPERTY:PROPERTY_TYPE": "REPOSITORY", "PROPERTY:REPOSITORY_PROPERTY_TYPE": "id-9"},
                 item, RepositoryIndex(), {})
    assert c is not None and c.name == "AKY_DB" and c.kind == "repository"


def test_context_variables_named_after_a_connection_are_an_inference(tmp_path: Path) -> None:
    item = item_in(make_project(tmp_path, "P", {"ODS_DB": "id-1", "ODS_DB_2": "id-2"}))
    params = {"HOST": "context.ODS_DB_2_Host", "DBNAME": "context.ODS_DB_2_Sid"}
    c = describe("JDBCInput", params, item, RepositoryIndex(), {})
    assert c is not None and (c.name, c.kind) == ("ODS_DB_2", "inferred")  # the longest label wins


def test_built_in_and_placeholder_settings(tmp_path: Path) -> None:
    item = item_in(make_project(tmp_path, "P", {}))
    built_in = describe("tOracleInput", {"HOST": '"db1"', "DBNAME": '"SALES"'}, item, RepositoryIndex(), {})
    assert built_in is not None and built_in.kind == "built_in" and "db1" in built_in.name
    placeholder = describe("tOracleInput", {"HOST": '"$HOST"'}, item, RepositoryIndex(), {})
    assert placeholder is not None and placeholder.kind == "unresolved"
    empty = describe("tOracleInput", {}, item, RepositoryIndex(), {})
    assert empty is not None and empty.kind == "built_in" and "not specified" in empty.name


def test_non_database_components_have_no_connection(tmp_path: Path) -> None:
    item = item_in(make_project(tmp_path, "P", {}))
    assert not is_db_component("tFileInputDelimited")
    assert describe("tFileInputDelimited", {}, item, RepositoryIndex(), {}) is None


def test_shared_connection_takes_the_connection_components_name(tmp_path: Path) -> None:
    item = item_in(make_project(tmp_path, "P", {"ODS_DB": "id-1"}))
    index = RepositoryIndex()
    opener = describe("tOracleConnection", {"PROPERTY:PROPERTY_TYPE": "REPOSITORY",
                                            "PROPERTY:REPOSITORY_PROPERTY_TYPE": "id-1"}, item, index, {})
    user = describe("tOracleInput", {"USE_EXISTING_CONNECTION": "true", "CONNECTION": "tOracleConnection_1"},
                    item, index, {})
    lost = describe("tOracleInput", {"USE_EXISTING_CONNECTION": "true", "CONNECTION": "tOracleConnection_9"},
                    item, index, {})
    comps = [Component(id="tOracleConnection_1", type="tOracleConnection", category=Category.CONNECTION, implementation="classic", connection=opener),
             Component(id="a", type="tOracleInput", category=Category.INPUT, implementation="classic", connection=user),
             Component(id="b", type="tOracleInput", category=Category.INPUT, implementation="classic", connection=lost)]
    resolve_shared(comps)
    assert comps[1].connection is not None and comps[1].connection.name == "ODS_DB"
    assert comps[2].connection is not None and comps[2].connection.kind == "unresolved"
