"""Context groups and context parameters of a Talend job."""

from __future__ import annotations

from lxml import etree

from tmt.models import ContextParam


def parse_contexts(root: etree._Element) -> dict[str, dict[str, ContextParam]]:
    """All ``<context>`` groups of the job. Secrets are never copied (value=None, is_secret=True)."""
    groups: dict[str, dict[str, ContextParam]] = {}
    for ctx in root.findall("context"):
        params: dict[str, ContextParam] = {}
        for p in ctx.findall("contextParameter"):
            name = p.get("name") or ""
            ptype = p.get("type") or "id_String"
            value = p.get("value")
            secret = ptype == "id_Password" or (value or "").startswith("enc:")
            params[name] = ContextParam(
                name=name,
                type=ptype,
                value=None if secret else value,
                is_secret=secret,
                source="repository" if p.get("repositoryContextId") else "built_in",
            )
        groups[ctx.get("name") or "Default"] = dict(sorted(params.items()))
    return dict(sorted(groups.items()))


def values_for(groups: dict[str, dict[str, ContextParam]], group: str) -> dict[str, str | None]:
    """name -> value for one group (None for secrets), used to fold ``context.*`` expressions."""
    return {n: p.value for n, p in groups.get(group, {}).items()}
