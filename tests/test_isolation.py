import uuid

import psycopg
import pytest
from fakes import FakeEmbedder
from psycopg import sql

from treant import canary, graph, names, worker
from treant.tokens import TokenContext


def test_isolation_clean_with_three_workspaces(fresh_db):
    emb = FakeEmbedder()
    for ws in ("alpha", "beta", "gamma"):
        canary.plant(fresh_db, emb, ws)
    assert canary.check(fresh_db, emb) == []


def test_isolation_detects_planted_leak(fresh_db):
    emb = FakeEmbedder()
    marker_b = canary.plant(fresh_db, emb, "beta")
    canary.plant(fresh_db, emb, "alpha")
    canary.plant(fresh_db, emb, "gamma")
    with fresh_db.publisher_tx() as conn:
        graph.add_fact(conn, "general", "fact", f"leaked {marker_b}")
    assert canary.check(fresh_db, emb) == [("general", "general", "beta")]
    # Once embedded, the leaked general fact is also what alpha's and gamma's searches find.
    worker.embed_general(fresh_db, emb)
    faults = canary.check(fresh_db, emb)
    assert {("general", "general", "beta"), ("search", "alpha", "beta")} <= set(faults)
    assert all(f[2] == "beta" for f in faults)


def test_planted_canaries_are_recorded_and_embedded(fresh_db):
    emb = FakeEmbedder()
    marker = canary.plant(fresh_db, emb, "alpha")
    with fresh_db.control_tx() as conn:
        n = conn.execute(
            "SELECT count(*) FROM control.canaries WHERE workspace = 'alpha' AND marker = %s",
            (marker,),
        ).fetchone()[0]
    assert n == 3
    with fresh_db.ws_tx("alpha") as conn:
        assert conn.execute("SELECT count(*) FROM ws_alpha.embeddings").fetchone()[0] == 3


def test_check_ignores_archived_workspaces(fresh_db):
    emb = FakeEmbedder()
    canary.plant(fresh_db, emb, "alpha")
    canary.plant(fresh_db, emb, "beta")
    marker_g = canary.plant(fresh_db, emb, "gamma")
    with fresh_db.control_tx() as conn:
        conn.execute("UPDATE control.workspaces SET status = 'archived' WHERE name = 'gamma'")
    assert canary.check(fresh_db, emb) == []  # gamma's pairs are not checked any more
    # but an archived workspace's marker in general is still a leak
    with fresh_db.publisher_tx() as conn:
        graph.add_fact(conn, "general", "fact", f"leaked {marker_g}")
    assert canary.check(fresh_db, emb) == [("general", "general", "gamma")]


