import psycopg
import pytest

from treant import jobs


def _clean(db, kind):
    with db.control_tx() as conn:
        conn.execute("DELETE FROM control.jobs WHERE kind = %s", (kind,))


def test_workspace_role_cannot_enqueue_for_other_workspace(db):
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"),
        db.ws_tx("alpha") as conn,
    ):
        jobs.enqueue(conn, "beta", "rls-test", "k1", {})


def test_workspace_role_cannot_read_jobs(db):
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.ws_tx("alpha") as conn:
        conn.execute("SELECT * FROM control.jobs").fetchall()


def test_enqueue_is_idempotent(db):
    _clean(db, "idem")
    for _ in range(2):
        with db.ws_tx("alpha") as conn:
            jobs.enqueue(conn, "alpha", "idem", "k", {"a": 1})
    with db.control_tx() as conn:
        n = conn.execute("SELECT count(*) FROM control.jobs WHERE kind = 'idem'").fetchone()[0]
    assert n == 1


def test_claim_lease_expiry_and_dead_letter(db):
    _clean(db, "lease")
    with db.ws_tx("alpha") as conn:
        jobs.enqueue(conn, "alpha", "lease", "k", {"revision_id": "r"})
    job = jobs.claim(db, {"lease"})
    assert job is not None and (job.workspace, job.kind, job.key) == ("alpha", "lease", "k")
    assert job.payload == {"revision_id": "r"}
    # running with a live lease is not claimable
    assert jobs.claim(db, {"lease"}) is None
    # expire the lease: reclaimable, and no attempt burned
    _expire(db, job.id)
    again = jobs.claim(db, {"lease"})
    assert again is not None and again.id == job.id and again.attempts == 0
    # other kinds are not claimed
    assert jobs.claim(db, {"other"}) is None

    for i in range(8):
        with db.control_tx() as conn:
            conn.execute(
                "UPDATE control.jobs SET status = 'running', run_after = now() WHERE id = %s",
                (job.id,),
            )
        jobs.fail(db, again, "boom")
        with db.control_tx() as conn:
            status, attempts, delay_s, cat = conn.execute(
                "SELECT status, attempts, extract(epoch FROM run_after - now()), error_category "
                "FROM control.jobs WHERE id = %s",
                (job.id,),
            ).fetchone()
        assert attempts == i + 1 and cat == "boom"
        # 30 s doubled per attempt: 60, 120, ... (the same transaction's now())
        assert float(delay_s) == pytest.approx(30 * 2 ** (i + 1), abs=1)
        if i < 7:
            assert status == "queued"
        else:
            assert status == "dead"


def test_complete_marks_done(db):
    _clean(db, "done")
    with db.ws_tx("alpha") as conn:
        jobs.enqueue(conn, "alpha", "done", "k", {})
    job = jobs.claim(db, {"done"})
    jobs.complete(db, job)
    with db.control_tx() as conn:
        s = conn.execute("SELECT status FROM control.jobs WHERE id = %s", (job.id,)).fetchone()[0]
    assert s == "done"
    assert jobs.claim(db, {"done"}) is None


def test_complete_and_fail_only_touch_running_jobs(db):
    _clean(db, "guard")
    with db.ws_tx("alpha") as conn:
        jobs.enqueue(conn, "alpha", "guard", "k", {})
    first = jobs.claim(db, {"guard"})
    _expire(db, first.id)
    second = jobs.claim(db, {"guard"})
    assert second is not None and second.id == first.id
    # simulate the lease having been lost to a different holder that is now dead-lettered
    with db.control_tx() as conn:
        conn.execute("UPDATE control.jobs SET status = 'dead' WHERE id = %s", (first.id,))
    assert jobs.complete(db, first) is False
    assert jobs.fail(db, first, "x") is False
    with db.control_tx() as conn:
        row = conn.execute(
            "SELECT status, attempts FROM control.jobs WHERE id = %s", (first.id,)
        ).fetchone()
    assert row == ("dead", 0)
    # a live running job: complete/fail succeed and report it
    with db.control_tx() as conn:
        conn.execute("UPDATE control.jobs SET status = 'running' WHERE id = %s", (first.id,))
    assert jobs.complete(db, second) is True
    assert jobs.complete(db, second) is False  # already done: not touched again
    with db.control_tx() as conn:
        s = conn.execute("SELECT status FROM control.jobs WHERE id = %s", (first.id,)).fetchone()[0]
    assert s == "done"


