"""Structural sanity checks for generated (or Studio-written) .item/.properties pairs.

Not a substitute for opening the job in Talend Studio, but catches the mistakes a
generator can make: dangling connection endpoints, tMap tables that do not match their
incoming connection labels, missing namespace declarations, tRunJob pointing at a
non-existent child, duplicate unique names.

Usage: python tools/validate_samples.py samples/talend/process
"""

from __future__ import annotations

import sys
from pathlib import Path

from lxml import etree

XSI = "http://www.w3.org/2001/XMLSchema-instance"


def check(item: Path) -> list[str]:
    errs: list[str] = []
    props = item.with_suffix(".properties")
    if not props.exists():
        errs.append("missing .properties")
    root = etree.parse(str(item)).getroot()  # raises on malformed XML
    if "TalendMapper" not in root.nsmap and b"TalendMapper:" in item.read_bytes():
        errs.append("TalendMapper prefix used but not declared")
    nodes = {}
    for n in root.findall("node"):
        uid = next(p.get("value") for p in n.findall("elementParameter") if p.get("name") == "UNIQUE_NAME")
        if uid in nodes:
            errs.append(f"duplicate node {uid}")
        nodes[uid] = n
    names = set()
    incoming: dict[str, list[str]] = {}
    for c in root.findall("connection"):
        for end in ("source", "target"):
            if c.get(end) not in nodes:
                errs.append(f"connection {c.get('label')} dangling {end}={c.get(end)}")
        uname = next(p.get("value") for p in c.findall("elementParameter") if p.get("name") == "UNIQUE_NAME")
        if uname in names:
            errs.append(f"duplicate connection name {uname}")
        names.add(uname)
        if c.get("connectorName") not in ("SUBJOB_OK", "RUN_IF", "COMPONENT_OK"):
            incoming.setdefault(c.get("target"), []).append(c.get("label"))
    for uid, n in nodes.items():
        if n.get("componentName") == "tMap":
            tabs = [t.get("name") for t in n.iter("inputTables")]
            if sorted(tabs) != sorted(incoming.get(uid, [])):
                errs.append(f"{uid}: tMap input tables {tabs} != incoming labels {incoming.get(uid)}")
            outs = {t.get("name") for t in n.iter("outputTables")}
            metas = {m.get("name") for m in n.findall("metadata")}
            if outs != metas:
                errs.append(f"{uid}: output tables {outs} != metadata {metas}")
        if n.get("componentName") == "tRunJob":
            ref = next(p.get("value") for p in n.findall("elementParameter")
                       if p.get("name") == "PROCESS:PROCESS_TYPE_PROCESS")
            child_id = ref.split(":", 1)[1]
            if not any(child_id in p.read_text(encoding="utf-8") for p in item.parent.glob("*.properties")):
                errs.append(f"{uid}: tRunJob child id {child_id} not found among sibling .properties")
    covered = {sj.find("elementParameter[@name='UNIQUE_NAME']").get("value") for sj in root.findall("subjob")}
    if not covered <= set(nodes):
        errs.append(f"subjob refers to unknown node(s): {covered - set(nodes)}")
    return errs


def main(folder: str) -> int:
    bad = 0
    for item in sorted(Path(folder).glob("*.item")):
        errs = check(item)
        print(("FAIL " if errs else "ok   ") + item.name)
        for e in errs:
            print("      -", e)
        bad += bool(errs)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
