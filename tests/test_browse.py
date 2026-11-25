import uuid
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb

from treant import apply, browse, graph, jobs
from treant.errors import InvalidInput


def _queue(db, ws, item, status="pending", reasons=()):
    source = item.get("source_text") or item.get("name") or item["text"]
    item = {**item, "source_text": source}
    with db.publisher_tx() as conn:
        return conn.execute(
            "INSERT INTO control.general_queue "
            "(workspace, session_id, revision_id, item, item_key, status, reasons) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                ws,
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                Jsonb(item),
                apply.item_key(item["kind"], uuid.uuid4().hex),
                status,
                Jsonb(list(reasons)),
            ),
        ).fetchone()[0]


def _fact(text, type="lesson"):
    return {"kind": "fact", "type": type, "text": text, "concept": None}


def _concept(name):
    return {"kind": "concept", "type": "tool", "name": name, "aliases": []}


def test_queue_flags_near_duplicates(fresh_db):
    a = _queue(fresh_db, "alpha", _fact("Use  Terraform plan before apply, always"))
    other = _queue(fresh_db, "beta", _fact("completely different advice about helm charts"))
    b = _queue(fresh_db, "beta", _fact("use terraform plan before apply always"))
    c = _queue(fresh_db, "alpha", _fact("use terraform plan before apply, always!"))
    done = _queue(fresh_db, "alpha", _fact("use terraform plan before apply always"), "published")
    kind_differs = _queue(fresh_db, "alpha", _concept("use terraform plan before apply always"))
    rows = {r["id"]: r for r in browse.queue(fresh_db, None, None)}
    assert rows[a]["similar_to"] is None
    assert rows[other]["similar_to"] is None
    assert rows[b]["similar_to"] == a
    assert rows[c]["similar_to"] == a  # earliest similar earlier id, not b
    assert rows[done]["similar_to"] is None  # not open
    assert rows[kind_differs]["similar_to"] is None
    # paging does not change the hint
    page = browse.queue(fresh_db, None, None, limit=1, offset=2)
    assert [r["id"] for r in page] == [b] and page[0]["similar_to"] == a


def test_queue_rows_hide_source_text_and_show_labels(fresh_db):
    f = _queue(
        fresh_db,
        "alpha",
        _fact("fact text"),
        reasons=[
            {"category": "domain", "start": 0, "end": 3},
            {"category": "domain", "start": 5, "end": 6},
            {"category": "term", "start": 1, "end": 2},
        ],
    )
    c = _queue(fresh_db, "alpha", _concept("Kubernetes"), "blocked")
    rows = {r["id"]: r for r in browse.queue(fresh_db, None, None)}
    assert rows[f]["text"] == "fact text" and rows[f]["categories"] == ["domain", "term"]
    assert rows[c]["text"] == "Kubernetes" and rows[c]["kind"] == "concept"
    assert all("source_text" not in r for r in rows.values())
    assert [r["id"] for r in browse.queue(fresh_db, "blocked", None)] == [c]
    assert [r["id"] for r in browse.queue(fresh_db, None, "beta")] == []
    item = browse.queue_item(fresh_db, f)
    assert item["source_text"] == "fact text"
    assert item["reasons"][0] == {"category": "domain", "start": 0, "end": 3}
    assert browse.queue_item(fresh_db, 10**9) is None


def test_limit_is_capped(fresh_db):
    with fresh_db.control_tx() as conn:
        for i in range(600):
            jobs.enqueue(conn, "alpha", "gate", f"k{i}", {})
    assert len(browse.jobs(fresh_db, None, None, None, limit=10_000)) == 500
    assert len(browse.jobs(fresh_db, None, None, None)) == 100
    assert len(browse.jobs(fresh_db, None, None, None, limit=500, offset=550)) == 50
    assert len(browse.jobs(fresh_db, None, "beta", None)) == 0
    assert len(browse.jobs(fresh_db, "queued", "alpha", "gate", limit=1, offset=-5)) == 1