def _expire(db, job_id):
    with db.control_tx() as conn:
        conn.execute(
            "UPDATE control.jobs SET lease_until = now() - interval '1 second' WHERE id = %s",
            (job_id,),
        )


def test_complete_rejects_stale_lease(db):
    _clean(db, "stale1")
    with db.ws_tx("alpha") as conn:
        jobs.enqueue(conn, "alpha", "stale1", "k", {})
    first = jobs.claim(db, {"stale1"})
    _expire(db, first.id)
    second = jobs.claim(db, {"stale1"})
    assert second is not None and second.id == first.id and second.lease != first.lease
    assert jobs.complete(db, first) is False
    assert jobs.complete(db, second) is True


def test_fail_rejects_stale_lease(db):
    _clean(db, "stale2")
    with db.ws_tx("alpha") as conn:
        jobs.enqueue(conn, "alpha", "stale2", "k", {})
    first = jobs.claim(db, {"stale2"})
    _expire(db, first.id)
    second = jobs.claim(db, {"stale2"})
    assert jobs.fail(db, first, "x") is False
    assert jobs.fail(db, second, "x") is True


def test_enqueue_run_after_delays_claim(db):
    _clean(db, "delay")
    with db.ws_tx("alpha") as conn:
        jobs.enqueue(conn, "alpha", "delay", "k", {}, run_after_s=3600)
    assert jobs.claim(db, {"delay"}) is None
    with db.control_tx() as conn:
        conn.execute("UPDATE control.jobs SET run_after = now() WHERE kind = 'delay'")
    assert jobs.claim(db, {"delay"}) is not None


def test_skipped_status_allowed(db):
    _clean(db, "skip")
    with db.ws_tx("alpha") as conn:
        jobs.enqueue(conn, "alpha", "skip", "k", {})
    with db.control_tx() as conn:
        conn.execute("UPDATE control.jobs SET status = 'skipped' WHERE kind = 'skip'")


def test_complete_with_skipped_status(db):
    _clean(db, "skipme")
    with db.ws_tx("alpha") as conn:
        jobs.enqueue(conn, "alpha", "skipme", "k", {})
    job = jobs.claim(db, {"skipme"})
    assert jobs.complete(db, job, status="skipped") is True
    with db.control_tx() as conn:
        s = conn.execute("SELECT status FROM control.jobs WHERE id = %s", (job.id,)).fetchone()[0]
    assert s == "skipped"
    with pytest.raises(ValueError):
        jobs.complete(db, job, status="dead")


def test_backoff_is_capped_at_six_hours(db):
    _clean(db, "cap")
    with db.ws_tx("alpha") as conn:
        jobs.enqueue(conn, "alpha", "cap", "k", {})
    job = jobs.claim(db, {"cap"})
    with db.control_tx() as conn:
        conn.execute("UPDATE control.jobs SET attempts = 12 WHERE id = %s", (job.id,))
    assert jobs.fail(db, job, "x", max_attempts=50) is True
    with db.control_tx() as conn:
        status, delay_s = conn.execute(
            "SELECT status, extract(epoch FROM run_after - now()) FROM control.jobs WHERE id = %s",
            (job.id,),
        ).fetchone()
    assert status == "queued" and float(delay_s) == pytest.approx(6 * 3600, abs=1)
    assert jobs.claim(db, {"cap"}) is None  # run_after is in the future


def test_holder_whose_job_was_requeued_cannot_settle_it(db):
    _clean(db, "requeued")
    with db.ws_tx("alpha") as conn:
        jobs.enqueue(conn, "alpha", "requeued", "k", {})
    job = jobs.claim(db, {"requeued"})
    assert jobs.fail(db, job, "x") is True  # back to queued
    assert jobs.complete(db, job) is False
    assert jobs.fail(db, job, "x") is False
    with db.control_tx() as conn:
        row = conn.execute(
            "SELECT status, attempts FROM control.jobs WHERE id = %s", (job.id,)
        ).fetchone()
    assert row == ("queued", 1)
