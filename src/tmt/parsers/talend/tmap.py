"""tMap: inputs, joins, filters and output mappings from ``nodeData`` (TalendMapper:MapperData)."""

from __future__ import annotations

from collections.abc import Mapping

from lxml import etree

from tmt.models import ColumnMapping, JoinKey, TMapInput, TMapLogic, TMapOutput
from tmt.parsers.talend.expressions import parse_expression


def extract_tmap(
    node: etree._Element,
    lookup_labels: set[str],
    context_values: Mapping[str, str | None],
) -> tuple[TMapLogic, bool]:
    """Returns ``(logic, has_variables)``.

    Which input is the main flow and which are lookups is decided by the incoming connections
    (``lookup_labels``), not by the order of ``inputTables``: Studio does not keep the main table first.
    """
    nd = node.find("nodeData")
    if nd is None:
        return TMapLogic(), False

    in_tables = [t for t in nd.findall("inputTables")]
    names = [t.get("name") or "" for t in in_tables]
    inputs: list[TMapInput] = []
    for t in in_tables:
        name = t.get("name") or ""
        is_lookup = name in lookup_labels
        keys: list[JoinKey] = []
        if is_lookup:
            keys = [
                JoinKey(
                    lookup_column=e.get("name") or "",
                    expression=parse_expression(e.get("expression") or "", context_values, names),
                    operator=e.get("operator") or "=",  # attribute not seen in samples yet: default "="
                )
                for e in t.findall("mapperTableEntries")
                if (e.get("expression") or "").strip()
            ]
        inputs.append(
            TMapInput(
                name=name,
                role="lookup" if is_lookup else "main",
                join_type=("inner" if t.get("innerJoin") == "true" else "left") if is_lookup else None,
                matching_mode=t.get("matchingMode") if is_lookup else None,
                join_keys=keys,
            )
        )

    outputs: list[TMapOutput] = []
    for t in nd.findall("outputTables"):
        flt = None
        if t.get("activateExpressionFilter") == "true" and (t.get("expressionFilter") or "").strip():
            flt = parse_expression(t.get("expressionFilter") or "", context_values, names)
        mappings = [
            ColumnMapping(
                target=e.get("name") or "",
                expression=parse_expression(e.get("expression") or "", context_values, names),
            )
            for e in t.findall("mapperTableEntries")
        ]
        outputs.append(
            TMapOutput(
                name=t.get("name") or "",
                filter=flt,
                mappings=mappings,
                # attribute names for reject outputs are UNVERIFIED (no sample yet)
                is_reject=t.get("reject") == "true" or t.get("rejectInnerJoin") == "true",
            )
        )

    var_tables = nd.findall("varTables")
    has_vars = any(len(v.findall("mapperTableEntries")) for v in var_tables)
    return TMapLogic(inputs=sorted(inputs, key=lambda i: (i.role != "main", i.name)), outputs=outputs), has_vars