def test_node_detail_includes_retracted_facts_and_edge_names(fresh_db):
    with fresh_db.ws_tx("alpha") as conn:
        a = graph.add_node(conn, "alpha", "instance", "cluster", "node-aaa")
        b = graph.add_node(conn, "alpha", "instance", "cluster", "node-bbb")
        c = graph.add_node(conn, "alpha", "instance", "cluster", "node-ccc")
        graph.add_edge(conn, "alpha", a, b, "uses")
        graph.add_edge(conn, "alpha", c, a, "depends_on")
        keep = graph.add_fact(conn, "alpha", "fact", "kept", a)
        gone = graph.add_fact(conn, "alpha", "fact", "gone", a)
        graph.retract(conn, "alpha", "fact", gone)
        sid = conn.execute(
            "INSERT INTO ws_alpha.sessions (token_id, client_session_id) VALUES ('t', 's') "
            "RETURNING id"
        ).fetchone()[0]
        revs = []
        for n in (1, 2, 3):
            rid = conn.execute(
                "INSERT INTO ws_alpha.revisions (session_id, n, sections, created_at) "
                "VALUES (%s, %s, '{}', %s) RETURNING id",
                (sid, n, datetime(2026, 1, n, tzinfo=UTC)),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO ws_alpha.journal_entries (revision_id, text, created_at) "
                "VALUES (%s, %s, %s)",
                (rid, f"entry {n}", datetime(2026, 1, n, tzinfo=UTC)),
            )
            revs.append(str(rid))
        graph.add_support(conn, "alpha", "node", a, revs[0])
        graph.add_support(conn, "alpha", "fact", keep, revs[1])  # rev 3 supports nothing
    d = browse.node(fresh_db, "alpha", a)
    assert d["name"] == "node-aaa"
    assert {f["text"]: f["status"] for f in d["facts"]} == {"kept": "current", "gone": "retracted"}
    out = [e for e in d["edges"] if e["direction"] == "out"]
    inc = [e for e in d["edges"] if e["direction"] == "in"]
    assert [(e["other_name"], e["type"]) for e in out] == [("node-bbb", "uses")]
    assert [(e["other_name"], e["type"]) for e in inc] == [("node-ccc", "depends_on")]
    assert [j["text"] for j in d["journal"]] == ["entry 2", "entry 1"]
    assert browse.node(fresh_db, "alpha", str(uuid.uuid4())) is None
    assert browse.node(fresh_db, "alpha", "not-a-uuid") is None
    assert [n["name"] for n in browse.nodes(fresh_db, "alpha", "NODE-b", 100, 0)] == ["node-bbb"]
    assert len(browse.nodes(fresh_db, "alpha", None, 100, 0)) == 3
    assert browse.nodes(fresh_db, "alpha", "100%", 100, 0) == []


def test_fact_history_is_ordered(fresh_db):
    with fresh_db.ws_tx("alpha") as conn:
        n = graph.add_node(conn, "alpha", "instance", "cluster", "n1")
        f = graph.add_fact(conn, "alpha", "fact", "v1 text", n)
        graph.update_fact(conn, "alpha", f, "v2 text")
        graph.update_fact(conn, "alpha", f, "v3 text")
    h = browse.fact_history(fresh_db, "alpha", f)
    assert [x["version"] for x in h] == [1, 2, 3]
    assert [x["data"]["text"] for x in h] == ["v1 text", "v2 text", "v3 text"]
    assert h[0]["reason"] and h[-1]["current"] is True and not h[0]["current"]
    assert browse.fact_history(fresh_db, "alpha", str(uuid.uuid4())) == []


def test_measurements_shape(fresh_db):
    empty = browse.measurements(fresh_db)
    assert set(empty["workspaces"]) == {"alpha", "beta", "gamma"}
    w = empty["workspaces"]["alpha"]
    assert set(w) == {"queue", "jobs", "gate_blocks", "searches_7d", "mistakes_30d"}
    assert w["searches_7d"] is None and w["mistakes_30d"] == 0
    assert empty["isolation"] is None
    assert empty["canaries"] == [("alpha", 0), ("beta", 0), ("gamma", 0)]
    _queue(fresh_db, "alpha", _fact("x"), "blocked", [{"category": "domain", "start": 0, "end": 1}])
    _queue(fresh_db, "alpha", _fact("y"), "pending")
    with fresh_db.control_tx() as conn:
        jobs.enqueue(conn, "alpha", "gate", "k", {})
        conn.execute("INSERT INTO control.mistakes (workspace, text) VALUES ('alpha', 'm')")
        conn.execute(
            "INSERT INTO control.mistakes (workspace, text, at) VALUES ('alpha', 'old', %s)",
            (datetime.now(UTC) - timedelta(days=40),),
        )
        conn.execute("INSERT INTO control.metrics (name, value) VALUES ('isolation.faults', 0)")
        conn.execute("INSERT INTO control.metrics (name, value) VALUES ('isolation.checks', 12)")
    m = browse.measurements(fresh_db)
    w = m["workspaces"]["alpha"]
    assert w["queue"] == {"blocked": 1, "pending": 1}
    assert w["jobs"] == {"queued": 1}
    assert w["gate_blocks"] == {"domain": 1}
    assert w["mistakes_30d"] == 1
    assert m["isolation"]["faults"] == 0 and m["isolation"]["checks"] == 12
    assert m["isolation"]["at"] is not None


