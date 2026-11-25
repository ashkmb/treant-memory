import uuid

import pytest

from treant import graph
from treant.errors import InvalidInput, NotFound


def _u(p: str) -> str:
    return f"{p}-{uuid.uuid4().hex[:8]}"


def _general_concept(db, name=None):
    with db.publisher_tx() as c:
        return graph.add_node(c, "general", "concept", "tool", name or _u("g"))


def test_add_edge_across_namespaces_rejected(db):
    g = _general_concept(db)
    with db.ws_tx("alpha") as c:
        local = graph.add_node(c, "alpha", "local_concept", "tool", _u("l"), refines=g)
        with pytest.raises(InvalidInput):
            graph.add_edge(c, "alpha", local, g, "uses")
    with db.ws_tx("alpha") as c, pytest.raises(InvalidInput):
        graph.add_edge(c, "alpha", local, str(uuid.uuid4()), "uses")


def test_concept_kind_only_in_general(db):
    g = _general_concept(db)
    with db.ws_tx("alpha") as c:
        with pytest.raises(InvalidInput):
            graph.add_node(c, "alpha", "concept", "tool", _u("x"))
        with pytest.raises(InvalidInput):
            graph.add_node(c, "alpha", "local_concept", "tool", _u("x"))  # no refines
    with db.publisher_tx() as c:
        with pytest.raises(InvalidInput):
            graph.add_node(c, "general", "instance", "host", _u("x"))
        with pytest.raises(InvalidInput):
            graph.add_node(c, "general", "local_concept", "host", _u("x"), refines=g)


def test_unknown_values_rejected(db):
    with db.ws_tx("alpha") as c:
        with pytest.raises(InvalidInput):
            graph.add_fact(c, "alpha", "bogus", "t")
        with pytest.raises(InvalidInput):
            graph.add_node(c, "alpha", "instance", "host", _u("i"), refines=str(uuid.uuid4()))
        a = graph.add_node(c, "alpha", "instance", "host", _u("i"))
        b = graph.add_node(c, "alpha", "instance", "host", _u("i"))
        with pytest.raises(InvalidInput):
            graph.add_edge(c, "alpha", a, b, "likes")
        f = graph.add_fact(c, "alpha", "fact", "t")
        with pytest.raises(InvalidInput):
            graph.update_fact(c, "alpha", f, status="bogus")
        with pytest.raises(InvalidInput):
            graph.update_fact(c, "alpha", f, status="closed")
        with pytest.raises(InvalidInput):
            graph.update_fact(c, "alpha", str(uuid.uuid4()), text="x")
        with pytest.raises(InvalidInput):
            graph.add_fact(c, "alpha", "fact", "t", node_id=str(uuid.uuid4()))


def test_update_fact_keeps_old_version(db):
    with db.ws_tx("alpha") as c:
        f = graph.add_fact(c, "alpha", "fact", "old text")
        v = graph.update_fact(c, "alpha", f, text="new text")
        assert v == 2
        text, version = c.execute(
            "SELECT text, version FROM ws_alpha.facts WHERE id = %s", (f,)
        ).fetchone()
        old = c.execute(
            "SELECT data->>'text', version FROM ws_alpha.versions "
            "WHERE item_kind = 'fact' AND item_id = %s",
            (f,),
        ).fetchall()
    assert (text, version) == ("new text", 2)
    assert old == [("old text", 1)]


def test_update_fact_text_change_drops_embedding(db):
    with db.ws_tx("alpha") as c:
        f = graph.add_fact(c, "alpha", "fact", "embed me")
        vec = "[" + ",".join(["0.1"] * 768) + "]"
        c.execute(
            "INSERT INTO ws_alpha.embeddings (item_kind, item_id, vec) VALUES ('fact', %s, %s)",
            (f, vec),
        )
        graph.update_fact(c, "alpha", f, status="superseded")
        n = c.execute("SELECT count(*) FROM ws_alpha.embeddings WHERE item_id = %s", (f,))
        assert n.fetchone()[0] == 1  # status-only change keeps it
        graph.update_fact(c, "alpha", f, text="changed")
        n = c.execute("SELECT count(*) FROM ws_alpha.embeddings WHERE item_id = %s", (f,))
        assert n.fetchone()[0] == 0


