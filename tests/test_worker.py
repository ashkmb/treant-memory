import uuid

import psycopg
from fakes import FakeEmbedder
from psycopg import sql

from treant import graph, jobs, worker
from treant.sessions import save_session


def _drain(db, embedder):
    while worker.run_once(db, embedder):
        pass


def _has_embedding(db, kind, item_id):
    with db.ws_tx("alpha") as conn:
        return conn.execute(
            "SELECT count(*) FROM ws_alpha.embeddings WHERE item_kind = %s AND item_id = %s",
            (kind, item_id),
        ).fetchone()[0]


def _entry_id(db, res):
    with db.ws_tx("alpha") as conn:
        return conn.execute(
            "SELECT j.id FROM ws_alpha.journal_entries j JOIN ws_alpha.revisions r "
            "ON r.id = j.revision_id WHERE r.session_id = %s AND r.n = %s",
            (res.session_id, res.revision),
        ).fetchone()[0]


def test_run_once_embeds_journal(db, ctx_a):
    res = save_session(db, ctx_a, "emb-1", {"journal": "deployed the thing"})
    _drain(db, FakeEmbedder())
    assert _has_embedding(db, "journal", _entry_id(db, res)) == 1


def test_run_once_embeds_current_facts_and_nodes(db, ctx_a):
    with db.ws_tx("alpha") as conn:
        fid = conn.execute(
            "INSERT INTO ws_alpha.facts (type, text) VALUES ('fact', 'redis has no password') "
            "RETURNING id"
        ).fetchone()[0]
        old = conn.execute(
            "INSERT INTO ws_alpha.facts (type, text, status) "
            "VALUES ('fact', 'stale', 'superseded') RETURNING id"
        ).fetchone()[0]
        nid = conn.execute(
            "INSERT INTO ws_alpha.nodes (kind, type, name, aliases) "
            "VALUES ('instance', 'service', 'nextcloud', ARRAY['nc']) RETURNING id"
        ).fetchone()[0]
    save_session(db, ctx_a, "emb-2", {"journal": "x"})
    _drain(db, FakeEmbedder())
    assert _has_embedding(db, "fact", fid) == 1
    assert _has_embedding(db, "node", nid) == 1
    assert _has_embedding(db, "fact", old) == 0


def test_embedder_down_fails_job_with_category(fresh_db, ctx_a):
    db = fresh_db  # own queue: failing jobs here must not touch other tests' jobs
    res = save_session(db, ctx_a, "emb-down", {"journal": "x"})
    while worker.run_once(db, FakeEmbedder(down=True)):
        pass
    with db.ws_tx("alpha") as conn:
        rev = conn.execute(
            "SELECT id::text FROM ws_alpha.revisions WHERE session_id = %s AND n = %s",
            (res.session_id, res.revision),
        ).fetchone()[0]
    with db.control_tx() as conn:
        row = conn.execute(
            "SELECT attempts, error_category FROM control.jobs "
            "WHERE workspace = 'alpha' AND kind = 'embed' AND key = %s",
            (rev,),
        ).fetchone()
    assert row == (1, "embedder")


def test_run_once_returns_false_when_idle(db):
    _drain(db, FakeEmbedder())
    assert worker.run_once(db, FakeEmbedder()) is False


def test_embed_general(db):
    with db.publisher_tx() as conn:
        fid = conn.execute(
            "INSERT INTO general.facts (type, text) VALUES ('fact', %s) RETURNING id",
            (f"general fact {uuid.uuid4()}",),
        ).fetchone()[0]
    assert worker.embed_general(db, FakeEmbedder()) >= 1
    with db.publisher_tx() as conn:
        n = conn.execute(
            "SELECT count(*) FROM general.embeddings WHERE item_kind = 'fact' AND item_id = %s",
            (fid,),
        ).fetchone()[0]
    assert n == 1
    assert worker.embed_general(db, FakeEmbedder()) == 0


class _RacingEmbedder(FakeEmbedder):
    """Runs ``hook`` once, inside the first embed call: between the worker's read and write."""

    def __init__(self, hook):
        super().__init__()
        self.hook = hook

    def embed_documents(self, texts):
        hook, self.hook = self.hook, None
        if hook is not None:
            hook()
        return super().embed_documents(texts)


def _distance(db, kind, item_id, text):
    vec = "[" + ",".join(repr(x) for x in FakeEmbedder()._vec(text)) + "]"
    with db.ws_tx("alpha") as conn:
        return conn.execute(
            "SELECT vec <=> %s::vector FROM ws_alpha.embeddings "
            "WHERE item_kind = %s AND item_id = %s",
            (vec, kind, item_id),
        ).fetchone()[0]


