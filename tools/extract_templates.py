"""One-off helper: extract scrubbed component templates from a real Talend 8.0.1 workspace.

The sample generator (generate_samples.py) never guesses parameter names; it clones the
nodes that Talend Studio itself wrote and only changes the values that matter. This script
creates those clones from a local workspace. It strips labels, comments, passwords, table
contents, schemas and mapper data, so no workspace content ends up in the repository.

Usage:
    python tools/extract_templates.py "C:/path/to/Talend_WS"
"""

from __future__ import annotations

import sys
from pathlib import Path

from lxml import etree

WANTED = [
    "tFileInputExcel", "tMap", "tLogRow", "tRunJob", "tOracleRow", "tJava",
    "tFixedFlowInput", "tAggregateRow", "tUniqRow", "tUnite",
    "JDBCInput", "JDBCOutput", "tPrejob", "tPostjob",
]
OUT = Path(__file__).parent / "templates"


def scrub_node(node: etree._Element) -> None:
    for m in node.findall("metadata"):
        node.remove(m)
    nd = node.find("nodeData")
    if nd is not None:
        node.remove(nd)
    for p in node.findall("elementParameter"):
        name, field = p.get("name"), p.get("field")
        if name in ("LABEL", "COMMENT"):
            p.set("value", "")
        if field == "PASSWORD":
            p.set("value", "")
        if field == "TABLE":
            for kid in list(p):
                p.remove(kid)
        if (p.get("value") or "").startswith("$"):
            p.set("value", '""')  # DataStage-migration placeholders name the source project's stages
        if (p.get("value") or "").startswith("context."):
            p.set("value", '""')  # context variables belong to the source project, not to the samples
        if name in ("FILENAME", "PROCESS"):
            p.set("value", '""')
        if name == "PREVIEW":
            p.set("value", "")
        if name == "DBNAME" and "$" in (p.get("value") or ""):
            p.set("value", '""')


def main(workspace: str) -> None:
    OUT.mkdir(exist_ok=True)
    found: dict[str, etree._Element] = {}
    for f in sorted(Path(workspace).glob("*/process/**/*.item")):
        try:
            root = etree.parse(str(f)).getroot()
        except etree.XMLSyntaxError:
            continue
        for n in root.iter("node"):
            c = n.get("componentName")
            if c in WANTED and c not in found:
                found[c] = n
        # keep the job-level <parameters> block of the smallest job as a template too
        if f.name == "DataFlow_0.1.item":
            params = root.find("parameters")
            for p in params.findall("elementParameter"):
                if p.get("field") == "PASSWORD":
                    p.set("value", "")
                if p.get("name") == "FILE_PATH":
                    p.set("value", '"C:/talend_logs"')
            (OUT / "_job_parameters.xml").write_bytes(
                etree.tostring(params, pretty_print=True, encoding="UTF-8"))
    for comp, node in found.items():
        scrub_node(node)
        (OUT / f"{comp}.xml").write_bytes(
            etree.tostring(node, pretty_print=True, encoding="UTF-8"))
        print("template:", comp)
    missing = set(WANTED) - set(found)
    if missing:
        sys.exit(f"missing components: {sorted(missing)}")


if __name__ == "__main__":
    main(sys.argv[1])
