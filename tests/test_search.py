import uuid

import pytest
from db_helpers import DAY1, _journal, _revision
from fakes import FakeEmbedder

from treant import graph, migrate, names, worker
from treant.search import search
from treant.tokens import TokenContext


def _marker() -> str:
    return "zq" + uuid.uuid4().hex[:12]


@pytest.fixture
def ctx_iso(db, admin_dsn, prefix):
    """A token context for a fresh, uniquely named workspace no other test writes to."""
    name = "sr" + uuid.uuid4().hex[:8]
    migrate.create_workspace(admin_dsn, prefix, name)
    return TokenContext("c1c1c1c1c1c1", name, frozenset({"read"}))


def _embed_ws(db, ws, emb, journals=False):
    """Embed everything pending in ``ws``; a sweep is bounded (oldest first), so loop."""
    schema = worker.sql.Identifier(names.ws_schema(ws))
    while True:
        with db.ws_tx(ws) as conn:
            items = worker._pending(conn, schema, journals)
        if not items:
            return
        vecs = worker._embed_all(emb, items)
        with db.ws_tx(ws) as conn:
            worker._upsert(conn, schema, items, vecs)


def test_search_finds_own_and_general_only(db, ctx_a):
    m, emb = _marker(), FakeEmbedder()
    with db.ws_tx("alpha") as c:
        a = graph.add_fact(c, "alpha", "fact", f"alpha note {m}")
    with db.ws_tx("beta") as c:
        b = graph.add_fact(c, "beta", "fact", f"beta note {m}")
    with db.publisher_tx() as c:
        g = graph.add_fact(c, "general", "fact", f"general note {m}")
    _embed_ws(db, "alpha", emb)
    _embed_ws(db, "beta", emb)
    worker.embed_general(db, emb)
    res = search(db, emb, ctx_a, m, limit=20)
    assert res.mode == "hybrid"
    ids = {h["id"]: h["namespace"] for h in res.hits}
    assert ids.get(a) == "workspace" and ids.get(g) == "general"
    assert b not in ids
    assert {h["namespace"] for h in res.hits} <= {"workspace", "general"}


def test_search_limit_clamped(db, ctx_a):
    m, emb = _marker(), FakeEmbedder()
    with db.ws_tx("alpha") as c:
        for i in range(25):
            graph.add_fact(c, "alpha", "fact", f"{m} item {i}")
    _embed_ws(db, "alpha", emb)
    assert len(search(db, emb, ctx_a, m, limit=100).hits) == 20
    assert len(search(db, emb, ctx_a, m, limit=0).hits) == 1
    assert len(search(db, emb, ctx_a, m, limit=-5).hits) == 1


def test_search_walk_adds_neighbour_facts(db, ctx_iso):
    # Fixed words: the workspace is fresh, so no other test's text can collide with them.
    ws, emb = ctx_iso.workspace, FakeEmbedder()
    with db.ws_tx(ws) as c:
        n1 = graph.add_node(c, ws, "instance", "service", "svcwalkq")
        n2 = graph.add_node(c, ws, "instance", "service", "othernode")
        graph.add_edge(c, ws, n1, n2, "uses")
        neighbour = graph.add_fact(c, ws, "fact", "unrelated words entirely", node_id=n2)
        for i in range(20):
            graph.add_fact(c, ws, "fact", f"svcwalkq distractor {i}")
    _embed_ws(db, ws, emb)
    res = search(db, emb, ctx_iso, "svcwalkq", limit=8)
    by_id = {h["id"]: h for h in res.hits}
    assert len(res.hits) == 8
    assert neighbour in by_id
    # Reached only by the walk: scored below every direct hit of the workspace. (General hits
    # come from the shared database and may themselves be walk hits of equal score.)
    others = [
        h["score"] for h in res.hits if h["id"] != neighbour and h["namespace"] == "workspace"
    ]
    assert others
    assert by_id[neighbour]["score"] < min(others)
    assert by_id[neighbour]["score"] > 0