def test_general_namespace_browse(fresh_db):
    assert browse.nodes(fresh_db, "general", None, 100, 0) == []
    assert browse.node(fresh_db, "general", str(uuid.uuid4())) is None
    for bad in ("control", "Bad Name", "../x", ""):
        with pytest.raises(InvalidInput, match="unknown namespace"):
            browse.nodes(fresh_db, bad, None, 100, 0)
    with pytest.raises(browse.Refused, match="workspace not available"):
        browse.nodes(fresh_db, "nosuchws", None, 100, 0)


def test_general_node_has_no_journal(fresh_db):
    with fresh_db.publisher_tx() as conn:
        gid = graph.add_node(conn, "general", "concept", "tool", "Terraform")
    d = browse.node(fresh_db, "general", gid)
    assert d["journal"] == [] and d["name"] == "Terraform"


def test_pairs_and_resolution_filter(fresh_db):
    with fresh_db.ws_tx("alpha") as conn:
        a = graph.add_node(conn, "alpha", "instance", "cluster", "na")
        b = graph.add_node(conn, "alpha", "instance", "cluster", "nb")
        c = graph.add_node(conn, "alpha", "instance", "cluster", "nc")
        conn.execute(
            "INSERT INTO ws_alpha.merge_flags (node_a, node_b, score) VALUES (%s,%s,.9)", (a, b)
        )
        conn.execute(
            "INSERT INTO ws_alpha.merge_flags (node_a, node_b, score, resolved_at, resolution) "
            "VALUES (%s,%s,.8, now(), 'dismissed')",
            (a, c),
        )
        f1 = graph.add_fact(conn, "alpha", "fact", "old", a)
        f2 = graph.add_fact(conn, "alpha", "fact", "new", a)
        conn.execute("INSERT INTO ws_alpha.conflicts (new_fact, old_fact) VALUES (%s,%s)", (f2, f1))
    open_ = browse.merges(fresh_db, "alpha")
    assert [(m["name_a"], m["name_b"]) for m in open_] == [("na", "nb")]
    assert "resolved_at" not in open_[0]
    every = browse.merges(fresh_db, "alpha", open_only=False)
    assert [m["resolution"] for m in every] == [None, "dismissed"]
    assert every[1]["resolved_at"] is not None
    cf = browse.conflicts(fresh_db, "alpha")
    assert cf[0]["new_text"] == "new" and cf[0]["old_text"] == "old"
    with pytest.raises(browse.Refused):
        browse.merges(fresh_db, "nosuchws")


def test_workspaces_sessions_quarantine_mistakes_audit(fresh_db):
    from treant import owner

    tok = owner.create_token(fresh_db, "cli", "alpha", "lbl", {"read"})
    owner.revoke_token(fresh_db, "cli", tok.split("_")[1])
    owner.create_token(fresh_db, "cli", "alpha", "lbl2", {"read"})
    with fresh_db.ws_tx("alpha") as conn:
        sid = conn.execute(
            "INSERT INTO ws_alpha.sessions (token_id, client_session_id) VALUES (%s, 'cs1') "
            "RETURNING id",
            (tok.split("_")[1],),
        ).fetchone()[0]
        rid = conn.execute(
            "INSERT INTO ws_alpha.revisions (session_id, n, sections, status, removed) "
            "VALUES (%s, 1, %s, 'quarantined', 2) RETURNING id",
            (sid, Jsonb({"journal": "SECRET"})),
        ).fetchone()[0]
        conn.execute("INSERT INTO control.mistakes (workspace, text) VALUES ('alpha', 'oops')")
    with fresh_db.control_tx() as conn:
        jobs.enqueue(conn, "alpha", "distill", str(rid), {})
    ws = {w["name"]: w for w in browse.workspaces(fresh_db)}
    assert ws["alpha"]["tokens_active"] == 1 and ws["alpha"]["tokens_revoked"] == 1
    assert ws["alpha"]["quarantined"] == 1 and ws["beta"]["quarantined"] == 0
    assert ws["alpha"]["queue_open"] == 0 and ws["alpha"]["jobs_dead"] == 0
    assert "publish" in ws["alpha"]["settings"]
    q = browse.quarantined(fresh_db, "alpha")
    assert q == [
        {
            "workspace": "alpha",
            "revision_id": str(rid),
            "created_at": q[0]["created_at"],
            "removed": 2,
        }
    ]
    assert "SECRET" not in str(browse.quarantined(fresh_db, None))
    s = browse.sessions(fresh_db, "alpha", 100, 0)
    assert s[0]["client_session_id"] == "cs1" and s[0]["token_label"] == "lbl"
    rev = s[0]["revisions"][0]
    assert (rev["n"], rev["status"], rev["has_generation"], rev["distill_status"]) == (
        1,
        "quarantined",
        False,
        "queued",
    )
    assert "SECRET" not in str(s)
    assert [m["text"] for m in browse.mistakes(fresh_db, "alpha", 100, 0)] == ["oops"]
    assert browse.mistakes(fresh_db, "beta", 100, 0) == []
    a = browse.audit(fresh_db, 100, 0, "token.")
    assert {x["action"] for x in a} == {"token.create", "token.revoke"}
    assert a[0]["id"] > a[-1]["id"]
    assert browse.audit(fresh_db, 100, 0, None)


