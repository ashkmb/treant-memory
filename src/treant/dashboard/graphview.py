"""Lay out a node graph on the server so the graph page can draw it as plain SVG (no JavaScript).

``layout`` places linked nodes with a Fruchterman-Reingold force layout seeded from the node ids
(so the same graph always draws the same way) and unlinked nodes in rows underneath. ``diagram`` turns a ``browse.graph`` result
into what the template draws: node positions, labels, merged edge lines and a viewBox.
"""

import hashlib
import math
from collections.abc import Iterable, Sequence

RADIUS = 9.0
FOCUS_RADIUS = 13.0
LABEL_MAX = 28
_PAD_X, _PAD_Y = 110.0, 40.0
_OFFSET = 5.0  # sideways shift for the two directions of one pair
GRAVITY = 1.0  # pull toward the centre; keeps separate clusters close
_COL, _ROW = 150.0, 70.0  # grid for unlinked nodes


def _seed(node_id: str) -> tuple[float, float]:
    h = hashlib.sha256(node_id.encode()).digest()
    return int.from_bytes(h[:4]) / 2**32 - 0.5, int.from_bytes(h[4:8]) / 2**32 - 0.5


def layout(
    ids: Sequence[str], edges: Iterable[tuple[str, str]], iterations: int = 0
) -> dict[str, tuple[float, float]]:
    """Positions for ``ids``: linked nodes by force layout, unlinked ones in rows underneath.
    Self-loops and edges whose ends are not in ``ids`` are ignored."""
    known = set(ids)
    links = {(a, b) for a, b in edges if a != b and a in known and b in known}
    ends = {x for pair in links for x in pair}
    linked = [i for i in ids if i in ends]
    pos = _force(linked, links, iterations)
    lonely = [i for i in ids if i not in ends]
    if lonely:
        xs = [p[0] for p in pos.values()] or [0.0]
        top = max((p[1] for p in pos.values()), default=-_ROW) + _ROW
        cols = max(1, min(len(lonely), int(max(max(xs) - min(xs), 600) // _COL) + 1))
        left = (min(xs) + max(xs)) / 2 - (cols - 1) * _COL / 2
        for n, i in enumerate(lonely):
            pos[i] = (round(left + (n % cols) * _COL, 1), round(top + (n // cols) * _ROW, 1))
    return pos


def _force(
    ids: Sequence[str], links: set[tuple[str, str]], iterations: int
) -> dict[str, tuple[float, float]]:
    """Fruchterman-Reingold with a pull toward the centre, seeded from the ids."""
    n = len(ids)
    if n == 0:
        return {}
    k = 110.0  # ideal distance between linked nodes
    side = k * math.sqrt(n) * 1.5
    pos = {i: [x * side, y * side] for i, (x, y) in ((i, _seed(i)) for i in ids)}
    steps = iterations or max(60, min(300, 30000 // n))
    temp = start = side / 8
    for step in range(steps):
        disp = {i: [0.0, 0.0] for i in ids}
        for a_i, a in enumerate(ids):
            ax, ay = pos[a]
            for b in ids[a_i + 1 :]:
                dx, dy = ax - pos[b][0], ay - pos[b][1]
                d2 = dx * dx + dy * dy or 0.01
                f = k * k / d2
                disp[a][0] += dx * f
                disp[a][1] += dy * f
                disp[b][0] -= dx * f
                disp[b][1] -= dy * f
        for a, b in links:
            dx, dy = pos[a][0] - pos[b][0], pos[a][1] - pos[b][1]
            d = math.hypot(dx, dy) or 0.1
            f = d / k
            disp[a][0] -= dx * f
            disp[a][1] -= dy * f
            disp[b][0] += dx * f
            disp[b][1] += dy * f
        for i in ids:
            x, y = pos[i]
            gx, gy = disp[i][0] - x * GRAVITY, disp[i][1] - y * GRAVITY
            d = math.hypot(gx, gy) or 0.1
            move = min(d, temp)
            pos[i] = [x + gx / d * move, y + gy / d * move]
        temp = max(1.0, start * (1 - (step + 1) / steps))
    return {i: (round(p[0], 1), round(p[1], 1)) for i, p in pos.items()}


def _label(name: str) -> str:
    return name if len(name) <= LABEL_MAX else name[: LABEL_MAX - 1] + "…"


def diagram(graph: dict) -> dict:
    """What the graph template draws for a ``browse.graph`` result."""
    nodes = graph["nodes"]
    ids = [n["id"] for n in nodes]
    known = set(ids)
    merged: dict[tuple[str, str], list[str]] = {}
    for e in graph["edges"]:
        if e["src"] != e["dst"] and e["src"] in known and e["dst"] in known:
            merged.setdefault((e["src"], e["dst"]), []).append(e["type"])
    pos = layout(ids, merged)
    if not pos:
        return {"nodes": [], "edges": [], "viewbox": (0, 0, 400, 120)}
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    x0, y0 = min(xs) - _PAD_X, min(ys) - _PAD_Y
    viewbox = (
        round(x0),
        round(y0),
        round(max(xs) - min(xs) + 2 * _PAD_X),
        round(max(ys) - min(ys) + 2 * _PAD_Y + 18),  # room for the last row's labels
    )
    radius = {n["id"]: FOCUS_RADIUS if n.get("depth") == 0 else RADIUS for n in nodes}
    out_nodes = [
        n
        | {"x": pos[n["id"]][0], "y": pos[n["id"]][1], "r": radius[n["id"]]}
        | {"label": _label(n["name"])}
        for n in nodes
    ]
    out_edges = []
    for (a, b), types in sorted(merged.items()):
        (ax, ay), (bx, by) = pos[a], pos[b]
        d = math.hypot(bx - ax, by - ay) or 0.1
        ux, uy = (bx - ax) / d, (by - ay) / d
        shift = _OFFSET if (b, a) in merged else 0.0
        ox, oy = -uy * shift, ux * shift
        at = 0.35 if shift else 0.5  # two directions: each label nearer its own source
        start, end = radius[a] + 2, radius[b] + 3
        out_edges.append(
            {
                "x1": round(ax + ux * start + ox, 1),
                "y1": round(ay + uy * start + oy, 1),
                "x2": round(bx - ux * end + ox, 1),
                "y2": round(by - uy * end + oy, 1),
                "mx": round(ax + (bx - ax) * at + ox * 2.4, 1),
                "my": round(ay + (by - ay) * at + oy * 2.4 - 3, 1),
                "label": ", ".join(sorted(set(types))),
                "dashed": types == ["refines"],
            }
        )
    return {"nodes": out_nodes, "edges": out_edges, "viewbox": viewbox}