def test_retract_keeps_row_and_history(db):
    with db.ws_tx("alpha") as c:
        f = graph.add_fact(c, "alpha", "fact", "keep me")
        graph.retract(c, "alpha", "fact", f)
        status, version = c.execute(
            "SELECT status, version FROM ws_alpha.facts WHERE id = %s", (f,)
        ).fetchone()
        hist = c.execute(
            "SELECT count(*) FROM ws_alpha.versions WHERE item_kind='fact' AND item_id=%s", (f,)
        ).fetchone()[0]
        n = graph.add_node(c, "alpha", "instance", "host", _u("i"))
        graph.retract(c, "alpha", "node", n)
        nstatus = c.execute("SELECT status FROM ws_alpha.nodes WHERE id = %s", (n,)).fetchone()[0]
        nhist = c.execute(
            "SELECT count(*) FROM ws_alpha.versions WHERE item_kind='node' AND item_id=%s", (n,)
        ).fetchone()[0]
        with pytest.raises(InvalidInput):
            graph.retract(c, "alpha", "bogus", f)
    assert (status, version, hist) == ("retracted", 2, 1)
    assert (nstatus, nhist) == ("retracted", 1)


def _fact_row(db, fid, ns="ws_alpha"):
    with db.admin_tx() as c:
        return c.execute(
            f"SELECT text, status, version FROM {ns}.facts WHERE id = %s", (fid,)
        ).fetchone()


def test_restore_fact_brings_back_old_text_and_status(db):
    with db.ws_tx("alpha") as c:
        fid = graph.add_fact(c, "alpha", "lesson", "a")
        graph.update_fact(c, "alpha", fid, text="b")
        graph.retract(c, "alpha", "fact", fid)
        v = graph.restore_fact(c, "alpha", fid, 1)
    assert v == 4
    assert _fact_row(db, fid) == ("a", "current", 4)
    with db.admin_tx() as c:
        n = c.execute(
            "SELECT count(*) FROM ws_alpha.versions WHERE item_kind='fact' AND item_id=%s", (fid,)
        ).fetchone()[0]
    assert n == 3


def test_restore_fact_unknown_version_or_current_version(db):
    with db.ws_tx("alpha") as c:
        fid = graph.add_fact(c, "alpha", "lesson", "a")
        with pytest.raises(InvalidInput, match="version not found"):
            graph.restore_fact(c, "alpha", fid, 9)
    with db.ws_tx("alpha") as c:
        fid = graph.add_fact(c, "alpha", "lesson", "a")
        assert graph.restore_fact(c, "alpha", fid, 1) == 2  # the current version: still snapshots
    with db.ws_tx("alpha") as c, pytest.raises(InvalidInput, match="fact not found"):
        graph.restore_fact(c, "alpha", str(uuid.uuid4()), 1)


def test_merge_nodes_moves_facts_edges_and_aliases(db):
    with db.ws_tx("alpha") as c:
        a = graph.add_node(c, "alpha", "instance", "db", _u("postgres"))
        b = graph.add_node(c, "alpha", "instance", "db", _u("postgresql"), aliases=["pg", "PG2"])
        other = graph.add_node(c, "alpha", "instance", "app", _u("app"))
        fid = graph.add_fact(c, "alpha", "fact", "f", node_id=b)
        e_in = graph.add_edge(c, "alpha", other, b, "uses")
        loop = graph.add_edge(c, "alpha", a, b, "depends_on")
        c.execute("UPDATE ws_alpha.nodes SET aliases = %s WHERE id = %s", (["pg2"], a))
        name_b = c.execute("SELECT name FROM ws_alpha.nodes WHERE id = %s", (b,)).fetchone()[0]
        graph.merge_nodes(c, "alpha", a, b)
        assert (
            c.execute("SELECT node_id::text FROM ws_alpha.facts WHERE id = %s", (fid,)).fetchone()[
                0
            ]
            == a
        )
        assert (
            c.execute("SELECT dst::text FROM ws_alpha.edges WHERE id = %s", (e_in,)).fetchone()[0]
            == a
        )
        assert (
            c.execute("SELECT status FROM ws_alpha.edges WHERE id = %s", (loop,)).fetchone()[0]
            == "retracted"
        )
        aliases = c.execute("SELECT aliases FROM ws_alpha.nodes WHERE id = %s", (a,)).fetchone()[0]
        assert sorted(x.lower() for x in aliases) == sorted(["pg2", "pg", name_b.lower()])
        assert (
            c.execute("SELECT status FROM ws_alpha.nodes WHERE id = %s", (b,)).fetchone()[0]
            == "superseded"
        )
        assert c.execute(
            "SELECT 1 FROM ws_alpha.edges WHERE src = %s AND dst = %s AND type = 'replaced_by'",
            (b, a),
        ).fetchone()
        reasons = c.execute(
            "SELECT item_kind, reason FROM ws_alpha.versions WHERE item_id IN (%s, %s, %s)",
            (a, b, fid),
        ).fetchall()
        assert ("fact", "merge") in reasons and reasons.count(("node", "merge")) == 2


