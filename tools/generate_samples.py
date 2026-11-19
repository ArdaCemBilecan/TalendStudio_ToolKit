"""Generate synthetic Talend Studio 8.0.1 sample jobs (.item + .properties).

Design rule: never invent Talend parameter names. Every component node is cloned from a
template that Talend Studio itself wrote (tools/templates/, see extract_templates.py) and
only the values that define the scenario are changed. Anything Studio has never been
observed to write is NOT generated here; those jobs are built by hand in Studio.

Usage:
    python tools/generate_samples.py --out samples/talend/process
    python tools/generate_samples.py --out <workspace>/DEV/process --project DEV \
        --author-id _XXXXXXXXXXXXXXXXXXXXXX --no-overwrite

Output is deterministic: same arguments -> byte-identical files.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import quoteattr

from lxml import etree

TEMPLATES = Path(__file__).parent / "templates"
XMI = "http://www.omg.org/XMI"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
MAPPER = "http://www.talend.org/mapper"
TFILE = "platform:/resource/org.talend.model/model/TalendFile.xsd"
NSMAP = {"xmi": XMI, "xsi": XSI, "TalendMapper": MAPPER, "talendfile": TFILE}
TACOKIT = {"JDBCInput", "JDBCOutput"}  # these write connectorName="MAIN" instead of "FLOW"
# Jobs whose committed file was written by Studio itself (ground truth). Never regenerated.
STUDIO_WRITTEN = {"job_prejob_postjob"}
TRIGGERS = {"SUBJOB_OK", "RUN_IF", "COMPONENT_OK"}
TRIGGER_STYLE = {"SUBJOB_OK": "1", "RUN_IF": "1", "COMPONENT_OK": "3"}  # all three seen in Studio-saved files


def eid(*parts: str) -> str:
    """Deterministic EMF-style id: '_' + 22 url-safe base64 chars."""
    digest = hashlib.sha1("|".join(parts).encode()).digest()
    return "_" + base64.urlsafe_b64encode(digest).decode()[:22]


def q(s: str) -> str:
    """Java string literal as Talend stores it (the XML layer escapes the quotes)."""
    return '"' + s.replace('"', '\\"') + '"'


# ---------------------------------------------------------------- schema helpers
@dataclass(frozen=True)
class Col:
    name: str
    type: str = "id_String"
    key: bool = False
    length: int = -1
    precision: int = -1
    nullable: bool = True
    pattern: str = ""


def S(name: str, length: int = 50, key: bool = False) -> Col:
    return Col(name, "id_String", key, length, nullable=not key)


def I(name: str, key: bool = False) -> Col:  # noqa: E743
    return Col(name, "id_Integer", key, 10, nullable=not key)


def D(name: str) -> Col:
    return Col(name, "id_Date", length=-1, pattern='"dd-MM-yyyy"')


def N(name: str) -> Col:
    return Col(name, "id_BigDecimal", length=12, precision=2)


def B(name: str) -> Col:
    return Col(name, "id_Boolean", length=-1)


CUSTOMERS = [I("customer_id", True), S("first_name"), S("last_name"), S("email", 100),
             S("country_code", 2), D("created_at"), B("is_active")]
ORDERS = [I("order_id", True), I("customer_id"), D("order_date"), N("amount"), S("status", 20)]
COUNTRIES = [S("country_code", 2, True), S("country_name", 60)]


def col_xml(c: Col) -> etree._Element:
    return etree.Element("column", {
        "comment": "", "key": str(c.key).lower(), "length": str(c.length),
        "name": c.name, "nullable": str(c.nullable).lower(), "pattern": c.pattern,
        "precision": str(c.precision), "sourceType": "", "type": c.type,
        "originalLength": str(c.length), "usefulColumn": "true"})


def metadata(connector: str, name: str, cols: list[Col], reject: bool = False) -> etree._Element:
    m = etree.Element("metadata", {"connector": connector, "name": name})
    for c in cols:
        m.append(col_xml(c))
    if reject:
        for n in ("errorCode", "errorMessage"):
            m.append(etree.Element("column", {
                "defaultValue": "", "key": "false", "length": "255", "name": n,
                "nullable": "true", "precision": "0", "sourceType": "", "type": "id_String",
                "originalLength": "-1", "usefulColumn": "true"}))
    return m


# ---------------------------------------------------------------- template helpers
_PARSER = etree.XMLParser(remove_blank_text=True)


def tpl(name: str) -> etree._Element:
    return deepcopy(etree.parse(str(TEMPLATES / f"{name}.xml"), _PARSER).getroot())


def setp(node: etree._Element, name: str, value: str, field_: str = "TEXT") -> None:
    for p in node.findall("elementParameter"):
        if p.get("name") == name:
            p.set("value", value)
            return
    etree.SubElement(node, "elementParameter", {"field": field_, "name": name, "value": value})


def delp(node: etree._Element, name: str) -> None:
    for p in node.findall("elementParameter"):
        if p.get("name") == name:
            node.remove(p)


def table(node: etree._Element, name: str, rows: list[dict[str, str]]) -> None:
    for p in node.findall("elementParameter"):
        if p.get("name") == name:
            for kid in list(p):
                p.remove(kid)
            i = 0
            for row in rows:
                for ref, val in row.items():
                    etree.SubElement(p, "elementValue",
                                     {"elementRef": ref, "value": val, "id": str(i)})
                    i += 1
            return
    raise KeyError(name)


# ---------------------------------------------------------------- job builder
@dataclass
class NodeSpec:
    uid: str
    comp: str
    el: etree._Element
    cols: list[Col] = field(default_factory=list)


@dataclass
class ConnSpec:
    src: str
    tgt: str
    kind: str  # main | lookup | merge | unique | SUBJOB_OK | RUN_IF
    label: str
    metaname: str
    uname: str
    condition: str = ""


class JobBuilder:
    def __init__(self, name: str, project: str, contexts: dict[str, dict[str, tuple[str, str]]]
                 | None = None) -> None:
        self.name, self.project = name, project
        self.contexts = contexts or {"Default": {}}
        self.nodes: list[NodeSpec] = []
        self.conns: list[ConnSpec] = []
        self._rows = 0
        self._trig = 0

    # -- nodes
    def _add(self, comp: str, uid: str, x: int, y: int, label: str = "", cols: list[Col] | None = None,
             tname: str | None = None) -> etree._Element:
        el = tpl(tname or comp)
        el.set("posX", str(x))
        el.set("posY", str(y))
        setp(el, "UNIQUE_NAME", uid)
        if label:
            setp(el, "LABEL", label)
        self.nodes.append(NodeSpec(uid, comp, el, cols or []))
        return el

    def jdbc_input(self, uid: str, x: int, y: int, label: str, cols: list[Col], query: str,
                   url: str = q("jdbc:oracle:thin:@//localhost:1521/XEPDB1"),
                   user: str = q("demo_user"), password: str = q("")) -> None:
        el = self._add("JDBCInput", uid, x, y, label, cols)
        setp(el, "configuration.dataSet.sqlQuery", query, "MEMO_SQL")
        setp(el, "configuration.dataSet.dataStore.jdbcUrl", url)
        setp(el, "configuration.dataSet.dataStore.userId", user)
        setp(el, "configuration.dataSet.dataStore.password", password, "PASSWORD")

    def jdbc_output(self, uid: str, x: int, y: int, label: str, cols: list[Col], table_expr: str,
                    action: str = "INSERT", clear: bool = False,
                    url: str = q("jdbc:oracle:thin:@//localhost:1521/XEPDB1"),
                    user: str = q("demo_user"), password: str = q("")) -> None:
        el = self._add("JDBCOutput", uid, x, y, label, cols)
        setp(el, "configuration.dataSet.tableName", table_expr, "TACOKIT_VALUE_SELECTION")
        setp(el, "configuration.dataAction", action, "CLOSED_LIST")
        setp(el, "configuration.clearData", str(clear).lower(), "CHECK")
        setp(el, "configuration.dataSet.dataStore.jdbcUrl", url)
        setp(el, "configuration.dataSet.dataStore.userId", user)
        setp(el, "configuration.dataSet.dataStore.password", password, "PASSWORD")
        setp(el, "QUERY", "", "MEMO_SQL")

    def excel_input(self, uid: str, x: int, y: int, label: str, cols: list[Col], path: str) -> None:
        el = self._add("tFileInputExcel", uid, x, y, label, cols)
        setp(el, "PROPERTY:PROPERTY_TYPE", "BUILT_IN", "PROPERTY_TYPE")
        delp(el, "PROPERTY:REPOSITORY_PROPERTY_TYPE")
        setp(el, "SCHEMA:SCHEMA_TYPE", "BUILT_IN", "SCHEMA_TYPE")
        delp(el, "SCHEMA:REPOSITORY_SCHEMA_TYPE")
        setp(el, "FILENAME", q(path), "FILE")
        table(el, "TRIMSELECT", [{"SCHEMA_COLUMN": c.name, "TRIM": "false"} for c in cols])
        table(el, "DATESELECT", [{"SCHEMA_COLUMN": c.name, "CONVERTDATE": "false",
                                  "PATTERN": q("MM-dd-yyyy")} for c in cols])

    def log_row(self, uid: str, x: int, y: int, cols: list[Col]) -> None:
        el = self._add("tLogRow", uid, x, y, "", cols)
        table(el, "LENGTHS", [{"SCHEMA_COLUMN": c.name, "LENGTH": "10"} for c in cols])

    def fixed_flow(self, uid: str, x: int, y: int, cols: list[Col], values: list[str]) -> None:
        el = self._add("tFixedFlowInput", uid, x, y, "", cols)
        setp(el, "NB_ROWS", "1")
        table(el, "VALUES", [{"SCHEMA_COLUMN": c.name, "VALUE": v} for c, v in zip(cols, values, strict=True)])

    def java(self, uid: str, x: int, y: int, code: str) -> None:
        el = self._add("tJava", uid, x, y)
        setp(el, "CODE", code, "MEMO_EDITOR_JAVA")

    def aggregate(self, uid: str, x: int, y: int, cols: list[Col], group: list[str],
                  ops: list[tuple[str, str, str]]) -> None:
        el = self._add("tAggregateRow", uid, x, y, "", cols)
        table(el, "GROUPBYS", [{"INPUT_COLUMN": g, "OUTPUT_COLUMN": g} for g in group])
        table(el, "OPERATIONS", [{"OUTPUT_COLUMN": o, "FUNCTION": f, "INPUT_COLUMN": i}
                                 for o, f, i in ops])

    def unite(self, uid: str, x: int, y: int, cols: list[Col]) -> None:
        self._add("tUnite", uid, x, y, "", cols)

    def uniq(self, uid: str, x: int, y: int, cols: list[Col], keys: list[str]) -> None:
        el = self._add("tUniqRow", uid, x, y, "", cols)
        table(el, "UNIQUE_KEY", [{"SCHEMA_COLUMN": c.name, "KEY_ATTRIBUTE": str(c.name in keys).lower(),
                                  "CASE_SENSITIVE": "false"} for c in cols])

    def oracle_row(self, uid: str, x: int, y: int, label: str, query: str) -> None:
        el = self._add("tOracleRow", uid, x, y, label)
        setp(el, "PROPERTY:PROPERTY_TYPE", "BUILT_IN", "TECHNICAL")
        delp(el, "PROPERTY:REPOSITORY_PROPERTY_TYPE")
        for name, val in (("DBNAME", "context.db_service"), ("LOCAL_SERVICE_NAME", "context.db_service"),
                          ("SCHEMA_DB", "context.db_schema"), ("USER", "context.db_user")):
            setp(el, name, val)
        setp(el, "PASS", "context.db_password", "PASSWORD")
        setp(el, "QUERY", query, "MEMO_SQL")

    # ---- UNVERIFIED components: written from knowledge of Talend, NOT cloned from a Studio file.
    # Open each job in Studio and re-save it; the Studio-written file then replaces ours.
    def _raw(self, comp: str, version: str, uid: str, x: int, y: int,
             params: list[tuple[str, str, str]], cols: list[Col] | None = None) -> etree._Element:
        el = etree.Element("node", {"componentName": comp, "componentVersion": version,
                                    "offsetLabelX": "0", "offsetLabelY": "0",
                                    "posX": str(x), "posY": str(y)})
        etree.SubElement(el, "elementParameter", {"field": "TEXT", "name": "UNIQUE_NAME",
                                                  "value": uid, "show": "false"})
        for fld, name, value in params:
            etree.SubElement(el, "elementParameter", {"field": fld, "name": name, "value": value})
        etree.SubElement(el, "elementParameter", {"field": "TEXT", "name": "CONNECTION_FORMAT", "value": "row"})
        self.nodes.append(NodeSpec(uid, comp, el, cols or []))
        return el

    def delimited_input(self, uid: str, x: int, y: int, cols: list[Col], path: str) -> None:
        self._raw("tFileInputDelimited", "0.102", uid, x, y, [
            ("PROPERTY_TYPE", "PROPERTY:PROPERTY_TYPE", "BUILT_IN"),
            ("SCHEMA_TYPE", "SCHEMA:SCHEMA_TYPE", "BUILT_IN"),
            ("FILE", "FILENAME", q(path)), ("CHECK", "CSV_OPTION", "false"),
            ("TEXT", "ROWSEPARATOR", q("\\n")), ("TEXT", "FIELDSEPARATOR", q(";")),
            ("TEXT", "HEADER", "1"), ("TEXT", "FOOTER", "0"), ("TEXT", "LIMIT", ""),
            ("CHECK", "REMOVE_EMPTY_ROW", "false"), ("CHECK", "UNCOMPRESS", "false"),
            ("CHECK", "DIE_ON_ERROR", "false"), ("ENCODING_TYPE", "ENCODING", q("UTF-8"))], cols)

    def delimited_output(self, uid: str, x: int, y: int, cols: list[Col], path: str) -> None:
        self._raw("tFileOutputDelimited", "0.102", uid, x, y, [
            ("PROPERTY_TYPE", "PROPERTY:PROPERTY_TYPE", "BUILT_IN"),
            ("FILE", "FILENAME", q(path)), ("TEXT", "ROWSEPARATOR", q("\\n")),
            ("TEXT", "FIELDSEPARATOR", q(";")), ("CHECK", "APPEND", "false"),
            ("CHECK", "INCLUDEHEADER", "true"), ("CHECK", "COMPRESS", "false"),
            ("ENCODING_TYPE", "ENCODING", q("UTF-8"))], cols)

    def sort_row(self, uid: str, x: int, y: int, cols: list[Col],
                 criteria: list[tuple[str, str, str]]) -> None:
        el = self._raw("tSortRow", "0.101", uid, x, y, [("CHECK", "EXTERNAL", "false")], cols)
        p = etree.SubElement(el, "elementParameter", {"field": "TABLE", "name": "CRITERIA"})
        i = 0
        for col, sort, order in criteria:
            for ref, val in (("COLNAME", col), ("SORT", sort), ("ORDER", order)):
                etree.SubElement(p, "elementValue", {"elementRef": ref, "value": val, "id": str(i)})
                i += 1

    def java_row(self, uid: str, x: int, y: int, cols: list[Col], code: str) -> None:
        self._raw("tJavaRow", "0.101", uid, x, y, [
            ("MEMO_EDITOR_JAVA", "CODE", code), ("MEMO_EDITOR_IMPORT", "IMPORT", "//import java.util.List;")], cols)

    def prejob(self, uid: str, x: int, y: int) -> None:
        self._add("tPrejob", uid, x, y)

    def postjob(self, uid: str, x: int, y: int) -> None:
        self._add("tPostjob", uid, x, y)

    def run_if(self, src: str, tgt: str, condition: str) -> None:
        self._trig += 1
        self.conns.append(ConnSpec(src, tgt, "RUN_IF", f"If{self._trig}", src, f"If{self._trig}", condition))

    def run_job(self, uid: str, x: int, y: int, child: str) -> None:
        el = self._add("tRunJob", uid, x, y)
        setp(el, "PROCESS", child, "PROCESS_TYPE")
        setp(el, "PROCESS:PROCESS_TYPE_PROCESS", f"{self.project}:{job_id(child)}", "TECHNICAL")
        setp(el, "LABEL", "__PROCESS__")

    def tmap(self, uid: str, x: int, y: int, inputs: list[dict], outputs: list[dict]) -> None:
        """inputs: {name, cols, lookup, inner, join{col: expr}};
        outputs: {name, entries[(col, expr)], cols, filter}."""
        el = self._add("tMap", uid, x, y)
        nd = etree.SubElement(el, "nodeData", {f"{{{XSI}}}type": "TalendMapper:MapperData"})
        etree.SubElement(nd, "uiProperties", {"shellMaximized": "true"})
        etree.SubElement(nd, "varTables", {"sizeState": "INTERMEDIATE", "name": "Var", "minimized": "true"})
        for o in outputs:
            attrs = {"sizeState": "INTERMEDIATE", "name": o["name"]}
            if o.get("filter"):
                attrs["expressionFilter"] = o["filter"]
                attrs["activateExpressionFilter"] = "true"
            ot = etree.SubElement(nd, "outputTables", attrs)
            by_name = {c.name: c for c in o["cols"]}
            for cname, expr in o["entries"]:
                etree.SubElement(ot, "mapperTableEntries", {
                    "name": cname, "expression": expr, "type": by_name[cname].type, "nullable": "true"})
        for i in inputs:
            attrs = {"sizeState": "INTERMEDIATE", "name": i["name"], "matchingMode": "UNIQUE_MATCH",
                     "lookupMode": "LOAD_ONCE"}
            if i.get("inner"):
                attrs["innerJoin"] = "true"
            it = etree.SubElement(nd, "inputTables", attrs)
            for c in i["cols"]:
                a = {"name": c.name, "type": c.type, "nullable": "true"}
                if i.get("lookup"):
                    a["expression"] = i.get("join", {}).get(c.name, "")
                etree.SubElement(it, "mapperTableEntries", a)
        self.nodes[-1].cols = []  # tMap metadata is per output, handled in _metadata_for
        self.nodes[-1].el.set("_outputs", "|".join(o["name"] for o in outputs))
        self._tmap_out = getattr(self, "_tmap_out", {})
        self._tmap_out[uid] = outputs

    # -- connections
    def flow(self, src: str, tgt: str, kind: str = "main", label: str | None = None,
             metaname: str | None = None) -> str:
        self._rows += 1
        uname = f"row{self._rows}"
        self.conns.append(ConnSpec(src, tgt, kind, label or uname, metaname or src, uname))
        return label or uname

    def on_ok(self, src: str, tgt: str) -> None:
        self._trig += 1
        self.conns.append(ConnSpec(src, tgt, "SUBJOB_OK", "OnSubjobOk", src, f"OnSubjobOk{self._trig}"))

    def on_component_ok(self, src: str, tgt: str) -> None:
        self._trig += 1
        self.conns.append(ConnSpec(src, tgt, "COMPONENT_OK", "OnComponentOk", src, f"OnComponentOk{self._trig}"))

    # -- assembly
    def _comp(self, uid: str) -> str:
        return next(n.comp for n in self.nodes if n.uid == uid)

    def _metadata_for(self, n: NodeSpec) -> list[etree._Element]:
        c = n.comp
        if c == "tMap":
            return [metadata("FLOW", o["name"], o["cols"]) for o in self._tmap_out[n.uid]]
        if c == "tUniqRow":
            return [metadata("UNIQUE", n.uid, n.cols)]
        if c in ("tFileInputExcel", "tFileInputDelimited"):
            return [metadata("FLOW", n.uid, n.cols), metadata("REJECT", "REJECT", n.cols, reject=True)]
        if c == "tPrejob":
            return []  # Studio writes no metadata block for tPrejob (but does for tPostjob)
        if c == "tOracleRow":
            return [metadata("FLOW", n.uid, []), metadata("REJECT", "REJECT", [], reject=True)]
        return [metadata("FLOW", n.uid, n.cols)]

    def _subjobs(self) -> list[str]:
        parent = {n.uid: n.uid for n in self.nodes}

        def find(a: str) -> str:
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        for cn in self.conns:
            if cn.kind not in TRIGGERS:
                parent[find(cn.tgt)] = find(cn.src)
        seen, out = set(), []
        for n in self.nodes:
            r = find(n.uid)
            if r not in seen:
                seen.add(r)
                out.append(next(m.uid for m in self.nodes if find(m.uid) == r))
        return out

    def build(self) -> bytes:
        root = etree.Element(f"{{{TFILE}}}ProcessType", nsmap=NSMAP, attrib={
            f"{{{XMI}}}version": "2.0", "defaultContext": "Default", "jobType": "Standard"})
        for cname, params in self.contexts.items():
            ctx = etree.SubElement(root, "context",
                                   {"confirmationNeeded": "false", "hide": "false", "name": cname})
            for pname, (ptype, value) in params.items():
                etree.SubElement(ctx, "contextParameter", {
                    "comment": "", "name": pname, "prompt": f"{pname}?", "promptNeeded": "false",
                    "type": ptype, "value": value, "internalId": eid(self.name, cname, pname)})
        params_el = tpl("_job_parameters")
        for p in params_el.findall("elementParameter"):
            if p.get("name") == "FILENAME_STATS":
                p.set("value", q(f"{self.name}_Logs.txt"))
        root.append(params_el)

        incoming = {c.tgt for c in self.conns}
        for n in self.nodes:
            el = n.el
            if "_outputs" in el.attrib:
                del el.attrib["_outputs"]
            for md in self._metadata_for(n):
                el.append(md)
            if any(p.get("name") == "START" for p in el.findall("elementParameter")):
                setp(el, "START", str(n.uid not in incoming).lower(), "CHECK")
            # tMap's nodeData must stay last, after metadata
            nd = el.find("nodeData")
            if nd is not None:
                el.remove(nd)
                el.append(nd)
            root.append(el)

        for cn in self.conns:
            if cn.kind in TRIGGERS:
                c = etree.SubElement(root, "connection", {
                    "connectorName": cn.kind, "label": cn.label, "lineStyle": TRIGGER_STYLE[cn.kind],
                    "metaname": cn.metaname, "offsetLabelX": "0", "offsetLabelY": "0",
                    "source": cn.src, "target": cn.tgt})
                if cn.kind == "RUN_IF":
                    etree.SubElement(c, "elementParameter", {"field": "TEXT", "name": "CONDITION",
                                                             "value": cn.condition})
                etree.SubElement(c, "elementParameter", {"field": "TEXT", "name": "UNIQUE_NAME",
                                                         "value": cn.uname, "show": "false"})
                continue
            src_comp = self._comp(cn.src)
            connector = "UNIQUE" if cn.kind == "unique" else ("MAIN" if src_comp in TACOKIT else "FLOW")
            style = {"main": "0", "unique": "0", "lookup": "8", "merge": "10"}[cn.kind]
            c = etree.SubElement(root, "connection", {
                "connectorName": connector, "label": cn.label, "lineStyle": style,
                "metaname": cn.metaname, "offsetLabelX": "0", "offsetLabelY": "0",
                "source": cn.src, "target": cn.tgt})
            if connector == "FLOW":
                etree.SubElement(c, "elementParameter", {"field": "CHECK", "name": "MONITOR_CONNECTION",
                                                         "value": "false"})
            etree.SubElement(c, "elementParameter", {"field": "TEXT", "name": "UNIQUE_NAME",
                                                     "value": cn.uname, "show": "false"})

        for uid in self._subjobs():
            sj = etree.SubElement(root, "subjob")
            etree.SubElement(sj, "elementParameter", {"field": "TEXT", "name": "UNIQUE_NAME",
                                                      "value": uid, "show": "false"})
            etree.SubElement(sj, "elementParameter", {"field": "COLOR", "name": "SUBJOB_TITLE_COLOR",
                                                      "value": "93;174;241", "show": "false"})
            etree.SubElement(sj, "elementParameter", {"field": "COLOR", "name": "SUBJOB_COLOR",
                                                      "value": "93;174;241"})
        etree.cleanup_namespaces(root, top_nsmap=NSMAP)
        body = etree.tostring(root, pretty_print=True, encoding="UTF-8", xml_declaration=False)
        # xsi:type="TalendMapper:MapperData" uses the prefix only inside an attribute *value*,
        # so lxml considers the declaration unused and drops it. Studio needs it on the root.
        if b"TalendMapper:" in body and b"xmlns:TalendMapper" not in body:
            body = body.replace(b" xmlns:talendfile=",
                                f' xmlns:TalendMapper="{MAPPER}" xmlns:talendfile='.encode(), 1)
        return b'<?xml version="1.0" encoding="UTF-8"?>\n' + body


def job_id(name: str) -> str:
    return eid("job", name)


def properties_xml(name: str, author_id: str, stamp: str, version: str = "0.1", item_path: str = "") -> str:
    pid, a = job_id(name), (lambda k: eid("props", name, k))
    pairs = [("created_product_fullname", "Talend Big Data Platform"),
             ("created_product_version", "8.0.1"), ("created_date", stamp),
             ("modified_product_fullname", "Talend Big Data Platform"),
             ("modified_product_version", "8.0.1"), ("modified_date", stamp)]
    extra = "\n".join(f'    <additionalProperties xmi:id="{a(k)}" key="{k}" value={quoteattr(v)}/>'
                      for k, v in pairs)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<xmi:XMI xmi:version="2.0" xmlns:xmi="http://www.omg.org/XMI" xmlns:TalendProperties="http://www.talend.org/properties">
  <TalendProperties:Property xmi:id="{a('property')}" id="{pid}" label="{name}" version="{version}" statusCode="" item="{a('item')}" displayName="{name}">
    <author href="../talend.project#{author_id}"/>
{extra}
  </TalendProperties:Property>
  <TalendProperties:ItemState xmi:id="{a('state')}" path="{item_path}"/>
  <TalendProperties:ProcessItem xmi:id="{a('item')}" property="{a('property')}" state="{a('state')}">
    <process href="{name}_{version}.item#/"/>
  </TalendProperties:ProcessItem>
</xmi:XMI>
"""