def test_setup_error_is_a_fault_not_a_crash(fresh_db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("detail")

    monkeypatch.setattr(canary, "_active", boom)
    assert canary.check(fresh_db, FakeEmbedder()) == [("error:setup", "-", "-")]
    monkeypatch.undo()
    monkeypatch.setattr(canary, "_canaries", boom)
    assert canary.check(fresh_db, FakeEmbedder()) == [("error:setup", "-", "-")]


def test_real_leak_through_general_reaches_search_and_brief(fresh_db):
    """No monkeypatching: beta's canary name becomes a general concept with a fact on it."""
    emb = FakeEmbedder()
    markers = _plant_all(emb=emb, db=fresh_db)
    with fresh_db.publisher_tx() as conn:
        node = graph.add_node(conn, "general", "concept", "tool", markers["beta"])
        graph.add_fact(conn, "general", "fact", f"about {markers['beta']}", node_id=node)
    worker.embed_general(fresh_db, emb)
    faults = set(canary.check(fresh_db, emb))
    for label in ("search", "brief"):
        assert (label, "alpha", "beta") in faults and (label, "gamma", "beta") in faults
    assert all(f[2] == "beta" for f in faults)


# --- the check must be able to fail -------------------------------------------------------


def _plant_all(db, emb, names=("alpha", "beta", "gamma")):
    return {ws: canary.plant(db, emb, ws) for ws in names}


def _self_args(db, emb, ws):
    markers, ids = canary._canaries(db, [ws])[ws]
    ctx = TokenContext(token_id="canary", workspace=ws, actions=frozenset({"read"}))
    return markers[0], ids, ctx


def test_each_check_helper_fires_on_own_canary(fresh_db):
    """Positive control: asked about its own canary, every helper must report exposure."""
    emb = FakeEmbedder()
    _plant_all(emb=emb, db=fresh_db)
    m, ids, ctx = _self_args(fresh_db, emb, "beta")
    assert canary._check_search(fresh_db, emb, ctx, m, ids)
    assert canary._check_brief(fresh_db, ctx, m, m)
    assert canary._check_guard(fresh_db, ctx, m, m)
    assert canary._check_walk(fresh_db, ctx, ids)
    assert canary._check_direct(fresh_db, "beta", "beta")


@pytest.mark.parametrize("label", ["search", "brief", "guard", "walk"])
def test_check_reports_each_path_that_exposes_the_other_workspace(fresh_db, monkeypatch, label):
    emb = FakeEmbedder()
    _plant_all(emb=emb, db=fresh_db)
    monkeypatch.setattr(canary, f"_check_{label}", lambda *a, **k: True)
    faults = canary.check(fresh_db, emb)
    assert (label, "alpha", "beta") in faults
    assert {f[0] for f in faults} == {label}


def test_direct_select_detects_a_real_grant(fresh, fresh_db):
    admin, _, prefix = fresh
    emb = FakeEmbedder()
    _plant_all(emb=emb, db=fresh_db)
    role = sql.Identifier(names.ws_role(prefix, "alpha"))
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(sql.SQL("GRANT USAGE ON SCHEMA ws_beta TO {}").format(role))
        c.execute(sql.SQL("GRANT SELECT ON ws_beta.facts TO {}").format(role))
    assert canary.check(fresh_db, emb) == [("direct_select", "alpha", "beta")]


def test_direct_select_errors_when_own_schema_unreadable(fresh, fresh_db):
    admin, _, prefix = fresh
    emb = FakeEmbedder()
    _plant_all(emb=emb, db=fresh_db)
    role = sql.Identifier(names.ws_role(prefix, "alpha"))
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(sql.SQL("REVOKE SELECT ON ws_alpha.facts FROM {}").format(role))
    faults = canary.check(fresh_db, emb)
    assert ("error:direct_select", "alpha", "beta") in faults


def test_raising_check_is_a_fault_not_a_crash(fresh_db, monkeypatch):
    emb = FakeEmbedder()
    _plant_all(emb=emb, db=fresh_db)

    def boom(*a, **k):
        raise RuntimeError("detail")

    monkeypatch.setattr(canary, "_check_brief", boom)
    faults = canary.check(fresh_db, emb)
    assert ("error:brief", "alpha", "beta") in faults
    assert all(f[0] == "error:brief" for f in faults)


def test_zero_canaries_is_reported(fresh_db):
    faults = canary.check(fresh_db, FakeEmbedder())
    assert {("no_canary", w, w) for w in ("alpha", "beta", "gamma")} <= set(faults)


def test_one_canary_only_reports_the_others(fresh_db):
    emb = FakeEmbedder()
    canary.plant(fresh_db, emb, "alpha")
    faults = canary.check(fresh_db, emb)
    assert ("no_canary", "beta", "beta") in faults and ("no_canary", "gamma", "gamma") in faults
    assert ("no_canary", "alpha", "alpha") not in faults


def test_fewer_than_two_active_workspaces_is_a_coverage_fault(fresh_db):
    emb = FakeEmbedder()
    canary.plant(fresh_db, emb, "alpha")
    with fresh_db.control_tx() as conn:
        conn.execute("UPDATE control.workspaces SET status = 'archived' WHERE name <> 'alpha'")
    assert ("coverage", "-", "-") in canary.check(fresh_db, emb)


def test_unembedded_canary_is_reported(fresh_db):
    emb = FakeEmbedder(down=True)
    for ws in ("alpha", "beta", "gamma"):
        canary.plant(fresh_db, emb, ws)
    faults = canary.check(fresh_db, FakeEmbedder())
    assert {("not_embedded", w, w) for w in ("alpha", "beta", "gamma")} <= set(faults)


def _plant_two(db, emb):
    return {ws: canary.plant(db, emb, ws) for ws in ("alpha", "beta", "gamma")}


def _queue_insert(db, item, key, status):
    import uuid

    from psycopg.types.json import Jsonb

    with db.publisher_tx() as conn:
        conn.execute(
            "INSERT INTO control.general_queue "
            "(workspace, session_id, revision_id, item, item_key, status) "
            "VALUES ('alpha', %s, %s, %s, %s, %s)",
            (str(uuid.uuid4()), str(uuid.uuid4()), Jsonb(item), key, status),
        )


def test_isolation_check_flags_marker_in_published_queue_row(fresh_db):
    emb = FakeEmbedder()
    markers = _plant_two(fresh_db, emb)
    assert canary.check(fresh_db, emb) == []
    item = {"kind": "fact", "type": "lesson", "text": f"restart {markers['beta']} now"}
    concept = {"kind": "concept", "type": "tool", "name": markers["gamma"], "aliases": []}
    _queue_insert(fresh_db, item, "k1", "published")
    _queue_insert(fresh_db, concept, "k2", "published")
    assert sorted(canary.check(fresh_db, emb)) == [
        ("general", "general", "beta"),
        ("general", "general", "gamma"),
    ]


def test_marker_in_unpublished_queue_rows_is_info_only(fresh_db, capsys):
    from treant import cli

    emb = FakeEmbedder()
    markers = _plant_two(fresh_db, emb)
    for i, status in enumerate(("pending", "waiting_review", "blocked", "dropped", "stale")):
        item = {"kind": "fact", "type": "lesson", "text": f"x {markers['beta']} {i}"}
        _queue_insert(fresh_db, item, f"k{i}", status)
    assert canary.check(fresh_db, emb) == []
    assert canary.queue_held(fresh_db) == 5
    assert cli.main(["isolation-test"]) == 0
    assert "canary markers held in queue: 5" in capsys.readouterr().out


def test_adversarial_summary_cannot_publish_other_workspace_name(fresh_db):
    import json

    from fakes import FakeBackend

    from treant import apply as apply_mod
    from treant import cli, distill, gate
    from treant.sessions import save_session
    from treant.tokens import TokenContext

    emb = FakeEmbedder()
    markers = _plant_two(fresh_db, emb)
    leaked = markers["beta"]
    ctx = TokenContext("a1a1a1a1a1a1", "alpha", frozenset({"create", "read"}))
    save_session(fresh_db, ctx, "adv-1", {"journal": "debugged the rollout"})

    def reply(messages, alias):
        body = json.loads(messages[-1]["content"])
        if "sections" in body:  # distill
            return json.dumps(
                {
                    "nodes": [],
                    "edges": [],
                    "facts": [],
                    "general": {
                        "concepts": [
                            {"name": "signoz", "type": "tool", "aliases": []},
                            {"name": "sneaky", "type": "tool", "aliases": [leaked]},
                        ],
                        "facts": [
                            {
                                "type": "lesson",
                                "text": f"always drain {leaked} first",
                                "concept": "signoz",
                            }
                        ],
                    },
                }
            )
        return json.dumps({"text": "always drain the cluster first"})  # gate rewrite

    backend = FakeBackend(reply)
    factory = lambda env, ws, s: backend
    judges = gate.Judges(jev=lambda t, k: 1.0, llm=lambda t, k: True)
    handlers = {
        "distill": lambda job: distill.handle(fresh_db, {}, job, factory),
        "apply": lambda job: apply_mod.handle(fresh_db, job),
        "gate": lambda job: gate.handle(fresh_db, {}, job, judges, factory),
    }
    cli.worker_loop(fresh_db, emb, {}, max_iterations=12, sleep=lambda s: None, handlers=handlers)
    with fresh_db.control_tx() as conn:
        queue = conn.execute("SELECT status, item->>'text' FROM control.general_queue").fetchall()
    assert queue, "the distilled general fact was never queued"
    with fresh_db.publisher_tx() as conn:
        texts = [r[0] for r in conn.execute("SELECT text FROM general.facts").fetchall()]
        texts += [r[0] for r in conn.execute("SELECT name FROM general.nodes").fetchall()]
    assert not any(leaked in t for t in texts)
    for status, text in queue:
        assert status == "blocked" or leaked not in (text or "")
    assert canary.check(fresh_db, emb) == []
    assert canary.queue_held(fresh_db) == 1
    assert sorted(queue, key=str) == [
        ("blocked", None),  # the concept carrying the name as an alias
        ("published", "always drain the cluster first"),  # the fact, rewritten
        ("published", None),  # the innocent concept
    ]


def test_search_source_never_names_other_workspace_session(db, ctx_a):
    from datetime import UTC, datetime

    from test_provenance import _revision

    from treant.search import search

    marker, emb, day = (
        "zq" + uuid.uuid4().hex[:12],
        FakeEmbedder(),
        datetime(2026, 11, 20, tzinfo=UTC),
    )
    ids = {}
    for ws, session in (("alpha", "alpha-only"), ("beta", "beta-only")):
        with db.ws_tx(ws) as c:
            ids[ws] = graph.add_fact(c, ws, "fact", f"{ws} note {marker}")
        revision = _revision(db, ws, session, day)
        with db.admin_tx() as c:
            c.execute(
                f"UPDATE {names.ws_schema(ws)}.facts SET revision_id = %s WHERE id = %s",
                (revision, ids[ws]),
            )
    with db.publisher_tx() as c:
        general = graph.add_fact(c, "general", "fact", f"general note {marker}")
    for ws in ("alpha", "beta"):
        schema = worker.sql.Identifier(names.ws_schema(ws))
        while True:
            with db.ws_tx(ws) as c:
                items = worker._pending(c, schema)
            if not items:
                break
            vecs = worker._embed_all(emb, items)
            with db.ws_tx(ws) as c:
                worker._upsert(c, schema, items, vecs)
    worker.embed_general(db, emb)
    hits = {h["id"]: h for h in search(db, emb, ctx_a, marker, limit=20).hits}
    assert hits[ids["alpha"]]["source"]["session"] == "alpha-only"
    assert ids["beta"] not in hits
    assert all(h.get("source", {}).get("session") != "beta-only" for h in hits.values())
    assert set(hits[general]["source"]) == {"saved"}
