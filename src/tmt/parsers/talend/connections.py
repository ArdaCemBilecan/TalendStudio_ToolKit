"""Which database connection does a component use? Resolved to the Studio *metadata connection name* (ODS_DB).

Talend keeps a reusable connection as a repository item (``metadata/connections/<Name>_0.1.item`` + ``.properties``).
A component points at it by id (``PROPERTY:REPOSITORY_PROPERTY_TYPE``); the human name is the ``label`` of that item's
``.properties`` file. Other components may instead share a ``t<DB>Connection`` component (``USE_EXISTING_CONNECTION``)
or carry the settings themselves (built-in). Passwords and user names are never read.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from lxml import etree

from tmt.models import Component, DbConnection, Resolution
from tmt.parsers.talend.components import _DB_RE
from tmt.parsers.talend.expressions import parse_expression

_XML = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, dtd_validation=False, huge_tree=False)
_PROPS = "{http://www.talend.org/properties}Property"
_DB_TYPES = {"JDBCInput", "JDBCOutput", "JDBCRow", "tOracleRow", "tDBRow"}
_NEWLINES = re.compile(r"[\r\n]+")
_CONNECTION_FIELDS = (
    "HOST", "PORT", "DBNAME", "LOCAL_SERVICE_NAME", "SCHEMA_DB", "USER", "PROPERTIES", "RAC_URL", "URL",
)
_CTX = re.compile(r"\bcontext\.([A-Za-z_]\w*)")


def _from_context_group(params: Mapping[str, str], labels: list[str]) -> str | None:
    """When a repository connection is dragged into a job, Studio creates a context group of the same name and fills the
    component with ``context.<Name>_Host`` ... If the repository link is gone but those variables remain, the name is
    still recoverable. This is an inference, and is reported as such (``kind="inferred"``)."""
    used = {m for key in _CONNECTION_FIELDS for m in _CTX.findall(params.get(key, ""))}
    for label in labels:  # longest label first, so ODS_DB_2 wins over ODS_DB
        if any(v.startswith(label + "_") for v in used):
            return label
    return None


def is_db_component(ctype: str) -> bool:
    return ctype in _DB_TYPES or bool(_DB_RE.match(ctype))


def project_root(item: Path) -> Path | None:
    for parent in list(item.resolve().parents)[:8]:
        if (parent / "talend.project").is_file():
            return parent
    return None


class RepositoryIndex:
    """id -> label of every repository item under ``<project>/metadata``, with the whole workspace as a fallback.

    The workspace fallback matters for a project cloned from another: ids are repeated, and a job may point at an
    item that lives in a sibling project.
    """

    def __init__(self) -> None:
        self._cache: dict[Path, dict[str, str]] = {}

    def _load(self, project: Path) -> dict[str, str]:
        if project in self._cache:
            return self._cache[project]
        names: dict[str, str] = {}
        for props in sorted((project / "metadata").glob("**/*.properties")):
            try:
                prop = etree.parse(str(props), _XML).getroot().find(_PROPS)
            except (etree.XMLSyntaxError, OSError):
                continue
            if prop is not None and prop.get("id") and prop.get("label"):
                names[str(prop.get("id"))] = str(prop.get("label"))
        self._cache[project] = names
        return names

    def labels(self, item: Path) -> list[str]:
        """Names of the repository items visible from ``item`` (own project first), longest first."""
        project = project_root(item)
        if project is None:
            return []
        found = set(self._load(project).values())
        try:
            for sibling in project.parent.iterdir():
                if sibling != project and (sibling / "talend.project").is_file():
                    found |= set(self._load(sibling).values())
        except OSError:
            pass
        return sorted(found, key=lambda s: (-len(s), s))

    def name_for(self, item: Path, repo_id: str) -> str | None:
        project = project_root(item)
        if project is None or not repo_id:
            return None
        if repo_id in self._load(project):
            return self._load(project)[repo_id]
        try:
            siblings = sorted(p for p in project.parent.iterdir() if p != project and (p / "talend.project").is_file())
        except OSError:
            return None
        for sibling in siblings:
            names = self._load(sibling)
            if repo_id in names:
                return names[repo_id]
        return None


def _shown(raw: str, context_values: Mapping[str, str | None]) -> str | None:
    e = parse_expression(raw, context_values)
    if e.resolution is Resolution.UNRESOLVED and e.value is None:
        return None if raw.strip() in ("", '""') else "?"
    value = e.value if e.resolution is Resolution.RESOLVED else (e.evaluated or raw)
    return (value or "").strip() or None


def describe(ctype: str, params: Mapping[str, str], item: Path, index: RepositoryIndex,
             context_values: Mapping[str, str | None]) -> DbConnection | None:
    """The connection a DB component uses, or None for components that are not database ones."""
    if not is_db_component(ctype):
        return None
    if params.get("USE_EXISTING_CONNECTION") == "true":
        return DbConnection(name="", kind="shared", detail=params.get("CONNECTION") or None)
    if params.get("PROPERTY:PROPERTY_TYPE") == "REPOSITORY":
        rid = params.get("PROPERTY:REPOSITORY_PROPERTY_TYPE", "").strip()
        label = index.name_for(item, rid)
        if label:
            return DbConnection(name=label, kind="repository", repo_id=rid)
        unknown = f"unknown repository connection ({rid or 'no id'})"
        return DbConnection(name=unknown, kind="repository", repo_id=rid or None)

    inferred = _from_context_group(params, index.labels(item))
    if inferred:
        return DbConnection(name=inferred, kind="inferred",
                            detail=f"inferred: the fields use the context variables {inferred}_*")
    pieces: list[str] = []
    for key in ("configuration.dataSet.dataStore.jdbcUrl", "URL", "RAC_URL"):
        v = params.get(key)
        if v and _shown(v, context_values) not in (None, "jdbc:"):
            pieces = [str(_shown(v, context_values))]
            break
    if not pieces:
        host = _shown(params.get("HOST", ""), context_values)
        db = _shown(params.get("LOCAL_SERVICE_NAME", "") or params.get("DBNAME", ""), context_values)
        pieces = [p for p in (host, db) if p]
    text = "/".join(pieces)
    if "$" in text or text == "?" or "?" in pieces:
        return DbConnection(name="unresolved (placeholder)", kind="unresolved", detail=text or None)
    if not text:
        return DbConnection(name="built-in (not specified)", kind="built_in")
    one_line = _NEWLINES.sub(" ", text)
    return DbConnection(name=f"built-in: {one_line}", kind="built_in", detail=text)


def resolve_shared(components: list[Component]) -> None:
    """A component that shares a ``t<DB>Connection`` takes that component's connection."""
    by_id = {c.id: c for c in components}
    for c in components:
        conn = c.connection
        if conn is None or conn.kind != "shared":
            continue
        target = by_id.get(conn.detail or "")
        if target is not None and target.connection is not None and target.connection.kind != "shared":
            c.connection = target.connection.model_copy(update={"detail": f"via {target.id}"})
        else:
            c.connection = DbConnection(name=f"shared connection {conn.detail or '?'} (not found in the job)",
                                        kind="unresolved", detail=conn.detail)