def test_targets_listed_per_workspace(fresh_db):
    from treant import owner

    owner_refused = owner.Refused
    owner.add_target(fresh_db, "cli", "alpha", "kube_context", "ctx-b")
    owner.add_target(fresh_db, "cli", "alpha", "aws_profile", "prof")
    assert browse.targets(fresh_db, "alpha") == [
        {"kind": "aws_profile", "value": "prof"},
        {"kind": "kube_context", "value": "ctx-b"},
    ]
    assert browse.targets(fresh_db, "beta") == []
    with pytest.raises(owner_refused):
        browse.targets(fresh_db, "nosuchws")


def test_label_of_concept_without_name_is_empty():
    assert browse._label({"kind": "concept", "name": None, "text": "t"}) == ""
    assert browse._label({"kind": "fact", "text": None}) == ""


def test_bad_paging_is_invalid_input(fresh_db):
    for limit, offset in (("x", 0), (10, "y"), (None, 0)):
        with pytest.raises(InvalidInput, match="bad paging"):
            browse.queue(fresh_db, None, None, limit, offset)


def test_queue_without_hints(fresh_db):
    _queue(fresh_db, "alpha", _fact("Use Terraform plan before apply, always"))
    b = _queue(fresh_db, "alpha", _fact("Use terraform plan before apply always"))
    assert {r["id"]: r["similar_to"] for r in browse.queue(fresh_db, None, None)}[b] is not None
    assert all(r["similar_to"] is None for r in browse.queue(fresh_db, None, None, hints=False))


def test_fact_history_current_has_explicit_columns(fresh_db):
    with fresh_db.ws_tx("alpha") as conn:
        n = graph.add_node(conn, "alpha", "instance", "cluster", "n1")
        f = graph.add_fact(conn, "alpha", "fact", "v1 text", n)
    current = browse.fact_history(fresh_db, "alpha", f)[-1]
    assert set(current["data"]) == {
        "id",
        "type",
        "text",
        "node_id",
        "status",
        "version",
        "created_at",
    }
    assert current["data"]["node_id"] == n and current["data"]["text"] == "v1 text"


def test_single_workspace_lookup(fresh_db):
    w = browse.workspace(fresh_db, "alpha")
    assert w["name"] == "alpha" and w == next(
        x for x in browse.workspaces(fresh_db) if x["name"] == "alpha"
    )
    assert browse.workspace(fresh_db, "zeta") is None


def test_graph_has_current_nodes_and_edges_and_refined_concepts(fresh_db):
    with fresh_db.publisher_tx() as conn:
        concept = graph.add_node(conn, "general", "concept", "tool", "terraform")
        graph.add_node(conn, "general", "concept", "tool", "unrelated")
    with fresh_db.ws_tx("alpha") as conn:
        a = graph.add_node(conn, "alpha", "instance", "service", "api")
        b = graph.add_node(conn, "alpha", "instance", "db", "postgres")
        c = graph.add_node(conn, "alpha", "local_concept", "tool", "our-tf", refines=concept)
        gone = graph.add_node(conn, "alpha", "instance", "db", "old-db")
        graph.add_edge(conn, "alpha", a, b, "uses")
        dead = graph.add_edge(conn, "alpha", a, gone, "uses")
        graph.retract(conn, "alpha", "node", gone)
        graph.retract(conn, "alpha", "edge", dead)
    g = browse.graph(fresh_db, "alpha")
    names = {n["name"]: n for n in g["nodes"]}
    assert set(names) == {"api", "postgres", "our-tf", "terraform"}
    assert names["terraform"]["general"] and not names["api"]["general"]
    assert {(e["src"], e["dst"], e["type"]) for e in g["edges"]} == {
        (a, b, "uses"),
        (c, concept, "refines"),
    }
    assert g["total"] == 3 and not g["truncated"]
    general = browse.graph(fresh_db, "general")
    assert {n["name"] for n in general["nodes"]} == {"terraform", "unrelated"}
    assert all(n["general"] for n in general["nodes"])


