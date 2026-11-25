"""Server-side graph layout for the dashboard's graph page (no JavaScript)."""

import math

from treant.dashboard.graphview import diagram, layout


def _ids(n):
    return [f"n{i}" for i in range(n)]


def test_layout_is_deterministic_and_spread():
    ids = _ids(12)
    edges = [("n0", "n1"), ("n1", "n2"), ("n2", "n0"), ("n3", "n4")]
    a = layout(ids, edges)
    assert a == layout(ids, edges)
    assert set(a) == set(ids)
    pts = list(a.values())
    assert all(math.isfinite(x) and math.isfinite(y) for x, y in pts)
    closest = min(math.dist(p, q) for i, p in enumerate(pts) for q in pts[i + 1 :])
    assert closest > 20


def test_connected_nodes_sit_closer_than_strangers():
    ids = _ids(10)
    pos = layout(ids, [("n0", "n1")])
    linked = math.dist(pos["n0"], pos["n1"])
    others = [math.dist(pos["n0"], pos[i]) for i in ids[2:]]
    assert linked < sum(others) / len(others)


def test_layout_edge_cases():
    assert layout([], []) == {}
    assert len(layout(["only"], [])) == 1
    # self-loops and edges to unknown ids are ignored, not errors
    assert set(layout(["a", "b"], [("a", "a"), ("a", "zzz")])) == {"a", "b"}


def test_diagram_positions_everything_inside_the_viewbox():
    graph = {
        "nodes": [
            {"id": "a", "name": "alpha", "type": "service", "kind": "instance", "general": False},
            {"id": "b", "name": "b" * 80, "type": "repo", "kind": "instance", "general": False},
            {"id": "c", "name": "gamma", "type": "tool", "kind": "concept", "general": True},
        ],
        "edges": [
            {"src": "a", "dst": "b", "type": "uses"},
            {"src": "a", "dst": "b", "type": "depends_on"},
            {"src": "b", "dst": "a", "type": "part_of"},
            {"src": "a", "dst": "a", "type": "uses"},
            {"src": "a", "dst": "c", "type": "refines"},
        ],
    }
    d = diagram(graph)
    x0, y0, w, h = d["viewbox"]
    for n in d["nodes"]:
        assert x0 <= n["x"] <= x0 + w and y0 <= n["y"] <= y0 + h
        assert len(n["label"]) <= 28
    long = next(n for n in d["nodes"] if n["id"] == "b")
    assert long["label"].endswith("…") and long["name"] == "b" * 80
    # parallel edges of one direction merge into one line with both labels; the self-loop is dropped
    labels = sorted(e["label"] for e in d["edges"])
    assert labels == ["depends_on, uses", "part_of", "refines"]
    # the two directions of a pair are offset so they do not draw on top of each other
    ab = next(e for e in d["edges"] if e["label"] == "depends_on, uses")
    ba = next(e for e in d["edges"] if e["label"] == "part_of")
    assert (ab["x1"], ab["y1"]) != (ba["x2"], ba["y2"])
    assert next(e for e in d["edges"] if e["label"] == "refines")["dashed"]


def test_diagram_of_empty_graph():
    d = diagram({"nodes": [], "edges": []})
    assert d["nodes"] == [] and d["edges"] == []


def test_unlinked_nodes_sit_in_rows_under_the_linked_ones():
    ids = _ids(10)
    pos = layout(ids, [("n0", "n1"), ("n1", "n2"), ("n2", "n3")])
    linked_bottom = max(pos[i][1] for i in ids[:4])
    lonely = [pos[i] for i in ids[4:]]
    assert all(y > linked_bottom for _, y in lonely)
    assert len({y for _, y in lonely}) <= 2  # six nodes fit in one or two rows
    xs = [x for x, _ in lonely]
    assert len(set(xs)) == len(xs) or len({y for _, y in lonely}) == 2
