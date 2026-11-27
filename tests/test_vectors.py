"""An item that stops being current loses its vector (trigger), and upgrade cleans old ones."""

import uuid

import psycopg
import pytest
from fakes import FakeEmbedder
from psycopg import sql

from treant import auth, graph, migrate, names, tools, worker
from treant.search import search
from treant.sessions import save_session
from treant.tokens import TokenContext

_ALL = frozenset({"create", "read", "update", "delete"})


@pytest.fixture
def ws(db, admin_dsn, prefix):
    """A fresh, uniquely named workspace no other test writes to."""
    name = "sr" + uuid.uuid4().hex[:8]
    migrate.create_workspace(admin_dsn, prefix, name)
    return name


def _embed_ws(db, ws, emb):
    """Embed everything pending in ``ws``; a sweep is bounded (oldest first), so loop."""
    schema = sql.Identifier(names.ws_schema(ws))
    while True:
        with db.ws_tx(ws) as conn:
            items = worker._pending(conn, schema)
        if not items:
            return
        vecs = worker._embed_all(emb, items)
        with db.ws_tx(ws) as conn:
            worker._upsert(conn, schema, items, vecs)


def _has_vec(db, ws, kind, item_id) -> bool:
    schema = sql.Identifier(names.ws_schema(ws))
    with db.ws_tx(ws) as conn:
        row = conn.execute(
            sql.SQL("SELECT 1 FROM {}.embeddings WHERE item_kind = %s AND item_id = %s").format(
                schema
            ),
            (kind, item_id),
        ).fetchone()
    return row is not None


def _fact(db, ws, text, type="fact"):
    with db.ws_tx(ws) as conn:
        return graph.add_fact(conn, ws, type, text)


def test_retract_fact_drops_vector(db, ws):
    fid = _fact(db, ws, "a fact to retract")
    _embed_ws(db, ws, FakeEmbedder())
    assert _has_vec(db, ws, "fact", fid)
    with db.ws_tx(ws) as conn:
        graph.retract(conn, ws, "fact", fid)
    assert not _has_vec(db, ws, "fact", fid)


def test_update_fact_status_drops_vector(db, ws):
    fid = _fact(db, ws, "a fact to supersede")
    _embed_ws(db, ws, FakeEmbedder())
    assert _has_vec(db, ws, "fact", fid)
    with db.ws_tx(ws) as conn:
        graph.update_fact(conn, ws, fid, status="superseded")
    assert not _has_vec(db, ws, "fact", fid)


def test_update_fact_text_and_status_drops_vector_and_worker_skips_it(db, ws):
    emb = FakeEmbedder()
    fid = _fact(db, ws, "old words here")
    _embed_ws(db, ws, emb)
    assert _has_vec(db, ws, "fact", fid)
    with db.ws_tx(ws) as conn:
        graph.update_fact(conn, ws, fid, text="new words here", status="superseded")
    assert not _has_vec(db, ws, "fact", fid)
    _embed_ws(db, ws, emb)
    assert not _has_vec(db, ws, "fact", fid)


def test_close_thread_drops_vector(db, ws):
    fid = _fact(db, ws, "an open thread to close", type="open_thread")
    _embed_ws(db, ws, FakeEmbedder())
    assert _has_vec(db, ws, "fact", fid)
    t = tools.Tools(db, FakeEmbedder())
    token = auth.current_token.set(TokenContext("c2c2c2c2c2c2", ws, _ALL))
    try:
        assert t.update_fact(fid, status="closed") == {"version": 2}
    finally:
        auth.current_token.reset(token)
    assert not _has_vec(db, ws, "fact", fid)


def test_set_status_node_drops_vector_and_restore_reembeds(db, ws):
    emb = FakeEmbedder()
    with db.ws_tx(ws) as conn:
        nid = graph.add_node(conn, ws, "instance", "service", "svcvecnode")
    _embed_ws(db, ws, emb)
    assert _has_vec(db, ws, "node", nid)
    with db.ws_tx(ws) as conn:
        graph.set_status(conn, ws, "node", nid, "superseded", "test")
    assert not _has_vec(db, ws, "node", nid)
    with db.ws_tx(ws) as conn:
        graph.set_status(conn, ws, "node", nid, "current", "restore")
    assert not _has_vec(db, ws, "node", nid)
    _embed_ws(db, ws, emb)
    assert _has_vec(db, ws, "node", nid)