def test_merge_nodes_refusals(db):
    with db.ws_tx("alpha") as c:
        a = graph.add_node(c, "alpha", "instance", "db", _u("x"))
        with pytest.raises(InvalidInput, match="same node"):
            graph.merge_nodes(c, "alpha", a, a)
        with pytest.raises(InvalidInput, match="node not found"):
            graph.merge_nodes(c, "alpha", a, str(uuid.uuid4()))
        with pytest.raises(InvalidInput, match="unknown namespace"):
            graph.merge_nodes(c, "general", a, a)


def test_merge_nodes_refuses_non_current_either_side(db):
    with db.ws_tx("alpha") as c:
        a = graph.add_node(c, "alpha", "instance", "db", _u("a"))
        b = graph.add_node(c, "alpha", "instance", "db", _u("b"))
        n = graph.add_node(c, "alpha", "instance", "db", _u("n"))
        graph.merge_nodes(c, "alpha", a, b)  # b is superseded now
        with pytest.raises(InvalidInput, match="node not current"):
            graph.merge_nodes(c, "alpha", a, b)  # gone not current
        with pytest.raises(InvalidInput, match="node not current"):
            graph.merge_nodes(c, "alpha", b, n)  # keep not current
        graph.retract(c, "alpha", "node", n)
        with pytest.raises(InvalidInput, match="node not current"):
            graph.merge_nodes(c, "alpha", a, n)


def _versions(c, kind, item_id):
    return c.execute(
        "SELECT count(*) FROM ws_alpha.versions WHERE item_kind = %s AND item_id = %s",
        (kind, item_id),
    ).fetchone()[0]


def test_retract_twice_is_a_no_op(db):
    with db.ws_tx("alpha") as c:
        f = graph.add_fact(c, "alpha", "fact", "retract me twice")
        graph.retract(c, "alpha", "fact", f)
        graph.retract(c, "alpha", "fact", f)
        version = c.execute("SELECT version FROM ws_alpha.facts WHERE id = %s", (f,)).fetchone()
        assert (version[0], _versions(c, "fact", f)) == (2, 1)
        n = graph.add_node(c, "alpha", "instance", "host", _u("i"))
        graph.retract(c, "alpha", "node", n)
        graph.retract(c, "alpha", "node", n)
        assert _versions(c, "node", n) == 1
        with pytest.raises(NotFound):
            graph.retract(c, "alpha", "fact", str(uuid.uuid4()))


def test_retract_locks_the_row(db, monkeypatch):
    seen = []
    with db.ws_tx("alpha") as c:
        f = graph.add_fact(c, "alpha", "fact", "lock me")
        real = c.execute

        def spy(query, *a, **k):
            seen.append(query if isinstance(query, str) else query.as_string(c))
            return real(query, *a, **k)

        monkeypatch.setattr(c, "execute", spy)
        graph.retract(c, "alpha", "fact", f)
    assert "FOR UPDATE" in seen[0]


def test_update_fact_needs_text_or_status(db):
    with db.ws_tx("alpha") as c:
        f = graph.add_fact(c, "alpha", "fact", "nothing to do")
        with pytest.raises(InvalidInput, match="nothing to update"):
            graph.update_fact(c, "alpha", f)
        assert _versions(c, "fact", f) == 0


def test_add_fact_unknown_revision_is_invalid_input(db):
    with db.ws_tx("alpha") as c, pytest.raises(InvalidInput, match="revision"):
        graph.add_fact(c, "alpha", "fact", "t", revision_id=str(uuid.uuid4()))


def test_add_node_refines_only_a_current_concept(db):
    g = _general_concept(db)
    with db.publisher_tx() as c:
        c.execute("UPDATE general.nodes SET status = 'superseded' WHERE id = %s", (g,))
    with db.ws_tx("alpha") as c, pytest.raises(InvalidInput, match="refines"):
        graph.add_node(c, "alpha", "local_concept", "tool", _u("l"), refines=g)


def test_add_edge_rejects_self_loops(db):
    with db.ws_tx("alpha") as c:
        a = graph.add_node(c, "alpha", "instance", "host", _u("i"))
        with pytest.raises(InvalidInput, match="self"):
            graph.add_edge(c, "alpha", a, a, "uses")


def test_missing_items_raise_not_found(db):
    with db.ws_tx("alpha") as c:
        for call in (
            lambda: graph.update_fact(c, "alpha", str(uuid.uuid4()), text="x"),
            lambda: graph.fact_state(c, "alpha", str(uuid.uuid4())),
            lambda: graph.set_status(c, "alpha", "fact", str(uuid.uuid4()), "current", "r"),
            lambda: graph.restore_fact(c, "alpha", str(uuid.uuid4()), 1),
        ):
            with pytest.raises(NotFound):
                call()
        with pytest.raises(InvalidInput) as e:
            graph.add_fact(c, "alpha", "bogus", "t")
        assert not isinstance(e.value, NotFound)
