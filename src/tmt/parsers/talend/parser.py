"""Talend Studio (Data Integration, 8.0.1) ``.item`` -> :class:`tmt.models.Job`.

Static analysis only: XML is parsed with entity/DTD resolution disabled and nothing extracted from
the job is ever executed. Unknown content is kept and flagged as an :class:`Issue`, never dropped.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from lxml import etree

from tmt.errors import ParseError
from tmt.models import (
    Column,
    Component,
    Connection,
    ConnKind,
    Expression,
    Issue,
    IssueCode,
    Job,
    Resolution,
    Schema,
    SourceRef,
    Subjob,
    TableRef,
)
from tmt.parsers.base import BaseParser
from tmt.parsers.talend.components import Ctx, Extraction, classify, extract
from tmt.parsers.talend.connections import RepositoryIndex, describe, resolve_shared
from tmt.parsers.talend.context import parse_contexts, values_for
from tmt.parsers.talend.expressions import parse_expression

log = logging.getLogger(__name__)

_XML = etree.XMLParser(
    resolve_entities=False, no_network=True, load_dtd=False, dtd_validation=False, huge_tree=False
)
_PROPS_NS = "{http://www.talend.org/properties}"
_FLOW_STYLE = {"0": ConnKind.FLOW, "8": ConnKind.LOOKUP, "10": ConnKind.MERGE}
_TRIGGERS = {ConnKind.ON_SUBJOB_OK, ConnKind.ON_COMPONENT_OK, ConnKind.RUN_IF}
_DATA_FLOW = {ConnKind.FLOW, ConnKind.LOOKUP, ConnKind.MERGE, ConnKind.UNIQUE, ConnKind.REJECT}
_LABEL_PLACEHOLDER = ("__PROCESS__", "__UNIQUE_NAME__")


def _conn_kind(connector: str, line_style: str | None) -> ConnKind:
    simple = {
        "SUBJOB_OK": ConnKind.ON_SUBJOB_OK,
        "COMPONENT_OK": ConnKind.ON_COMPONENT_OK,
        "RUN_IF": ConnKind.RUN_IF,
        "REJECT": ConnKind.REJECT,
        "UNIQUE": ConnKind.UNIQUE,
    }
    if connector in simple:
        return simple[connector]
    if connector in ("FLOW", "MAIN"):  # MAIN is what Component Kit components write
        return _FLOW_STYLE.get(line_style if line_style is not None else "0", ConnKind.UNKNOWN)
    return ConnKind.UNKNOWN


def _int(v: str | None) -> int | None:
    try:
        n = int(v) if v is not None else -1
    except ValueError:
        return None
    return None if n < 0 else n


def _columns(meta: etree._Element) -> Schema:
    return Schema(
        columns=[
            Column(
                name=c.get("name") or "",
                type=c.get("type") or "id_String",
                length=_int(c.get("length")),
                precision=_int(c.get("precision")),
                nullable=c.get("nullable", "true") == "true",
                is_key=c.get("key", "false") == "true",
                pattern=c.get("pattern") or None,
            )
            for c in meta.findall("column")
        ]
    )


class _Properties:
    def __init__(self, path: Path) -> None:
        self.id: str | None = None
        self.label: str | None = None
        self.version: str | None = None
        self.folder = ""
        self.tool_version: str | None = None
        self.found = False
        props = path.with_suffix(".properties")
        if not props.is_file():
            return
        try:
            root = etree.parse(str(props), _XML).getroot()
        except etree.XMLSyntaxError:
            return
        self.found = True
        prop = root.find(f"{_PROPS_NS}Property")
        if prop is not None:
            self.id, self.label, self.version = prop.get("id"), prop.get("label"), prop.get("version")
            extra = {a.get("key"): a.get("value") for a in prop.findall("additionalProperties")}
            self.tool_version = extra.get("modified_product_version") or extra.get("created_product_version")
        state = root.find(f"{_PROPS_NS}ItemState")
        if state is not None:
            self.folder = state.get("path") or ""


class TalendParser(BaseParser):
    name = "talend"

    def __init__(self, dialect: str | None = "oracle") -> None:
        self.dialect = dialect
        self._repository = RepositoryIndex()

    # ------------------------------------------------------------ BaseParser
    def can_parse(self, path: Path) -> bool:
        if path.suffix != ".item" or not path.is_file():
            return False
        try:
            return b"ProcessType" in path.read_bytes()[:4096]
        except OSError:
            return False

    def parse(self, path: Path, *, context: str | None = None, base: Path | None = None) -> Job:
        shown = _display_path(path, base)
        try:
            root = etree.parse(str(path), _XML).getroot()
        except (etree.XMLSyntaxError, OSError) as exc:
            raise ParseError(shown, f"cannot read XML: {exc}") from exc
        if etree.QName(root).localname != "ProcessType":
            raise ParseError(shown, "root element is not a Talend ProcessType")

        props = _Properties(path)
        issues: list[Issue] = []
        stem_name, _, stem_version = path.stem.rpartition("_")
        name = props.label or stem_name or path.stem
        version = props.version or stem_version or "0.1"
        if not props.found:
            issues.append(Issue(code=IssueCode.MISSING_METADATA, severity="warning",
                                message="no readable .properties file; name/version taken from the file name"))
        if root.get("jobType", "Standard") != "Standard":
            issues.append(Issue(code=IssueCode.UNSUPPORTED_CONSTRUCT, severity="warning",
                                message=f"jobType={root.get('jobType')!r} is outside v1 scope"))

        contexts = parse_contexts(root)
        default_ctx = root.get("defaultContext") or "Default"
        chosen = context or default_ctx
        if chosen not in contexts:
            issues.append(Issue(code=IssueCode.MISSING_METADATA, severity="info",
                                message=f"context group {chosen!r} not defined; using {default_ctx!r}"))
            chosen = default_ctx
        ctx_values = values_for(contexts, chosen)

        connections, conn_issues = self._connections(root, shown)
        issues += conn_issues
        lookup_labels: dict[str, set[str]] = {}
        for c in connections:
            if c.kind is ConnKind.LOOKUP:
                lookup_labels.setdefault(c.target, set()).add(c.label or c.name)

        components: list[Component] = []
        extractions: dict[str, Extraction] = {}
        seen: set[str] = set()
        for index, node in enumerate(root.findall("node"), start=1):
            comp, ext = self._component(node, index, shown, seen, ctx_values, lookup_labels, path)
            components.append(comp)
            extractions[comp.id] = ext
        resolve_shared(components)
        issues += [i for comp in components for i in comp.issues]

        reads = sorted((r for e in extractions.values() for r in e.reads), key=_ref_key)
        writes = sorted((w for e in extractions.values() for w in e.writes), key=_ref_key)
        for ref in (*reads, *writes):
            if ref.resolution is Resolution.CONTEXT_DEPENDENT:
                vars_ = ", ".join(ref.expression.referenced_context)
                known = ref.name is not None
                issues.append(Issue(
                    code=IssueCode.UNRESOLVED_CONTEXT,
                    severity="info" if known else "warning",
                    component_id=ref.component_id,
                    message=f"{ref.access} target {ref.expression.raw} depends on context ({vars_})"
                            + (f"; folded with group {chosen!r} to {_qualified(ref)}" if known else ""),
                ))

        subjobs = self._subjobs(root, components, connections)
        by_id = {c.id: c for c in components}
        for sj in subjobs:
            for cid in sj.component_ids:
                by_id[cid].subjob_id = sj.id

        children = sorted((e.child for e in extractions.values() if e.child),
                          key=lambda c: (c.name, c.id or ""))
        job = Job(
            tool_version=props.tool_version,
            repo_id=props.id,
            project=_project_label(path),
            name=name,
            version=version,
            path=shown,
            folder=props.folder,
            default_context=default_ctx,
            contexts=contexts,
            components=sorted(components, key=lambda c: c.id),
            connections=sorted(connections, key=lambda c: (c.source, c.target, c.name)),
            subjobs=subjobs,
            reads=reads,
            writes=writes,
            child_jobs=children,
            issues=sorted(issues, key=lambda i: (i.component_id or "", i.code.value, i.message)),
        )
        log.debug("parsed %s: %d components, %d connections", shown, len(components), len(connections))
        return job

    # ------------------------------------------------------------ pieces
    def _connections(self, root: etree._Element, shown: str) -> tuple[list[Connection], list[Issue]]:
        out: list[Connection] = []
        issues: list[Issue] = []
        for c in root.findall("connection"):
            params = {p.get("name"): p.get("value") or "" for p in c.findall("elementParameter")}
            label = c.get("label")
            name = params.get("UNIQUE_NAME") or label or f"conn_{len(out) + 1}"
            kind = _conn_kind(c.get("connectorName") or "", c.get("lineStyle"))
            source = c.get("source") or ""
            if kind is ConnKind.UNKNOWN:
                issues.append(Issue(
                    code=IssueCode.UNKNOWN_CONNECTOR, severity="warning", component_id=source,
                    message=f"connection {name!r}: connectorName={c.get('connectorName')!r} "
                            f"lineStyle={c.get('lineStyle')!r} is not recognised"))
            condition: Expression | None = None
            if kind is ConnKind.RUN_IF:
                condition = parse_expression(params.get("CONDITION", ""))
            meta = c.get("metaname")
            out.append(Connection(
                name=name, source=source, target=c.get("target") or "", kind=kind, label=label,
                source_output=meta if meta and meta != source else None,
                condition=condition, source_ref=SourceRef(file=shown, line=c.sourceline),
            ))
        return out, issues

    def _component(
        self, node: etree._Element, index: int, shown: str, seen: set[str],
        ctx_values: dict[str, str | None], lookup_labels: dict[str, set[str]], item: Path,
    ) -> tuple[Component, Extraction]:
        ctype = node.get("componentName") or "unknown"
        params: dict[str, str] = {}
        visible: dict[str, str] = {}
        tables: dict[str, list[dict[str, str]]] = {}
        for p in node.findall("elementParameter"):
            pname, pfield = p.get("name") or "", p.get("field")
            if pfield == "TABLE":
                tables[pname] = _table_rows(p)
                continue
            if pfield == "PASSWORD":  # secrets are never copied
                continue
            value = p.get("value") or ""
            params[pname] = value
            hidden = p.get("show") == "false" or pname in ("UNIQUE_NAME", "LABEL", "CONNECTION_FORMAT")
            if not hidden and value not in ("", '""'):
                visible[pname] = value

        issues: list[Issue] = []
        uid = params.get("UNIQUE_NAME") or f"{ctype}_unnamed{index}"
        if not params.get("UNIQUE_NAME"):
            issues.append(Issue(code=IssueCode.MISSING_METADATA, severity="warning", component_id=uid,
                                message="node has no UNIQUE_NAME; id generated"))
        while uid in seen:
            uid += "_dup"
        seen.add(uid)

        cls = classify(ctype)
        if not cls.supported:
            issues.append(Issue(code=IssueCode.UNSUPPORTED_COMPONENT, severity="warning", component_id=uid,
                                message=f"{ctype} is not supported in v1; recorded but not analysed"))
        elif not cls.verified:
            issues.append(Issue(code=IssueCode.UNVERIFIED_COMPONENT, severity="info", component_id=uid,
                                message=f"{ctype} is handled by a generic pattern; no real Studio sample "
                                        "has confirmed its parameters yet"))
        for key, what in (("PROPERTY:PROPERTY_TYPE", "connection"), ("SCHEMA:SCHEMA_TYPE", "schema")):
            if params.get(key) == "REPOSITORY":
                issues.append(Issue(code=IssueCode.UNRESOLVED_REPOSITORY_REF, severity="info", component_id=uid,
                                    message=f"{what} comes from a repository item that v0.1 does not resolve"))

        ext = extract(Ctx(uid=uid, ctype=ctype, node=node, params=params, context_values=ctx_values,
                          dialect=self.dialect, lookup_labels=lookup_labels.get(uid, set()), tables=tables),
                  cls.category)
        issues += ext.issues

        schemas: dict[str, Schema] = {}
        for meta in node.findall("metadata"):
            key = (meta.get("name") or "") if ctype == "tMap" else (meta.get("connector") or "FLOW")
            if meta.findall("column") or ctype == "tMap":
                schemas[key] = _columns(meta)
        label = params.get("LABEL", "").strip()
        comp = Component(
            id=uid, type=ctype, category=cls.category, implementation=cls.implementation,
            label=None if not label or label in _LABEL_PLACEHOLDER else label,
            params=dict(sorted(visible.items())),
            schemas=dict(sorted(schemas.items())),
            connection=describe(ctype, params, item, self._repository, ctx_values),
            tmap=ext.tmap, aggregate=ext.aggregate, unique_keys=ext.unique_keys, sort_keys=ext.sort_keys,
            sql=ext.sql, code=ext.code, supported=cls.supported,
            issues=sorted(issues, key=lambda i: (i.code.value, i.message)),
            source_ref=SourceRef(file=shown, line=node.sourceline),
        )
        return comp, ext

    def _subjobs(
        self, root: etree._Element, components: list[Component], connections: list[Connection],
    ) -> list[Subjob]:
        parent = {c.id: c.id for c in components}

        def find(a: str) -> str:
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        for c in connections:
            if c.kind in _DATA_FLOW and c.source in parent and c.target in parent:
                parent[find(c.target)] = find(c.source)
        groups: dict[str, list[str]] = {}
        for comp in components:
            groups.setdefault(find(comp.id), []).append(comp.id)

        declared = [sj.find("elementParameter[@name='UNIQUE_NAME']") for sj in root.findall("subjob")]
        declared_ids = {e.get("value") for e in declared if e is not None and e.get("value") in parent}
        out: list[Subjob] = []
        for members in groups.values():
            members.sort()
            chosen = next((m for m in members if m in declared_ids), members[0])
            out.append(Subjob(id=chosen, component_ids=members))
        return sorted(out, key=lambda s: s.id)


def _project_label(item: Path) -> str | None:
    """Technical label of the Studio project the item lives in (from the nearest ``talend.project``)."""
    for parent in list(item.resolve().parents)[:8]:
        proj = parent / "talend.project"
        if proj.is_file():
            try:
                m = re.search(r'technicalLabel="([^"]+)"', proj.read_text(encoding="utf-8", errors="replace")[:4000])
            except OSError:
                return None
            return m.group(1) if m else None
    return None


def _table_rows(param: etree._Element) -> list[dict[str, str]]:
    """Group a TABLE parameter's flat ``elementValue`` list into rows (a row ends when a ref repeats)."""
    rows: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for ev in param.findall("elementValue"):
        ref = ev.get("elementRef") or ""
        if ref in current:
            rows.append(current)
            current = {}
        current[ref] = ev.get("value") or ""
    if current:
        rows.append(current)
    return rows


def _qualified(r: TableRef) -> str:
    return ".".join(p for p in (r.database, r.db_schema, r.name) if p)


def _ref_key(r: TableRef) -> tuple[str, str, str]:
    return (r.component_id, r.access, r.expression.raw)


def _display_path(path: Path, base: Path | None) -> str:
    if base is not None:
        try:
            return path.resolve().relative_to(base.resolve()).as_posix()
        except ValueError:
            pass
    return path.name