def test_merge_supersedes_node_vector(db, ws):
    with db.ws_tx(ws) as conn:
        keep = graph.add_node(conn, ws, "instance", "service", "svckeep")
        gone = graph.add_node(conn, ws, "instance", "service", "svcgone")
    _embed_ws(db, ws, FakeEmbedder())
    assert _has_vec(db, ws, "node", gone)
    with db.ws_tx(ws) as conn:
        graph.merge_nodes(conn, ws, keep, gone)
    assert not _has_vec(db, ws, "node", gone)
    assert _has_vec(db, ws, "node", keep)


def test_text_edit_still_drops_and_reembeds(db, ws):
    emb = FakeEmbedder()
    fid = _fact(db, ws, "first wording")
    _embed_ws(db, ws, emb)
    assert _has_vec(db, ws, "fact", fid)
    with db.ws_tx(ws) as conn:
        graph.update_fact(conn, ws, fid, text="second wording")
    assert not _has_vec(db, ws, "fact", fid)
    _embed_ws(db, ws, emb)
    assert _has_vec(db, ws, "fact", fid)


def _pending_one(db, ws, kind, item_id):
    """The worker's pending item for one id, embedded, as the worker holds it before writing."""
    schema = sql.Identifier(names.ws_schema(ws))
    with db.ws_tx(ws) as conn:
        items = [
            i for i in worker._pending(conn, schema, journals=True) if i[:2] == (kind, item_id)
        ]
    assert len(items) == 1
    return schema, items, worker._embed_all(FakeEmbedder(), items)


def test_node_superseded_mid_embed_gets_no_vector(db, ws):
    with db.ws_tx(ws) as conn:
        nid = str(graph.add_node(conn, ws, "instance", "service", "svcmidembed"))
    schema, items, vecs = _pending_one(db, ws, "node", nid)
    with db.ws_tx(ws) as conn:  # lands between the worker's read and its write
        graph.set_status(conn, ws, "node", nid, "superseded", "test")
    with db.ws_tx(ws) as conn:
        worker._upsert(conn, schema, items, vecs)
    assert not _has_vec(db, ws, "node", nid)


def test_journal_quarantined_mid_embed_gets_no_vector(db, ws):
    ctx = TokenContext("c4c4c4c4c4c4", ws, _ALL)
    res = save_session(db, ctx, "vec-mid", {"journal": "alpha beta deployed"})
    assert not res.quarantined
    schema = sql.Identifier(names.ws_schema(ws))
    with db.ws_tx(ws) as conn:
        rev, jid = conn.execute(
            sql.SQL(
                "SELECT r.id, j.id::text FROM {s}.revisions r "
                "JOIN {s}.journal_entries j ON j.revision_id = r.id "
                "WHERE r.session_id = %s AND r.n = %s"
            ).format(s=schema),
            (res.session_id, res.revision),
        ).fetchone()
    schema, items, vecs = _pending_one(db, ws, "journal", jid)
    with db.ws_tx(ws) as conn:  # as distill's re-scrub quarantines a revision
        conn.execute(
            sql.SQL("UPDATE {s}.revisions SET status = 'quarantined' WHERE id = %s").format(
                s=schema
            ),
            (rev,),
        )
    with db.ws_tx(ws) as conn:
        worker._upsert(conn, schema, items, vecs)
    assert not _has_vec(db, ws, "journal", jid)


