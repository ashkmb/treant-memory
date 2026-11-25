import itertools
import uuid

from treant import graph
from treant.walk import walk


def _u(p: str) -> str:
    return f"{p}-{uuid.uuid4().hex[:8]}"


def _setup(db):
    with db.publisher_tx() as c:
        g = graph.add_node(c, "general", "concept", "tool", _u("signoz"))
    with db.ws_tx("alpha") as c:
        a = graph.add_node(c, "alpha", "local_concept", "tool", _u("a"), refines=g)
    with db.ws_tx("beta") as c:
        b = graph.add_node(c, "beta", "local_concept", "tool", _u("b"), refines=g)
    return g, a, b


def test_walk_never_crosses_via_general(db):
    g, a, b = _setup(db)
    with db.ws_tx("alpha") as c:
        result = walk(c, "alpha", [("ws", a)], hops=2)
    assert ("general", g) in result
    assert ("ws", a) in result
    assert ("ws", b) not in result and ("general", b) not in result
    assert all(ref[1] != b for ref in result)


def test_walk_from_general_goes_down_only_into_origin(db):
    g, a, b = _setup(db)
    with db.ws_tx("alpha") as c:
        result = walk(c, "alpha", [("general", g)], hops=2)
    assert ("ws", a) in result
    assert all(ref[1] != b for ref in result)
    with db.ws_tx("beta") as c:
        result = walk(c, "beta", [("general", g)], hops=1)
    assert ("ws", b) in result and all(ref[1] != a for ref in result)


def test_walk_follows_edges_and_clamps_hops(db):
    with db.ws_tx("alpha") as c:
        n = [graph.add_node(c, "alpha", "instance", "host", _u("n")) for _ in range(4)]
        for s, d in itertools.pairwise(n):
            graph.add_edge(c, "alpha", s, d, "uses")
        r0 = walk(c, "alpha", [("ws", n[0])], hops=0)
        r2 = walk(c, "alpha", [("ws", n[0])], hops=9)
        back = walk(c, "alpha", [("ws", n[1])], hops=1)  # edges both directions
    assert r0 == [("ws", n[0])]
    assert r2 == [("ws", n[0]), ("ws", n[1]), ("ws", n[2])]
    assert set(back) == {("ws", n[0]), ("ws", n[1]), ("ws", n[2])}


def test_walk_skips_superseded_and_retracted(db):
    with db.ws_tx("alpha") as c:
        s, ok, gone, edge_gone = (
            graph.add_node(c, "alpha", "instance", "host", _u("n")) for _ in range(4)
        )
        graph.add_edge(c, "alpha", s, ok, "uses")
        graph.add_edge(c, "alpha", s, gone, "uses")
        e = graph.add_edge(c, "alpha", s, edge_gone, "uses")
        graph.retract(c, "alpha", "node", gone)
        graph.retract(c, "alpha", "edge", e)
        result = walk(c, "alpha", [("ws", s)], hops=2)
    assert ("ws", ok) in result
    assert ("ws", gone) not in result and ("ws", edge_gone) not in result


def test_walk_drops_foreign_and_retracted_starts(db):
    g, a, b = _setup(db)
    with db.ws_tx("alpha") as c:
        dead = graph.add_node(c, "alpha", "instance", "host", _u("dead"))
        graph.retract(c, "alpha", "node", dead)
        result = walk(c, "alpha", [("ws", b), ("ws", dead), ("ws", a)], hops=2)
    assert ("ws", b) not in result and ("ws", dead) not in result
    assert ("ws", a) in result and ("general", g) in result


def test_walk_code_never_descends_into_other_workspace(db):
    g, a, b = _setup(db)
    with db.admin_tx() as c:  # role that can read every workspace schema
        result = walk(c, "alpha", [("ws", a)], hops=2)
        down = walk(c, "alpha", [("general", g)], hops=2)
    for r in (result, down):
        assert ("general", g) in r
        assert all(ref[1] != b for ref in r)
    assert ("ws", a) in down


def test_walk_skips_superseded_nodes_and_general_items(db):
    with db.publisher_tx() as c:
        g = graph.add_node(c, "general", "concept", "tool", _u("g"))
        g_old = graph.add_node(c, "general", "concept", "tool", _u("gold"))
        g_next = graph.add_node(c, "general", "concept", "tool", _u("gnext"))
        g_edge = graph.add_node(c, "general", "concept", "tool", _u("gedge"))
        graph.add_edge(c, "general", g, g_old, "uses")
        graph.add_edge(c, "general", g, g_next, "uses")
        dead_edge = graph.add_edge(c, "general", g, g_edge, "uses")
        graph.retract(c, "general", "edge", dead_edge)
        c.execute("UPDATE general.nodes SET status = 'superseded' WHERE id = %s", (g_old,))
    with db.ws_tx("alpha") as c:
        local = graph.add_node(c, "alpha", "local_concept", "tool", _u("l"), refines=g)
        old_local = graph.add_node(c, "alpha", "local_concept", "tool", _u("ol"), refines=g)
        n = graph.add_node(c, "alpha", "instance", "host", _u("n"))
        old_n = graph.add_node(c, "alpha", "instance", "host", _u("on"))
        graph.add_edge(c, "alpha", n, old_n, "uses")
        c.execute(
            "UPDATE ws_alpha.nodes SET status = 'superseded' WHERE id = ANY(%s::uuid[])",
            ([old_local, old_n],),
        )
        down = walk(c, "alpha", [("general", g)], hops=1)
        side = walk(c, "alpha", [("ws", n)], hops=2)
    assert ("ws", local) in down and ("general", g_next) in down
    assert ("ws", old_local) not in down
    assert ("general", g_old) not in down and ("general", g_edge) not in down
    assert ("ws", old_n) not in side