def test_edit_during_embed_does_not_leave_stale_vector(db):
    _drain(db, FakeEmbedder())
    t1, t2 = f"first wording {uuid.uuid4().hex}", f"second phrasing {uuid.uuid4().hex}"
    with db.ws_tx("alpha") as conn:
        fid = graph.add_fact(conn, "alpha", "fact", t1)
        jobs.enqueue(conn, "alpha", "embed", f"race-seed:{fid}", {"revision_id": None})

    def edit():
        # What tools.update_fact does: new text, then a job for the new version.
        with db.ws_tx("alpha") as conn:
            version = graph.update_fact(conn, "alpha", fid, text=t2)
            jobs.enqueue(conn, "alpha", "embed", f"fact:{fid}:{version}", {"revision_id": None})

    emb = _RacingEmbedder(edit)
    assert worker.run_once(db, emb) is True
    _drain(db, emb)
    assert _distance(db, "fact", fid, t2) < 1e-6


def test_journal_of_dead_job_is_embedded_by_a_later_job(db, ctx_a):
    old = save_session(db, ctx_a, "emb-dead-1", {"journal": "journal whose job died"})
    with db.ws_tx("alpha") as conn:
        rev = conn.execute(
            "SELECT id::text FROM ws_alpha.revisions WHERE session_id = %s AND n = %s",
            (old.session_id, old.revision),
        ).fetchone()[0]
    with db.control_tx() as conn:  # the embedder was down past the last retry
        conn.execute(
            "UPDATE control.jobs SET status = 'dead', attempts = 8, error_category = 'embedder' "
            "WHERE workspace = 'alpha' AND kind = 'embed' AND key = %s",
            (rev,),
        )
    save_session(db, ctx_a, "emb-dead-2", {"journal": "a later session"})
    _drain(db, FakeEmbedder())
    assert _has_embedding(db, "journal", _entry_id(db, old)) == 1


def test_upsert_waits_for_concurrent_edit(db):
    """A fact edit cannot slip between the worker's gated select and its commit."""
    _drain(db, FakeEmbedder())
    t1, t2 = f"first wording {uuid.uuid4().hex}", f"second phrasing {uuid.uuid4().hex}"
    with db.ws_tx("alpha") as conn:
        fid = graph.add_fact(conn, "alpha", "fact", t1)
    schema = sql.Identifier("ws_alpha")
    with db.ws_tx("alpha") as conn:
        items = [i for i in worker._pending(conn, schema) if i[1] == fid]
    assert len(items) == 1
    vectors = worker._embed_all(FakeEmbedder(), items)

    blocked = None
    with db.ws_tx("alpha") as writer:
        worker._upsert(writer, schema, items, vectors)  # worker's write transaction is open
        try:
            with db.ws_tx("alpha") as editor:
                editor.execute("SET LOCAL lock_timeout = '500ms'")
                graph.update_fact(editor, "alpha", fid, text=t2)
        except psycopg.errors.LockNotAvailable as e:
            blocked = e
    assert blocked is not None  # the edit waited for the worker instead of racing it

    with db.ws_tx("alpha") as conn:
        version = graph.update_fact(conn, "alpha", fid, text=t2)
        jobs.enqueue(conn, "alpha", "embed", f"fact:{fid}:{version}", {"revision_id": None})
    _drain(db, FakeEmbedder())
    assert _distance(db, "fact", fid, t2) < 1e-6


def test_sweep_is_bounded_and_requeues(fresh_db, monkeypatch):
    db = fresh_db
    monkeypatch.setattr(worker, "SWEEP_LIMIT", 3)
    with db.ws_tx("alpha") as conn:
        for i in range(5):
            conn.execute(
                "INSERT INTO ws_alpha.facts (type, text) VALUES ('fact', %s)", (f"sweep fact {i}",)
            )
        jobs.enqueue(conn, "alpha", "embed", "seed", {"revision_id": None})

    def embedded():
        with db.ws_tx("alpha") as conn:
            return conn.execute(
                "SELECT count(*) FROM ws_alpha.embeddings WHERE item_kind = 'fact'"
            ).fetchone()[0]

    assert worker.run_once(db, FakeEmbedder()) is True
    assert embedded() == 3
    with db.control_tx() as conn:
        follow = conn.execute(
            "SELECT key, payload FROM control.jobs WHERE kind = 'embed' AND status = 'queued'"
        ).fetchall()
    assert len(follow) == 1
    assert follow[0][0].startswith("sweep:alpha:")
    assert follow[0][1] == {"revision_id": None}
    assert worker.run_once(db, FakeEmbedder()) is True
    assert embedded() == 5
    assert worker.run_once(db, FakeEmbedder()) is False


def _stage_job(db, ws, kind, key):
    with db.control_tx() as conn:
        jobs.enqueue(conn, ws, kind, key, {"revision_id": None})


def _job_state(db, kind, key):
    with db.control_tx() as conn:
        return conn.execute(
            "SELECT status, error_category, attempts FROM control.jobs "
            "WHERE kind = %s AND key = %s",
            (kind, key),
        ).fetchone()