# ---------------------------------------------------------------- scenarios
def all_jobs(project: str) -> dict[str, JobBuilder]:
    jobs: dict[str, JobBuilder] = {}

    def new(name: str, contexts: dict | None = None) -> JobBuilder:
        jobs[name] = JobBuilder(name, project, contexts)
        return jobs[name]

    sel_cust = q("SELECT customer_id, first_name, last_name, email, country_code, created_at, is_active "
                 "FROM SRC.CUSTOMERS")
    sel_ord = q("SELECT order_id, customer_id, order_date, amount, status FROM SRC.ORDERS")
    sel_cty = q("SELECT country_code, country_name FROM REF.COUNTRIES")

    # 1 ---- straight load from an Excel file
    j = new("job_simple_copy")
    j.excel_input("tFileInputExcel_1", 160, 160, "customers_file", CUSTOMERS, "C:/data/customers.xlsx")
    j.jdbc_output("tJDBCOutput_1", 400, 160, "stg_customers", CUSTOMERS, q("STG.CUSTOMERS"), clear=True)
    j.flow("tFileInputExcel_1", "tJDBCOutput_1")

    # 2 ---- filter + rename + expressions in one tMap
    j = new("job_filter_map")
    out_cols = [I("customer_id", True), S("given_name"), S("family_name"), S("full_name", 101),
                S("email", 100), S("country_code", 2), S("email_lower", 100), D("load_date")]
    j.jdbc_input("tJDBCInput_1", 160, 160, "src_customers", CUSTOMERS, sel_cust)
    r1 = j.flow("tJDBCInput_1", "tMap_1")
    j.tmap("tMap_1", 400, 160,
           [{"name": r1, "cols": CUSTOMERS}],
           [{"name": "out_active", "cols": out_cols, "filter": f"{r1}.is_active",
             "entries": [("customer_id", f"{r1}.customer_id"),
                         ("given_name", f"{r1}.first_name"),
                         ("family_name", f"{r1}.last_name"),
                         ("full_name", f'{r1}.first_name + " " + {r1}.last_name'),
                         ("email", f'{r1}.email == null ? "unknown" : {r1}.email'),
                         ("country_code", f"StringHandling.UPCASE({r1}.country_code)"),
                         ("email_lower", f"{r1}.email.toLowerCase()"),
                         ("load_date", "TalendDate.getCurrentDate()")]}])
    j.jdbc_output("tJDBCOutput_1", 640, 160, "stg_customers_active", out_cols, q("STG.CUSTOMERS_ACTIVE"))
    j.flow("tMap_1", "tJDBCOutput_1", metaname="out_active", label="out_active")

    # 3 ---- main + two lookups (inner and left join)
    j = new("job_join_lookup")
    out_cols = [I("order_id", True), D("order_date"), N("amount"), S("customer_name", 101),
                S("country_name", 60)]
    j.jdbc_input("tJDBCInput_1", 160, 120, "src_orders", ORDERS, sel_ord)
    j.jdbc_input("tJDBCInput_2", 160, 240, "src_customers", CUSTOMERS, sel_cust)
    j.jdbc_input("tJDBCInput_3", 160, 360, "ref_countries", COUNTRIES, sel_cty)
    r1 = j.flow("tJDBCInput_1", "tMap_1")
    r2 = j.flow("tJDBCInput_2", "tMap_1", "lookup")
    r3 = j.flow("tJDBCInput_3", "tMap_1", "lookup")
    j.tmap("tMap_1", 440, 240,
           [{"name": r1, "cols": ORDERS},
            {"name": r2, "cols": CUSTOMERS, "lookup": True, "inner": True,
             "join": {"customer_id": f"{r1}.customer_id"}},
            {"name": r3, "cols": COUNTRIES, "lookup": True, "inner": False,
             "join": {"country_code": f"{r2}.country_code"}}],
           [{"name": "out_enriched", "cols": out_cols, "filter": f'{r1}.status.equals("COMPLETED")',
             "entries": [("order_id", f"{r1}.order_id"), ("order_date", f"{r1}.order_date"),
                         ("amount", f"{r1}.amount"),
                         ("customer_name", f'{r2}.first_name + " " + {r2}.last_name'),
                         ("country_name", f"{r3}.country_name")]}])
    j.jdbc_output("tJDBCOutput_1", 700, 240, "stg_orders_enriched", out_cols, q("STG.ORDERS_ENRICHED"))
    j.flow("tMap_1", "tJDBCOutput_1", metaname="out_enriched", label="out_enriched")

    # 4 ---- aggregate
    j = new("job_aggregate")
    agg_cols = [I("customer_id", True), N("total_amount"), I("order_count"), D("last_order_date")]
    j.jdbc_input("tJDBCInput_1", 160, 160, "src_orders", ORDERS, sel_ord)
    j.aggregate("tAggregateRow_1", 400, 160, agg_cols, ["customer_id"],
                [("total_amount", "sum", "amount"), ("order_count", "count", "order_id"),
                 ("last_order_date", "max", "order_date")])
    j.jdbc_output("tJDBCOutput_1", 640, 160, "mart_customer_totals", agg_cols, q("MART.CUSTOMER_TOTALS"))
    j.flow("tJDBCInput_1", "tAggregateRow_1")
    j.flow("tAggregateRow_1", "tJDBCOutput_1")

    # 5 ---- everything driven by context variables
    ctx = {g: {"db_host": ("id_String", h), "db_port": ("id_String", "1521"),
               "db_service": ("id_String", s), "db_user": ("id_String", u),
               "db_password": ("id_Password", ""),
               "src_schema": ("id_String", "SRC"), "src_table": ("id_String", "CUSTOMERS"),
               "tgt_schema": ("id_String", t), "tgt_table": ("id_String", "CUSTOMERS_COPY")}
           for g, h, s, u, t in (("Default", "dev-db.example.local", "DEVPDB", "dev_user", "STG_DEV"),
                                 ("Prod", "prod-db.example.local", "PRODPDB", "prod_user", "STG"))}
    j = new("job_context_driven", ctx)
    url = q("jdbc:oracle:thin:@//") + ' + context.db_host + ":" + context.db_port + "/" + context.db_service'
    q_ctx = ('"SELECT customer_id, first_name, last_name, email, country_code, created_at, is_active FROM "'
             ' + context.src_schema + "." + context.src_table')
    j.jdbc_input("tJDBCInput_1", 160, 160, "src_dynamic", CUSTOMERS, q_ctx, url, "context.db_user",
                 "context.db_password")
    j.jdbc_output("tJDBCOutput_1", 400, 160, "tgt_dynamic", CUSTOMERS,
                  'context.tgt_schema + "." + context.tgt_table', url=url, user="context.db_user",
                  password="context.db_password")
    j.flow("tJDBCInput_1", "tJDBCOutput_1")

    # 6 ---- union + de-duplication
    j = new("job_union_uniq")
    j.jdbc_input("tJDBCInput_1", 160, 120, "src_customers_eu", CUSTOMERS,
                 q("SELECT customer_id, first_name, last_name, email, country_code, created_at, is_active "
                   "FROM SRC.CUSTOMERS_EU"))
    j.jdbc_input("tJDBCInput_2", 160, 240, "src_customers_us", CUSTOMERS,
                 q("SELECT customer_id, first_name, last_name, email, country_code, created_at, is_active "
                   "FROM SRC.CUSTOMERS_US"))
    j.unite("tUnite_1", 400, 180, CUSTOMERS)
    j.uniq("tUniqRow_1", 600, 180, CUSTOMERS, ["customer_id"])
    j.jdbc_output("tJDBCOutput_1", 820, 180, "stg_customers_all", CUSTOMERS, q("STG.CUSTOMERS_ALL"))
    j.flow("tJDBCInput_1", "tUnite_1", "merge")
    j.flow("tJDBCInput_2", "tUnite_1", "merge")
    j.flow("tUnite_1", "tUniqRow_1")
    j.flow("tUniqRow_1", "tJDBCOutput_1", "unique")

    # 7 ---- custom Java (must be flagged "manual" by the tool)
    j = new("job_with_java")
    cols = [I("customer_id", True), S("first_name")]
    j.java("tJava_1", 160, 160, 'String runUser = System.getProperty("user.name");\n'
           'System.out.println("Started by " + runUser);\nglobalMap.put("run_user", runUser);')
    j.fixed_flow("tFixedFlowInput_1", 400, 160, cols, [q("1"), q("Ada")])
    j.log_row("tLogRow_1", 640, 160, cols)
    j.on_ok("tJava_1", "tFixedFlowInput_1")
    j.flow("tFixedFlowInput_1", "tLogRow_1")

    # 8 ---- orchestrator
    j = new("job_orchestrator")
    j.run_job("tRunJob_1", 160, 160, "job_simple_copy")
    j.run_job("tRunJob_2", 380, 160, "job_filter_map")
    j.run_job("tRunJob_3", 600, 160, "job_join_lookup")
    j.on_ok("tRunJob_1", "tRunJob_2")
    j.on_ok("tRunJob_2", "tRunJob_3")

    # 10 ---- one tMap, two filtered outputs
    j = new("job_multi_output")
    j.jdbc_input("tJDBCInput_1", 160, 200, "src_customers", CUSTOMERS, sel_cust)
    r1 = j.flow("tJDBCInput_1", "tMap_1")
    j.tmap("tMap_1", 400, 200, [{"name": r1, "cols": CUSTOMERS}],
           [{"name": "out_active", "cols": CUSTOMERS, "filter": f"{r1}.is_active",
             "entries": [(c.name, f"{r1}.{c.name}") for c in CUSTOMERS]},
            {"name": "out_inactive", "cols": CUSTOMERS, "filter": f"!{r1}.is_active",
             "entries": [(c.name, f"{r1}.{c.name}") for c in CUSTOMERS]}])
    j.jdbc_output("tJDBCOutput_1", 660, 120, "stg_customers_active", CUSTOMERS, q("STG.CUSTOMERS_ACTIVE"))
    j.jdbc_output("tJDBCOutput_2", 660, 280, "stg_customers_inactive", CUSTOMERS, q("STG.CUSTOMERS_INACTIVE"))
    j.flow("tMap_1", "tJDBCOutput_1", label="out_active", metaname="out_active")
    j.flow("tMap_1", "tJDBCOutput_2", label="out_inactive", metaname="out_inactive")

    # 11 ---- hand-written SQL in tOracleRow, parameterised by context
    ctx = {g: {"db_service": ("id_String", s), "db_schema": ("id_String", sc),
               "db_user": ("id_String", u), "db_password": ("id_Password", "")}
           for g, s, sc, u in (("Default", "DEVPDB", "STG_DEV", "dev_user"),
                               ("Prod", "PRODPDB", "STG", "prod_user"))}
    j = new("job_sql_template", ctx)
    j.oracle_row("tOracleRow_1", 160, 160, "truncate_target",
                 '"TRUNCATE TABLE " + context.db_schema + ".ORDERS_DAILY"')
    j.oracle_row("tOracleRow_2", 380, 160, "load_target",
                 '"INSERT INTO " + context.db_schema + ".ORDERS_DAILY (order_id, customer_id, amount) '
                 'SELECT o.order_id, o.customer_id, o.amount FROM SRC.ORDERS o '
                 'JOIN SRC.CUSTOMERS c ON c.customer_id = o.customer_id '
                 'WHERE o.order_date >= TRUNC(SYSDATE) - 1 AND c.is_active = 1"')
    j.oracle_row("tOracleRow_3", 600, 160, "flag_late_orders",
                 '"UPDATE " + context.db_schema + ".ORDERS_DAILY SET amount = 0 WHERE amount < 0"')
    j.on_ok("tOracleRow_1", "tOracleRow_2")
    j.on_ok("tOracleRow_2", "tOracleRow_3")
    _chain_jobs(new, sel_ord, sel_cust)
    _unverified_jobs(new)
    return jobs