def test_restored_fact_is_reembedded_and_found(db, ws):
    emb, m = FakeEmbedder(), "zq" + uuid.uuid4().hex[:12]
    fid = _fact(db, ws, f"{m} restored")
    _embed_ws(db, ws, emb)
    assert _has_vec(db, ws, "fact", fid)
    with db.ws_tx(ws) as conn:
        graph.retract(conn, ws, "fact", fid)
    assert not _has_vec(db, ws, "fact", fid)
    with db.ws_tx(ws) as conn:
        graph.restore_fact(conn, ws, fid, 1)  # version 1: the current, unretracted text
    assert not _has_vec(db, ws, "fact", fid)
    _embed_ws(db, ws, emb)
    assert _has_vec(db, ws, "fact", fid)
    res = search(db, emb, TokenContext("c5c5c5c5c5c5", ws, frozenset({"read"})), m)
    assert res.mode == "hybrid"
    assert fid in [h["id"] for h in res.hits]


def _has_general_vec(db, kind, item_id) -> bool:
    with db.publisher_tx() as conn:
        row = conn.execute(
            "SELECT 1 FROM general.embeddings WHERE item_kind = %s AND item_id = %s",
            (kind, item_id),
        ).fetchone()
    return row is not None


def test_general_retract_drops_vector(db):
    with db.publisher_tx() as conn:
        gid = graph.add_fact(conn, "general", "fact", f"general vec {uuid.uuid4().hex[:8]}")
    while worker.embed_general(db, FakeEmbedder()):
        pass
    assert _has_general_vec(db, "fact", gid)
    with db.publisher_tx() as conn:
        graph.retract(conn, "general", "fact", gid)
    assert not _has_general_vec(db, "fact", gid)


def test_general_node_superseded_mid_embed_gets_no_vector(db):
    with db.publisher_tx() as conn:
        nid = str(
            graph.add_node(conn, "general", "concept", "service", f"gmid{uuid.uuid4().hex[:8]}")
        )
    schema = sql.Identifier("general")
    while True:  # the general sweep is bounded, oldest first: reach this node
        with db.publisher_tx() as conn:
            pending = worker._pending(conn, schema)
        items = [i for i in pending if i[:2] == ("node", nid)]
        if items or not pending:
            break
        worker.embed_general(db, FakeEmbedder())
    assert len(items) == 1
    vecs = worker._embed_all(FakeEmbedder(), items)
    with db.publisher_tx() as conn:
        graph.set_status(conn, "general", "node", nid, "superseded", "test")
    with db.publisher_tx() as conn:
        worker._upsert(conn, schema, items, vecs)
    assert not _has_general_vec(db, "node", nid)


_VEC = "[" + ",".join(["0.1"] * 768) + "]"


def _vec_indexes(conn, schema: str) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = %s AND tablename = 'embeddings' "
            "AND position('hnsw' in indexdef) > 0",
            (schema,),
        )
    }


def _triggers(conn, schema: str) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND NOT t.tgisinternal AND t.tgenabled = 'O'",
            (schema,),
        )
    }


def _public_can_run_trigger_fn(conn, schema: str) -> bool:
    return conn.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, "
        "aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a "
        "WHERE n.nspname = %s AND p.proname = 'drop_vector_when_not_current' "
        "AND a.grantee = 0 AND a.privilege_type = 'EXECUTE')",
        (schema,),
    ).fetchone()[0]