def test_worker_loop_runs_all_stages(fresh_db):
    from treant import cli

    order = []

    def fake(kind):
        def handle(job):
            order.append(kind)
            jobs.complete(fresh_db, job)

        return handle

    for kind in ("gate", "apply", "distill", "embed"):
        _stage_job(fresh_db, "alpha", kind, f"{kind}-1")
    sleeps = []
    cli.worker_loop(
        fresh_db,
        FakeEmbedder(),
        {},
        max_iterations=2,
        sleep=sleeps.append,
        handlers={k: fake(k) for k in ("distill", "apply", "gate")},
    )
    assert order == ["distill", "apply", "gate"]  # fixed stage order, one job each
    assert [
        _job_state(fresh_db, k, f"{k}-1")[0] for k in ("embed", "distill", "apply", "gate")
    ] == ["done"] * 4
    assert sleeps == [5]  # busy on the first iteration only


def test_worker_loop_survives_stage_error(fresh_db, caplog):
    from treant import cli

    ran = []

    def boom(job):
        raise RuntimeError("secret detail")

    def ok(job):
        ran.append(job.kind)
        jobs.complete(fresh_db, job)

    _stage_job(fresh_db, "alpha", "distill", "d1")
    _stage_job(fresh_db, "alpha", "gate", "g1")
    cli.worker_loop(
        fresh_db,
        FakeEmbedder(),
        {},
        max_iterations=1,
        sleep=lambda s: None,
        handlers={"distill": boom, "apply": ok, "gate": ok},
    )
    assert ran == ["gate"]  # the stage after the failing one still ran
    status, category, attempts = _job_state(fresh_db, "distill", "d1")
    assert (status, category, attempts) == ("queued", "internal", 1)
    assert "RuntimeError" in caplog.text and "secret detail" not in caplog.text


def _fact_job(db, key):
    with db.ws_tx("alpha") as conn:
        jobs.enqueue(conn, "alpha", "embed", key, {"revision_id": None})


def _embed_job(db, key):
    with db.control_tx() as conn:
        return conn.execute(
            "SELECT status, error_category, attempts FROM control.jobs "
            "WHERE kind = 'embed' AND key = %s",
            (key,),
        ).fetchone()


def test_internal_error_fails_job_by_category_only(fresh_db, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("secret detail")

    _fact_job(fresh_db, "internal")
    monkeypatch.setattr(worker, "_process", boom)
    assert worker.run_once(fresh_db, FakeEmbedder()) is True
    assert _embed_job(fresh_db, "internal") == ("queued", "internal", 1)
    assert "RuntimeError" in caplog.text and "secret detail" not in caplog.text


def test_lost_lease_is_not_logged_as_done(fresh_db, monkeypatch, caplog):
    import logging

    _fact_job(fresh_db, "lost")
    monkeypatch.setattr(jobs, "complete", lambda db, job, status="done": False)
    with caplog.at_level(logging.INFO, logger="treant.worker"):
        assert worker.run_once(fresh_db, FakeEmbedder()) is True
    assert "embed job done" not in caplog.text
    assert "lease lost" in caplog.text


class _FailsAfter(FakeEmbedder):
    def __init__(self, ok_calls):
        super().__init__()
        self.ok_calls = ok_calls

    def embed_documents(self, texts):
        if self.ok_calls == 0:
            from treant.embed import EmbedderUnavailable

            raise EmbedderUnavailable("down mid-job")
        self.ok_calls -= 1
        return super().embed_documents(texts)


def test_late_batch_error_keeps_the_vectors_already_made(fresh_db, monkeypatch):
    db = fresh_db
    monkeypatch.setattr(worker, "BATCH", 2)
    with db.ws_tx("alpha") as conn:
        for i in range(5):
            conn.execute(
                "INSERT INTO ws_alpha.facts (type, text) VALUES ('fact', %s)", (f"batch fact {i}",)
            )
    _fact_job(db, "late")
    assert worker.run_once(db, _FailsAfter(ok_calls=2)) is True
    assert _embed_job(db, "late") == ("queued", "embedder", 1)
    with db.ws_tx("alpha") as conn:
        n = conn.execute(
            "SELECT count(*) FROM ws_alpha.embeddings WHERE item_kind = 'fact'"
        ).fetchone()[0]
    assert n == 4  # two full batches were stored before the third failed


def test_sweep_takes_the_oldest_items_first(fresh_db, monkeypatch):
    db = fresh_db
    monkeypatch.setattr(worker, "SWEEP_LIMIT", 1)
    with db.ws_tx("alpha") as conn:
        old = conn.execute(
            "INSERT INTO ws_alpha.facts (type, text, created_at) "
            "VALUES ('fact', 'old', now() - interval '1 day') RETURNING id"
        ).fetchone()[0]
        conn.execute("INSERT INTO ws_alpha.facts (type, text) VALUES ('fact', 'new')")
    with db.ws_tx("alpha") as conn:  # a later row version: the old fact now sits last on disk
        conn.execute("UPDATE ws_alpha.facts SET text = 'old fact' WHERE id = %s", (old,))
    _fact_job(db, "oldest")
    assert worker.run_once(db, FakeEmbedder()) is True
    with db.ws_tx("alpha") as conn:
        ids = conn.execute(
            "SELECT item_id FROM ws_alpha.embeddings WHERE item_kind = 'fact'"
        ).fetchall()
    assert ids == [(old,)]