def _chain_jobs(new, sel_ord: str, sel_cust: str) -> None:  # noqa: ANN001
    """Multi-step flows (tMap -> tAggregateRow, tUnite -> tMap) built from observed components only."""
    j = new("job_join_aggregate")
    sales = [S("country_code", 2), N("amount")]
    totals = [S("country_code", 2, True), N("total_amount"), I("order_count")]
    j.jdbc_input("tJDBCInput_1", 160, 120, "src_orders", ORDERS, sel_ord)
    j.jdbc_input("tJDBCInput_2", 160, 260, "src_customers", CUSTOMERS, sel_cust)
    r1 = j.flow("tJDBCInput_1", "tMap_1")
    r2 = j.flow("tJDBCInput_2", "tMap_1", "lookup")
    j.tmap("tMap_1", 420, 190,
           [{"name": r1, "cols": ORDERS},
            {"name": r2, "cols": CUSTOMERS, "lookup": True, "inner": True, "join": {"customer_id": f"{r1}.customer_id"}}],
           [{"name": "out_sales", "cols": sales, "filter": f'{r1}.status.equals("COMPLETED")',
             "entries": [("country_code", f"{r2}.country_code"), ("amount", f"{r1}.amount")]}])
    j.aggregate("tAggregateRow_1", 680, 190, totals, ["country_code"],
                [("total_amount", "sum", "amount"), ("order_count", "count", "amount")])
    j.jdbc_output("tJDBCOutput_1", 920, 190, "mart_sales_by_country", totals, q("MART.SALES_BY_COUNTRY"))
    j.flow("tMap_1", "tAggregateRow_1", label="out_sales", metaname="out_sales")
    j.flow("tAggregateRow_1", "tJDBCOutput_1")

    j = new("job_union_filter")
    out_cols = [I("customer_id", True), S("email", 100), S("country_code", 2)]
    j.jdbc_input("tJDBCInput_1", 160, 120, "src_customers_eu", CUSTOMERS,
                 q("SELECT customer_id, first_name, last_name, email, country_code, created_at, is_active "
                   "FROM SRC.CUSTOMERS_EU"))
    j.jdbc_input("tJDBCInput_2", 160, 260, "src_customers_us", CUSTOMERS,
                 q("SELECT customer_id, first_name, last_name, email, country_code, created_at, is_active "
                   "FROM SRC.CUSTOMERS_US"))
    j.unite("tUnite_1", 400, 190, CUSTOMERS)
    j.flow("tJDBCInput_1", "tUnite_1", "merge")
    j.flow("tJDBCInput_2", "tUnite_1", "merge")
    r1 = j.flow("tUnite_1", "tMap_1", label="all_customers", metaname="tUnite_1")
    j.tmap("tMap_1", 640, 190, [{"name": r1, "cols": CUSTOMERS}],
           [{"name": "out_active", "cols": out_cols, "filter": f"{r1}.is_active",
             "entries": [("customer_id", f"{r1}.customer_id"),
                         ("email", f'{r1}.email == null ? "unknown" : {r1}.email'),
                         ("country_code", f"StringHandling.UPCASE({r1}.country_code)")]}])
    j.jdbc_output("tJDBCOutput_1", 900, 190, "stg_customers_active_all", out_cols, q("STG.CUSTOMERS_ACTIVE_ALL"))
    j.flow("tMap_1", "tJDBCOutput_1", label="out_active", metaname="out_active")