def test_search_walk_hits_ranked_by_own_similarity(db, ctx_iso):
    ws, emb = ctx_iso.workspace, FakeEmbedder()
    with db.ws_tx(ws) as c:
        n1 = graph.add_node(c, ws, "instance", "service", "kestrelhub")
        n2 = graph.add_node(c, ws, "instance", "service", "othernode")
        graph.add_edge(c, ws, n1, n2, "uses")
        close = graph.add_fact(c, ws, "fact", "kestrel nesting notes for spring season", node_id=n2)
        far = [
            graph.add_fact(c, ws, "fact", text, node_id=n2)
            for text in ("unrelated words entirely", "nothing in common here", "plain line")
        ]
        for i in range(20):
            graph.add_fact(c, ws, "fact", f"kestrelhub distractor {i}")
    _embed_ws(db, ws, emb)
    walk_ids = {close, *far}
    # One walk slot at limit 4: the walk hit closest to the query takes it.
    res = search(db, emb, ctx_iso, "kestrelhub kestrel", limit=4)
    assert [h["id"] for h in res.hits if h["id"] in walk_ids] == [close]
    res = search(db, emb, ctx_iso, "kestrelhub kestrel", limit=8)
    walked = {h["id"]: h["score"] for h in res.hits if h["id"] in walk_ids}
    direct = [h["score"] for h in res.hits if h["id"] not in walk_ids]
    assert len(walked) == 2 and close in walked
    assert all(walked[close] > s for i, s in walked.items() if i != close)
    assert max(walked.values()) < min(direct)


def test_search_leftover_slots_go_to_walk_hits(fresh_db):
    ctx = TokenContext("c2c2c2c2c2c2", "alpha", frozenset({"read"}))
    emb = FakeEmbedder()
    with fresh_db.ws_tx("alpha") as c:
        n1 = graph.add_node(c, "alpha", "instance", "service", "kestrelhub")
        n2 = graph.add_node(c, "alpha", "instance", "service", "othernode")
        graph.add_edge(c, "alpha", n1, n2, "uses")
    _embed_ws(fresh_db, "alpha", emb)
    with fresh_db.ws_tx("alpha") as c:  # added after the embed run: reachable by the walk only
        walk_ids = {graph.add_fact(c, "alpha", "fact", f"note {i}", node_id=n2) for i in range(5)}
    res = search(fresh_db, emb, ctx, "kestrelhub service", limit=8)
    ids = [h["id"] for h in res.hits]
    # Two direct hits (the nodes) leave six slots; all five walk hits fill them.
    assert set(ids) == {n1, n2} | walk_ids
    walk_scores = [h["score"] for h in res.hits if h["id"] in walk_ids]
    assert max(walk_scores) < min(h["score"] for h in res.hits if h["id"] not in walk_ids)


def test_search_keyword_fallback_when_embedder_down(db, ctx_a):
    m = _marker()
    with db.ws_tx("alpha") as c:
        hit = graph.add_fact(c, "alpha", "fact", f"literal 100% {m}_x text")
        graph.add_fact(c, "alpha", "fact", f"literal 100 {m}Yx text")
    res = search(db, FakeEmbedder(down=True), ctx_a, f"% {m}_x", limit=20)
    assert res.mode == "keyword"
    assert [h["id"] for h in res.hits] == [hit]


def test_keyword_alias_match_is_per_alias(db, ctx_iso):
    ws = ctx_iso.workspace
    with db.ws_tx(ws) as c:
        n = graph.add_node(c, ws, "instance", "host", "plainname", aliases=["kestrel", "osprey"])
    down = FakeEmbedder(down=True)
    assert [h["id"] for h in search(db, down, ctx_iso, "osprey").hits] == [n]
    assert search(db, down, ctx_iso, "kestrel osprey").hits == []  # spans two aliases