def test_graph_keeps_the_best_connected_nodes_when_capped(fresh_db):
    with fresh_db.ws_tx("alpha") as conn:
        hub = graph.add_node(conn, "alpha", "instance", "service", "hub")
        spokes = [graph.add_node(conn, "alpha", "instance", "db", f"s{i}") for i in range(3)]
        for s in spokes:
            graph.add_edge(conn, "alpha", hub, s, "uses")
        for i in range(4):
            graph.add_node(conn, "alpha", "instance", "db", f"lonely{i}")
    g = browse.graph(fresh_db, "alpha", limit=4)
    assert {n["name"] for n in g["nodes"]} == {"hub", "s0", "s1", "s2"}
    assert len(g["edges"]) == 3 and g["total"] == 8 and g["truncated"]
    with pytest.raises(InvalidInput):
        browse.graph(fresh_db, "nosuchws")


def _chain(db):
    """a -uses-> b -uses-> c -uses-> d, a lonely node, and a retracted edge b -> x."""
    with db.ws_tx("alpha") as conn:
        ids = {
            n: graph.add_node(conn, "alpha", "instance", t, n)
            for n, t in [
                ("a", "service"),
                ("b", "service"),
                ("c", "ticket"),
                ("d", "ticket"),
                ("lonely", "ticket"),
                ("x", "repo"),
            ]
        }
        for s, d in [("a", "b"), ("b", "c"), ("c", "d")]:
            graph.add_edge(conn, "alpha", ids[s], ids[d], "uses")
        graph.retract(
            conn, "alpha", "edge", graph.add_edge(conn, "alpha", ids["b"], ids["x"], "uses")
        )
    return ids


def test_graph_filters_by_type_name_and_unlinked(fresh_db):
    _chain(fresh_db)
    names = lambda g: {n["name"] for n in g["nodes"]}
    everything = browse.graph(fresh_db, "alpha")
    assert names(everything) == {"a", "b", "c", "d", "lonely", "x"}
    assert dict(everything["types"]) == {"service": 2, "ticket": 3, "repo": 1}
    linked = browse.graph(fresh_db, "alpha", unlinked=False)
    assert names(linked) == {"a", "b", "c", "d"} and linked["hidden_unlinked"] == 2
    assert names(browse.graph(fresh_db, "alpha", types=["ticket"])) == {"c", "d", "lonely"}
    # a name search shows the matches and their direct neighbours
    assert names(browse.graph(fresh_db, "alpha", q="B")) == {"a", "b", "c"}
    assert browse.graph(fresh_db, "alpha", q="nothing-like-this")["nodes"] == []


def test_neighbourhood_walks_current_edges_both_ways(fresh_db):
    ids = _chain(fresh_db)
    one = browse.neighbourhood(fresh_db, "alpha", ids["b"], 1)
    assert {n["name"]: n["depth"] for n in one["nodes"]} == {"b": 0, "a": 1, "c": 1}
    assert one["focus"]["name"] == "b"
    assert {(e["src"], e["dst"]) for e in one["edges"]} == {
        (ids["a"], ids["b"]),
        (ids["b"], ids["c"]),
    }
    two = browse.neighbourhood(fresh_db, "alpha", ids["b"], 2)
    assert {n["name"] for n in two["nodes"]} == {"a", "b", "c", "d"}
    assert len(browse.neighbourhood(fresh_db, "alpha", ids["b"], 99)["nodes"]) == 4  # clamped
    capped = browse.neighbourhood(fresh_db, "alpha", ids["b"], 2, limit=3)
    assert capped["nodes"][0]["name"] == "b" and len(capped["nodes"]) == 3 and capped["truncated"]
    assert browse.neighbourhood(fresh_db, "alpha", str(uuid.uuid4()), 1) is None
    assert browse.neighbourhood(fresh_db, "alpha", "not-a-uuid", 1) is None