def _unverified_jobs(new) -> None:  # noqa: ANN001
    """Jobs using components never observed in a real Studio file (see JobBuilder._raw)."""
    cols = [I("customer_id", True), S("first_name"), S("last_name")]
    fixed = [q("1"), q("Ada"), q("Lovelace")]

    j = new("job_delimited_copy")
    j.delimited_input("tFileInputDelimited_1", 160, 160, cols, "C:/data/customers.csv")
    j.delimited_output("tFileOutputDelimited_1", 420, 160, cols, "C:/data/customers_out.csv")
    j.flow("tFileInputDelimited_1", "tFileOutputDelimited_1")

    j = new("job_sort_row")
    j.fixed_flow("tFixedFlowInput_1", 160, 160, cols, fixed)
    j.sort_row("tSortRow_1", 400, 160, cols, [("last_name", "alpha", "asc"), ("customer_id", "num", "desc")])
    j.log_row("tLogRow_1", 640, 160, cols)
    j.flow("tFixedFlowInput_1", "tSortRow_1")
    j.flow("tSortRow_1", "tLogRow_1")

    j = new("job_with_javarow")
    j.fixed_flow("tFixedFlowInput_1", 160, 160, cols, fixed)
    j.java_row("tJavaRow_1", 400, 160, cols,
               "output_row.customer_id = input_row.customer_id;\n"
               "output_row.first_name = input_row.first_name.toUpperCase();\n"
               "output_row.last_name = input_row.last_name;")
    j.log_row("tLogRow_1", 640, 160, cols)
    j.flow("tFixedFlowInput_1", "tJavaRow_1")
    j.flow("tJavaRow_1", "tLogRow_1")

    j = new("job_prejob_postjob")
    j.prejob("tPrejob_1", 160, 80)
    j.java("tJava_1", 380, 80, 'System.out.println("init");')
    j.fixed_flow("tFixedFlowInput_1", 160, 200, cols, fixed)
    j.log_row("tLogRow_1", 400, 200, cols)
    j.postjob("tPostjob_1", 160, 320)
    j.java("tJava_2", 380, 320, 'System.out.println("cleanup");')
    j.on_component_ok("tPrejob_1", "tJava_1")
    j.flow("tFixedFlowInput_1", "tLogRow_1")
    j.on_component_ok("tPostjob_1", "tJava_2")

    j = new("job_runif")
    j.java("tJava_1", 160, 200, 'globalMap.put("load_full", true);')
    j.java("tJava_2", 400, 120, 'System.out.println("full load");')
    j.java("tJava_3", 400, 280, 'System.out.println("delta load");')
    j.run_if("tJava_1", "tJava_2", '((Boolean)globalMap.get("load_full"))')
    j.run_if("tJava_1", "tJava_3", '!((Boolean)globalMap.get("load_full"))')


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--project", default="DEV", help="technical label of the target Talend project")
    ap.add_argument("--author-id", default="_AUTHOR_PLACEHOLDER0000",
                    help="xmi id of the author in the target project's talend.project")
    ap.add_argument("--timestamp", default="2026-01-01T00:00:00.000+0000")
    ap.add_argument("--item-path", default="", help="Studio folder inside Job Designs, e.g. ToolKit")
    ap.add_argument("--include-studio-written", action="store_true",
                    help="also (re)generate jobs listed in STUDIO_WRITTEN")
    ap.add_argument("--no-overwrite", action="store_true")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, jb in all_jobs(args.project).items():
        if name in STUDIO_WRITTEN and not args.include_studio_written:
            continue
        item, props = out / f"{name}_0.1.item", out / f"{name}_0.1.properties"
        if args.no_overwrite and (item.exists() or props.exists()):
            print("skip (exists):", name)
            continue
        item.write_bytes(jb.build())
        props.write_text(properties_xml(name, args.author_id, args.timestamp, item_path=args.item_path), encoding="utf-8", newline="\n")
        print("wrote", item.name)


if __name__ == "__main__":
    main()
