"""Draw the lineage graph as a self-contained SVG: no scripts, no external assets, light and dark aware.

Layout: columns by dependency depth (data flows left to right), rows ordered to reduce edge crossings.
Cycles do not break it: edges inside a strongly connected group are drawn as dashed "back" edges.
"""

from __future__ import annotations

from html import escape

import networkx as nx

NODE_H = 34
ROW_GAP = 14
COL_GAP = 96
MARGIN = 24
LEGEND_H = 34
CHAR_W = 7.2
MAX_LABEL = 38


def _label(text: str) -> str:
    return text if len(text) <= MAX_LABEL else text[: MAX_LABEL - 1] + "…"


def _display(attrs: dict[str, object]) -> str:
    """The text shown in a node: the label, marked when the name is not certain."""
    unresolved = not attrs["resolved"] and attrs["type"] != "job"
    return _label(str(attrs["label"])) + (" (unresolved)" if unresolved else "")


def _width(text: str) -> float:
    return min(300.0, max(96.0, CHAR_W * len(text) + 30))


def _layers(g: nx.DiGraph) -> dict[str, int]:
    """Longest-path depth of each node after collapsing cycles."""
    cond = nx.condensation(g)
    member = cond.graph["mapping"]
    depth: dict[int, int] = {}
    for level, generation in enumerate(nx.topological_generations(cond)):  # generation index = longest path
        for c in generation:
            depth[c] = level
    return {n: depth[member[n]] for n in g.nodes}


def _order(g: nx.DiGraph, layer: dict[str, int]) -> dict[int, list[str]]:
    cols: dict[int, list[str]] = {}
    for n in sorted(g.nodes, key=lambda n: (g.nodes[n]["type"] != "job", str(g.nodes[n]["label"]).upper())):
        cols.setdefault(layer[n], []).append(n)
    pos = {n: i for col in cols.values() for i, n in enumerate(col)}
    for sweep in range(4):  # barycentre ordering, alternating direction
        for depth in sorted(cols, reverse=bool(sweep % 2)):
            def bary(n: str, pos: dict[str, int] = pos) -> float:
                near = [pos[m] for m in (*g.predecessors(n), *g.successors(n)) if layer[m] != layer[n]]
                return sum(near) / len(near) if near else float(pos[n])

            cols[depth].sort(key=lambda n: (bary(n), str(g.nodes[n]["label"]).upper()))
            for i, n in enumerate(cols[depth]):
                pos[n] = i
    return cols


def to_svg(g: nx.DiGraph, interactive: bool = False, highlight: str | None = None) -> str:
    """``interactive`` makes every node focusable and tags it with ``data-node`` so a page can attach its own tooltip
    (the native ``<title>`` tooltip is then omitted); ``highlight`` is the node id drawn as the master."""
    if len(g) == 0:
        return _wrap(320, 60, '<text x="24" y="36" class="tmt-text">No jobs to draw.</text>', "empty lineage graph")

    layer = _layers(g)
    cols = _order(g, layer)
    widths = {n: _width(_display(g.nodes[n])) for n in g.nodes}
    col_w = {d: max(widths[n] for n in nodes) for d, nodes in cols.items()}
    x0: dict[int, float] = {}
    x = float(MARGIN)
    for d in sorted(cols):
        x0[d] = x
        x += col_w[d] + COL_GAP
    total_w = x - COL_GAP + MARGIN
    rows = max(len(nodes) for nodes in cols.values())
    total_h = LEGEND_H + MARGIN + rows * (NODE_H + ROW_GAP) + MARGIN

    xy: dict[str, tuple[float, float]] = {}
    for d, nodes in cols.items():
        for i, n in enumerate(nodes):
            xy[n] = (x0[d], LEGEND_H + MARGIN + i * (NODE_H + ROW_GAP))

    parts: list[str] = [_legend()]
    for u, v, a in sorted(g.edges(data=True), key=lambda e: (e[0], e[1])):
        ux, uy = xy[u]
        vx, vy = xy[v]
        sx, sy = ux + widths[u], uy + NODE_H / 2
        ex, ey = vx, vy + NODE_H / 2
        back = layer[v] <= layer[u]
        if back:  # a cycle: leave on the right, come back around on the left
            sx, ex = ux + widths[u], vx
            c1, c2 = sx + 60, ex - 60
            sy2 = sy
        else:
            dx = max(40.0, (ex - sx) * 0.5)
            c1, c2, sy2 = sx + dx, ex - dx, sy
        cls = "tmt-edge" + (" tmt-runs" if a["kind"] == "runs" else "") + (
            " tmt-dash" if (not a.get("resolved", True) or back) else "")
        curve = f"M{sx:.1f},{sy2:.1f} C{c1:.1f},{sy2:.1f} {c2:.1f},{ey:.1f} {ex:.1f},{ey:.1f}"
        what = {"read": "reads", "write": "writes", "runs": "starts"}[a["kind"]]
        actions = f" ({', '.join(a['actions'])})" if a.get("actions") else ""
        title = f"{g.nodes[u]['label']} {what} {g.nodes[v]['label']}{actions}" if a["kind"] != "read" else (
            f"{g.nodes[v]['label']} reads {g.nodes[u]['label']}")
        ends = (f' data-u="{escape(u, quote=True)}" data-v="{escape(v, quote=True)}"' if interactive else "")
        tip = "" if interactive else f"<title>{escape(title)}</title>"
        parts.append(f'<path class="{cls}" d="{curve}" marker-end="url(#tmt-arrow)"{ends}>{tip}</path>')
        if a["kind"] == "runs" and not back:
            parts.append(f'<text class="tmt-edge-label" x="{(sx + ex) / 2:.1f}" y="{(sy2 + ey) / 2 - 4:.1f}" '
                         'text-anchor="middle">tRunJob</text>')

    for n in sorted(g.nodes, key=lambda n: xy[n]):
        attrs = g.nodes[n]
        nx_, ny = xy[n]
        w = widths[n]
        unresolved = not attrs["resolved"] and attrs["type"] != "job"
        cls = f"tmt-node tmt-{attrs['type']}" + (" tmt-unresolved" if unresolved else "")
        if attrs["type"] == "job":
            shape = (f'<rect class="{cls}" x="{nx_:.1f}" y="{ny:.1f}" width="{w:.1f}" height="{NODE_H}" '
                     f'rx="{NODE_H / 2}"/>')
        elif attrs["type"] == "file":
            fold = 10
            pts = (f"{nx_:.1f},{ny:.1f} {nx_ + w - fold:.1f},{ny:.1f} {nx_ + w:.1f},{ny + fold:.1f} "
                   f"{nx_ + w:.1f},{ny + NODE_H:.1f} {nx_:.1f},{ny + NODE_H:.1f}")
            shape = f'<polygon class="{cls}" points="{pts}"/>'
        else:
            shape = f'<rect class="{cls}" x="{nx_:.1f}" y="{ny:.1f}" width="{w:.1f}" height="{NODE_H}" rx="3"/>'
        text = _display(attrs)
        full = str(attrs["label"]) + (" — depends on a context variable or placeholder" if unresolved else "")
        if highlight == n:
            shape = shape.replace('class="', 'class="tmt-master ', 1)
        label_text = (f'<text class="tmt-text" x="{nx_ + w / 2:.1f}" y="{ny + NODE_H / 2 + 4:.1f}" '
                      f'text-anchor="middle">{escape(text)}</text>')
        if interactive:
            parts.append(f'<g class="tmt-n" data-node="{escape(n, quote=True)}" tabindex="0" role="button" '
                         f'aria-label="{escape(full, quote=True)}">{shape}{label_text}</g>')
        else:
            parts.append(f"<g><title>{escape(full)}</title>{shape}{label_text}</g>")

    return _wrap(total_w, total_h, "\n".join(parts), "lineage graph: jobs, the tables they read and write")