def test_keyword_escapes_each_wildcard(db, ctx_iso):
    ws = ctx_iso.workspace
    down = FakeEmbedder(down=True)
    with db.ws_tx(ws) as c:
        under = graph.add_fact(c, ws, "fact", "name foo_bar here")
        graph.add_fact(c, ws, "fact", "name fooXbar here")  # matches an unescaped _
        pct = graph.add_fact(c, ws, "fact", "rate 50% off")
        graph.add_fact(c, ws, "fact", "rate 50 percent off")  # matches an unescaped %
        slash = graph.add_fact(c, ws, "fact", "path a\\b here")
        graph.add_fact(c, ws, "fact", "path ab here")  # matches if \\ escapes the b
    assert [h["id"] for h in search(db, down, ctx_iso, "foo_bar").hits] == [under]
    assert [h["id"] for h in search(db, down, ctx_iso, "50% off").hits] == [pct]
    assert [h["id"] for h in search(db, down, ctx_iso, "a\\b").hits] == [slash]


def test_keyword_search_stays_in_the_workspace(db, ctx_iso):
    m = _marker()
    with db.ws_tx("beta") as c:
        graph.add_fact(c, "beta", "fact", f"beta only {m}")
        graph.add_node(c, "beta", "instance", "host", f"node{m}")
    with db.publisher_tx() as c:
        g = graph.add_fact(c, "general", "fact", f"general {m}")
    hits = search(db, FakeEmbedder(down=True), ctx_iso, m).hits
    assert [(h["namespace"], h["id"]) for h in hits] == [("general", g)]


def test_non_current_items_are_never_hits(db, ctx_iso):
    ws, emb = ctx_iso.workspace, FakeEmbedder()
    with db.ws_tx(ws) as c:
        live = graph.add_fact(c, ws, "fact", "heronfeather live")
        gone = [graph.add_fact(c, ws, "fact", f"heronfeather {s}") for s in ("old", "wrong")]
        node = graph.add_node(c, ws, "instance", "host", "heronfeather")
    _embed_ws(db, ws, emb)
    with db.ws_tx(ws) as c:
        graph.update_fact(c, ws, gone[0], status="superseded")
        graph.retract(c, ws, "fact", gone[1])
        graph.retract(c, ws, "node", node)
    for embedder in (emb, FakeEmbedder(down=True)):
        ids = {h["id"] for h in search(db, embedder, ctx_iso, "heronfeather").hits}
        assert live in ids and not ids & {*gone, node}


def test_hits_are_sorted_and_unique(db, ctx_iso):
    ws, emb = ctx_iso.workspace, FakeEmbedder()
    with db.ws_tx(ws) as c:
        a = graph.add_node(c, ws, "instance", "service", "plover")
        b = graph.add_node(c, ws, "instance", "service", "sandpiper")
        graph.add_edge(c, ws, a, b, "uses")
        graph.add_edge(c, ws, b, a, "depends_on")
        graph.add_fact(c, ws, "fact", "plover sandpiper shared", node_id=a)
        graph.add_fact(c, ws, "fact", "plover sandpiper shared too", node_id=b)
        for i in range(6):
            graph.add_fact(c, ws, "fact", f"plover note {i}", node_id=a)
    _embed_ws(db, ws, emb)
    hits = search(db, emb, ctx_iso, "plover sandpiper", limit=20).hits
    keys = [(h["namespace"], h["kind"], h["id"]) for h in hits]
    assert len(keys) == len(set(keys))
    assert [(-h["score"], h["id"]) for h in hits] == sorted((-h["score"], h["id"]) for h in hits)