def test_upgrade_cleans_stale_vectors_and_indexes(fresh):
    admin, _, prefix = fresh
    name, archived = "sr" + uuid.uuid4().hex[:8], "sr" + uuid.uuid4().hex[:8]
    migrate.create_workspace(admin, prefix, name)
    migrate.create_workspace(admin, prefix, archived)
    s = names.ws_schema(name)
    schema = sql.Identifier(s)
    with psycopg.connect(admin) as conn, conn.transaction():
        conn.execute(
            "UPDATE control.workspaces SET status = 'archived' WHERE name = %s", (archived,)
        )
        stale = graph.add_fact(conn, name, "fact", "stale fact")
        live = graph.add_fact(conn, name, "fact", "live fact")
        orphan_journal = str(uuid.uuid4())
        stale_node = graph.add_node(conn, name, "instance", "service", "svcstalenode")
        quarantined_journal = conn.execute(
            sql.SQL(
                "WITH s AS (INSERT INTO {s}.sessions (token_id, client_session_id) "
                "VALUES ('c6c6c6c6c6c6', 'vec-upgrade') RETURNING id), "
                "r AS (INSERT INTO {s}.revisions (session_id, n, sections, status) "
                "SELECT id, 1, '{{}}', 'quarantined' FROM s RETURNING id) "
                "INSERT INTO {s}.journal_entries (revision_id, text) "
                "SELECT id, 'alpha beta' FROM r RETURNING id::text"
            ).format(s=schema)
        ).fetchone()[0]
        for kind, item_id in (
            ("fact", stale),
            ("fact", live),
            ("journal", orphan_journal),
            ("node", stale_node),
            ("journal", quarantined_journal),
        ):
            conn.execute(
                sql.SQL(
                    "INSERT INTO {}.embeddings (item_kind, item_id, vec) VALUES (%s, %s, %s::vector)"
                ).format(schema),
                (kind, item_id, _VEC),
            )
        # An older release left the vector of a fact that stopped being current.
        conn.execute(
            sql.SQL("ALTER TABLE {}.facts DISABLE TRIGGER facts_drop_vector").format(schema)
        )
        conn.execute(
            sql.SQL("UPDATE {}.facts SET status = 'superseded' WHERE id = %s").format(schema),
            (stale,),
        )
        conn.execute(
            sql.SQL("ALTER TABLE {}.facts ENABLE TRIGGER facts_drop_vector").format(schema)
        )
        # ... and of a node that stopped being current.
        conn.execute(
            sql.SQL("ALTER TABLE {}.nodes DISABLE TRIGGER nodes_drop_vector").format(schema)
        )
        conn.execute(
            sql.SQL("UPDATE {}.nodes SET status = 'superseded' WHERE id = %s").format(schema),
            (stale_node,),
        )
        conn.execute(
            sql.SQL("ALTER TABLE {}.nodes ENABLE TRIGGER nodes_drop_vector").format(schema)
        )
        gstale = graph.add_fact(conn, "general", "fact", "stale general fact")
        conn.execute(
            "INSERT INTO general.embeddings (item_kind, item_id, vec) "
            "VALUES ('fact', %s, %s::vector)",
            (gstale, _VEC),
        )
        conn.execute("ALTER TABLE general.facts DISABLE TRIGGER facts_drop_vector")
        conn.execute("UPDATE general.facts SET status = 'retracted' WHERE id = %s", (gstale,))
        conn.execute("ALTER TABLE general.facts ENABLE TRIGGER facts_drop_vector")
        # ... and the single vector index of older releases.
        for target in (schema, sql.Identifier("general")):
            conn.execute(
                sql.SQL(
                    "CREATE INDEX embeddings_vec_idx ON {}.embeddings USING hnsw (vec vector_cosine_ops)"
                ).format(target)
            )

    assert set(migrate.upgrade(admin, prefix)) == {name, archived}
    assert set(migrate.upgrade(admin, prefix)) == {name, archived}

    with psycopg.connect(admin) as conn:
        kept = {
            (r[0], str(r[1]))
            for r in conn.execute(
                sql.SQL("SELECT item_kind, item_id FROM {}.embeddings").format(schema)
            )
        }
        assert kept == {("fact", live)}
        assert (
            conn.execute(
                "SELECT 1 FROM general.embeddings WHERE item_id = %s", (gstale,)
            ).fetchone()
            is None
        )
        ws_indexes = {
            "embeddings_fact_vec_idx",
            "embeddings_node_vec_idx",
            "embeddings_journal_vec_idx",
        }
        for ws_schema in (s, names.ws_schema(archived)):
            assert _vec_indexes(conn, ws_schema) == ws_indexes
            assert {"facts_drop_vector", "nodes_drop_vector"} <= _triggers(conn, ws_schema)
            assert not _public_can_run_trigger_fn(conn, ws_schema)
        assert _vec_indexes(conn, "general") == {
            "embeddings_fact_vec_idx",
            "embeddings_node_vec_idx",
        }
        assert {"facts_drop_vector", "nodes_drop_vector"} <= _triggers(conn, "general")
        assert not _public_can_run_trigger_fn(conn, "general")