def _legend() -> str:
    items = [("tmt-job", "job", 0), ("tmt-table", "table", 1), ("tmt-file", "file", 2)]
    out = []
    for cls, name, i in items:
        x = MARGIN + i * 92
        radius = 7 if name == "job" else 2
        out.append(f'<rect class="tmt-node {cls}" x="{x}" y="10" width="26" height="14" rx="{radius}"/>'
                   f'<text class="tmt-edge-label" x="{x + 32}" y="21">{name}</text>')
    x = MARGIN + 3 * 92
    out.append(f'<path class="tmt-edge tmt-dash" d="M{x},17 L{x + 26},17"/>'
               f'<text class="tmt-edge-label" x="{x + 32}" y="21">unresolved</text>')
    return "".join(out)


_STYLE = """
.tmt-graph{--g-bg:#ffffff;--g-text:#0f172a;--g-muted:#475569;--g-edge:#64748b;--g-job:#dbeafe;--g-job-s:#1d4ed8;
--g-table:#dcfce7;--g-table-s:#15803d;--g-file:#fef9c3;--g-file-s:#a16207;--g-bad:#b91c1c;background:var(--g-bg);
font:12px system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
@media (prefers-color-scheme: dark){.tmt-graph{--g-bg:#0b1220;--g-text:#e5e7eb;--g-muted:#94a3b8;--g-edge:#8aa0c0;
--g-job:#1e3a8a;--g-job-s:#93c5fd;--g-table:#14532d;--g-table-s:#86efac;--g-file:#422006;--g-file-s:#fde68a;--g-bad:#fca5a5}}
.tmt-text{fill:var(--g-text)}.tmt-edge-label{fill:var(--g-muted);font-size:11px}
.tmt-node{stroke-width:1.5}
.tmt-job{fill:var(--g-job);stroke:var(--g-job-s)}.tmt-table{fill:var(--g-table);stroke:var(--g-table-s)}
.tmt-file{fill:var(--g-file);stroke:var(--g-file-s)}
.tmt-unresolved{stroke:var(--g-bad);stroke-dasharray:5 3}
.tmt-master{stroke-width:3.5}
.tmt-n{cursor:pointer;outline:none}.tmt-n:hover .tmt-node,.tmt-n:focus .tmt-node{stroke-width:3}
.tmt-n:focus-visible .tmt-node{stroke:var(--g-text);stroke-width:3.5}
.tmt-dim{opacity:.18}.tmt-edge.tmt-hot{stroke:var(--g-text);stroke-width:2.4;opacity:1}
.tmt-edge{fill:none;stroke:var(--g-edge);stroke-width:1.4}.tmt-runs{stroke-width:2.6}
.tmt-dash{stroke-dasharray:5 4}
#tmt-arrow path{fill:var(--g-edge)}
"""


def _wrap(w: float, h: float, body: str, desc: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" class="tmt-graph" viewBox="0 0 {w:.0f} {h:.0f}" '
        f'width="{w:.0f}" height="{h:.0f}" role="img" aria-label="{escape(desc)}">\n'
        f"<title>{escape(desc)}</title>\n<style>{_STYLE}</style>\n"
        '<defs><marker id="tmt-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
        'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z"/></marker></defs>\n'
        f"{body}\n</svg>\n"
    )