def test_walk_hits_stay_below_direct_hits_that_score_zero(fresh_db):
    ctx = TokenContext("c3c3c3c3c3c3", "alpha", frozenset({"read"}))
    emb = FakeEmbedder()
    with fresh_db.ws_tx("alpha") as c:
        n1 = graph.add_node(c, "alpha", "instance", "service", "kestrelhub")
        n2 = graph.add_node(c, "alpha", "instance", "service", "othernode")
        graph.add_edge(c, "alpha", n1, n2, "uses")
    _embed_ws(fresh_db, "alpha", emb)
    with fresh_db.ws_tx("alpha") as c:  # reachable by the walk only
        walk_ids = {graph.add_fact(c, "alpha", "fact", f"note {i}", node_id=n2) for i in range(3)}
    query = "zzqqxx"  # shares no word with anything stored: every direct hit scores 0
    assert emb.embed_query(query) != emb.embed_query("kestrelhub")
    res = search(fresh_db, emb, ctx, query, limit=8)
    direct = [h["score"] for h in res.hits if h["id"] not in walk_ids]
    walked = [h["score"] for h in res.hits if h["id"] in walk_ids]
    assert direct and walked and min(direct) == 0
    assert max(walked) < min(direct)


def _link_revision(db, ws, fact_id, revision_id):
    with db.admin_tx() as c:
        c.execute(
            f"UPDATE {names.ws_schema(ws)}.facts SET revision_id = %s WHERE id = %s",
            (revision_id, fact_id),
        )


def test_hits_carry_source(db, ctx_iso):
    ws, emb, m = ctx_iso.workspace, FakeEmbedder(), _marker()
    with db.ws_tx(ws) as c:
        plain = graph.add_fact(c, ws, "fact", f"plain {m}")
        linked = graph.add_fact(c, ws, "fact", f"linked {m}")
    _link_revision(db, ws, linked, _revision(db, ws, "sess-one", DAY1))
    _embed_ws(db, ws, emb)
    res = search(db, emb, ctx_iso, m, limit=8)
    by_id = {h["id"]: h for h in res.hits}
    assert all("saved" in h["source"] for h in res.hits)
    assert by_id[linked]["source"]["session"] == "sess-one"
    assert by_id[linked]["source"]["confirmed"] == "2026-11-20"
    assert "session" not in by_id[plain]["source"]


def test_journal_hit_carries_source_session(db, ctx_iso):
    ws, emb, m = ctx_iso.workspace, FakeEmbedder(), _marker()
    rev = _revision(db, ws, "sess-journal", DAY1)
    jid = _journal(db, ws, rev, f"journal {m}")
    _embed_ws(db, ws, emb, journals=True)
    res = search(db, emb, ctx_iso, m, limit=8)
    hit = next(h for h in res.hits if h["id"] == jid)
    assert hit["source"]["session"] == "sess-journal"
    assert hit["source"]["confirmed"] == "2026-11-20"


def test_keyword_mode_hits_carry_source(db, ctx_iso):
    ws, m = ctx_iso.workspace, _marker()
    with db.ws_tx(ws) as c:
        graph.add_fact(c, ws, "fact", f"keyword {m}")
    res = search(db, FakeEmbedder(down=True), ctx_iso, m, limit=8)
    assert res.mode == "keyword" and res.hits
    assert all("saved" in h["source"] for h in res.hits)


def test_walked_hits_carry_source(db, ctx_iso):
    ws, emb = ctx_iso.workspace, FakeEmbedder()
    with db.ws_tx(ws) as c:
        n1 = graph.add_node(c, ws, "instance", "service", "svcsrcq")
        n2 = graph.add_node(c, ws, "instance", "service", "othersrc")
        graph.add_edge(c, ws, n1, n2, "uses")
        neighbour = graph.add_fact(c, ws, "fact", "unrelated words entirely", node_id=n2)
        for i in range(20):
            graph.add_fact(c, ws, "fact", f"svcsrcq distractor {i}")
    _embed_ws(db, ws, emb)
    res = search(db, emb, ctx_iso, "svcsrcq", limit=8)
    by_id = {h["id"]: h for h in res.hits}
    assert neighbour in by_id
    assert all("saved" in h["source"] for h in res.hits)
    # Reached only by the walk: scored below every direct hit of the workspace.
    others = [
        h["score"] for h in res.hits if h["id"] != neighbour and h["namespace"] == "workspace"
    ]
    assert others
    assert by_id[neighbour]["score"] < min(others)
