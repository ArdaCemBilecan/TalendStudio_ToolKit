"""Tiny builders for synthetic .item files used by unit tests."""

from __future__ import annotations

from pathlib import Path

HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<talendfile:ProcessType xmi:version="2.0" xmlns:xmi="http://www.omg.org/XMI" '
    'xmlns:talendfile="platform:/resource/org.talend.model/model/TalendFile.xsd" '
    'defaultContext="Default" jobType="Standard">\n'
)
FOOTER = "</talendfile:ProcessType>\n"


def node(uid: str, comp: str, params: str = "", extra: str = "") -> str:
    return (
        f'<node componentName="{comp}" componentVersion="0.1" posX="0" posY="0">'
        f'<elementParameter field="TEXT" name="UNIQUE_NAME" value="{uid}" show="false"/>{params}{extra}</node>\n'
    )


def param(name: str, value: str, field: str = "TEXT") -> str:
    value = value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
    return f'<elementParameter field="{field}" name="{name}" value="{value}"/>'


def conn(src: str, tgt: str, connector: str = "FLOW", style: str = "0", label: str = "row1") -> str:
    return (
        f'<connection connectorName="{connector}" label="{label}" lineStyle="{style}" metaname="{src}" '
        f'source="{src}" target="{tgt}"><elementParameter field="TEXT" name="UNIQUE_NAME" value="{label}" '
        f'show="false"/></connection>\n'
    )


def write_job(tmp: Path, name: str, body: str, contexts: str = '<context name="Default"/>\n') -> Path:
    item = tmp / f"{name}_0.1.item"
    item.write_text(HEADER + contexts + body + FOOTER, encoding="utf-8")
    return item
