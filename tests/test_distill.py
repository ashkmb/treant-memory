import json

import pytest
from fakes import FakeBackend

from treant import backends, distill, graph, jobs
from treant.backends import BackendBadReply, BackendRejected, BackendUnavailable
from treant.sessions import save_session

REPLY = {
    "nodes": [
        {"ref": "n1", "kind": "instance", "type": "cluster", "name": "prod-eks"},
        {"ref": "n2", "kind": "local_concept", "type": "tool", "name": "obs", "concept": "signoz"},
    ],
    "edges": [{"src": "n2", "dst": "n1", "type": "runs_on"}],
    "facts": [{"type": "lesson", "text": "pin the chart", "node": "n1"}],
    "general": {
        "concepts": [{"name": "signoz", "type": "tool", "aliases": []}],
        "facts": [{"type": "lesson", "text": "pin chart versions", "concept": "signoz"}],
    },
}


def _job(ws, rev, kind="distill"):
    return jobs.Job(0, ws, kind, rev, {"revision_id": rev}, 0, "lease")


def _rev_id(db, res, ws="alpha"):
    with db.ws_tx(ws) as conn:
        return str(
            conn.execute(
                f"SELECT id FROM ws_{ws}.revisions WHERE session_id = %s AND n = %s",
                (res.session_id, res.revision),
            ).fetchone()[0]
        )


def _factory(backend):
    return lambda env, ws, s: backend


def test_parse_generation_accepts_valid():
    gen, dropped = distill.parse_generation("```json\n" + json.dumps(REPLY) + "\n```")
    assert dropped == 0
    assert [n["ref"] for n in gen["nodes"]] == ["n1", "n2"]
    assert gen["nodes"][1]["concept"] == "signoz"
    assert gen["edges"] == REPLY["edges"]
    assert gen["facts"][0]["node"] == "n1" and gen["facts"][0]["supersedes"] is None
    assert gen["general"]["concepts"][0]["name"] == "signoz"
    assert gen["general"]["facts"][0]["concept"] == "signoz"


def test_parse_generation_drops_invalid_items():
    bad = {
        "nodes": [
            {"ref": "n1", "kind": "instance", "type": "t", "name": "ok"},
            {"ref": "n2", "kind": "bogus", "type": "t", "name": "x"},
            {"ref": "n3", "kind": "concept", "type": "t", "name": "x"},
            {"ref": "n4", "kind": "local_concept", "type": "t", "name": "x"},
        ],
        "edges": [
            {"src": "n1", "dst": "n9", "type": "uses"},
            {"src": "n1", "dst": "n1", "type": "nope"},
        ],
        "facts": [
            {"type": "weird", "text": "x"},
            {"type": "fact", "text": "y" * 2001},
            {"type": "fact", "text": "fine", "node": "n1"},
        ],
        "general": {"concepts": [{"name": "", "type": "t"}], "facts": [{"type": "fact"}]},
    }
    gen, dropped = distill.parse_generation(json.dumps(bad))
    assert [n["ref"] for n in gen["nodes"]] == ["n1"]
    assert gen["edges"] == [] and len(gen["facts"]) == 1
    assert gen["general"] == {"concepts": [], "facts": []}
    assert dropped == 3 + 2 + 2 + 2
    empty, n = distill.parse_generation("not json at all")
    assert empty == distill.empty_generation() and n == 1


def test_parse_generation_caps():
    nodes = [{"ref": f"n{i}", "kind": "instance", "type": "t", "name": "a"} for i in range(101)]
    gen, dropped = distill.parse_generation(json.dumps({"nodes": nodes}))
    assert len(gen["nodes"]) == 100 and dropped == 1


def test_run_stores_generation_and_enqueues_apply(db, ctx_a):
    res = save_session(db, ctx_a, "d-store", {"journal": "worked on prod-eks"})
    rev = _rev_id(db, res)
    fake = FakeBackend([json.dumps(REPLY)])
    assert distill.run(db, {}, _job("alpha", rev), _factory(fake)) == "done"
    with db.ws_tx("alpha") as conn:
        out = conn.execute(
            "SELECT output FROM ws_alpha.generations WHERE revision_id = %s", (rev,)
        ).fetchone()[0]
    assert out["nodes"][0]["name"] == "prod-eks" and out["dropped"] == 0
    assert out["fact_refs"] == {}
    with db.control_tx() as conn:
        n = conn.execute(
            "SELECT count(*) FROM control.jobs WHERE kind = 'apply' AND key = %s", (rev,)
        ).fetchone()[0]
    assert n == 1
    assert out["stats"] == {"kept": 6, "dropped": 0}
    # re-run upserts
    fake2 = FakeBackend([json.dumps({"facts": [{"type": "fact", "text": "only"}, {"type": "x"}]})])
    assert distill.run(db, {}, _job("alpha", rev), _factory(fake2)) == "done"
    with db.ws_tx("alpha") as conn:
        out = conn.execute(
            "SELECT output FROM ws_alpha.generations WHERE revision_id = %s", (rev,)
        ).fetchone()[0]
    assert out["nodes"] == [] and out["dropped"] == 1
    assert out["stats"] == {"kept": 1, "dropped": 1}


def test_run_distills_older_revision(db, ctx_a):
    r1 = save_session(db, ctx_a, "d-old", {"journal": "first"})
    save_session(db, ctx_a, "d-old", {"journal": "second"})
    fake = FakeBackend([json.dumps(REPLY)])
    rev = _rev_id(db, r1)
    assert distill.run(db, {}, _job("alpha", rev), _factory(fake)) == "done"
    assert len(fake.calls) == 1
    with db.ws_tx("alpha") as conn:
        n = conn.execute(
            "SELECT count(*) FROM ws_alpha.generations WHERE revision_id = %s", (rev,)
        ).fetchone()[0]
    assert n == 1


def test_run_skips_non_stored_revision(db, ctx_a):
    res = save_session(db, ctx_a, "d-drop", {"journal": "x"})
    rev = _rev_id(db, res)
    with db.admin_tx() as conn:
        conn.execute("UPDATE ws_alpha.revisions SET status = 'dropped' WHERE id = %s", (rev,))
    fake = FakeBackend([json.dumps(REPLY)])
    assert distill.run(db, {}, _job("alpha", rev), _factory(fake)) == "skipped"


def test_run_no_backend(db, ctx_a):
    res = save_session(db, ctx_a, "d-none", {"journal": "x"})
    rev = _rev_id(db, res)
    assert distill.run(db, {}, _job("alpha", rev), lambda env, ws, s: None) == "no_backend"
    with db.ws_tx("alpha") as conn:
        n = conn.execute(
            "SELECT count(*) FROM ws_alpha.generations WHERE revision_id = %s", (rev,)
        ).fetchone()[0]
    assert n == 0


def test_run_model_down_propagates_and_handle_fails_retryable(db, ctx_a):
    with db.control_tx() as conn:
        conn.execute("DELETE FROM control.jobs WHERE kind = 'distill'")
    res = save_session(db, ctx_a, "d-down", {"journal": "x"})
    rev = _rev_id(db, res)
    with pytest.raises(BackendUnavailable):
        distill.run(db, {}, _job("alpha", rev), _factory(FakeBackend(unavailable=True)))
    job = jobs.claim(db, {"distill"})
    assert job.key == rev
    status = distill.handle(db, {}, job, _factory(FakeBackend(unavailable=True)))
    assert status == "model_unavailable"
    with db.control_tx() as conn:
        st, cat = conn.execute(
            "SELECT status, error_category FROM control.jobs WHERE id = %s", (job.id,)
        ).fetchone()
    assert (st, cat) == ("queued", "model_unavailable")


def test_handle_rejected_missing_key_and_done(db, ctx_a):
    with db.control_tx() as conn:
        conn.execute("DELETE FROM control.jobs WHERE kind = 'distill'")
    res = save_session(db, ctx_a, "d-handle", {"journal": "x"})
    rev = _rev_id(db, res)

    def claim():
        with db.control_tx() as conn:
            conn.execute(
                "UPDATE control.jobs SET status = 'queued', run_after = now() "
                "WHERE kind = 'distill' AND key = %s",
                (rev,),
            )
        return jobs.claim(db, {"distill"})

    def category(job):
        with db.control_tx() as conn:
            return conn.execute(
                "SELECT status, error_category FROM control.jobs WHERE id = %s", (job.id,)
            ).fetchone()

    job = claim()
    rejected = FakeBackend([BackendRejected("rejected")])
    assert distill.handle(db, {}, job, _factory(rejected)) == "model_rejected"
    assert category(job) == ("queued", "model_rejected")

    job = claim()
    # real factory with no key in env: unavailable, retried
    assert distill.handle(db, {}, job, backends.for_workspace) == "model_unavailable"
    assert category(job)[1] == "model_unavailable"

    job = claim()
    assert distill.handle(db, {}, job, _factory(FakeBackend([json.dumps(REPLY)]))) == "done"
    assert category(job) == ("done", None)


def test_prompt_carries_fact_refs(db, ctx_a):
    with db.ws_tx("alpha") as conn:
        node = graph.add_node(conn, "alpha", "instance", "cluster", "zebra-cluster-x")
        fid = graph.add_fact(conn, "alpha", "lesson", "zebra lesson", node_id=node)
        graph.add_fact(
            conn,
            "alpha",
            "lesson",
            "unrelated",
            node_id=graph.add_node(conn, "alpha", "instance", "cluster", "unmentioned-thing"),
        )
    with db.admin_tx() as conn:
        conn.execute(
            "INSERT INTO general.nodes (kind, type, name) VALUES ('concept', 'tool', 'quokka')"
        )
    res = save_session(db, ctx_a, "d-refs", {"journal": "fixed Zebra-Cluster-X with QUOKKA"})
    rev = _rev_id(db, res)
    reply = {
        "facts": [
            {"type": "fact", "text": "t", "supersedes": "f1"},
            {"type": "fact", "text": "t2", "supersedes": "f99"},
        ]
    }
    fake = FakeBackend([json.dumps(reply)])
    assert distill.run(db, {}, _job("alpha", rev), _factory(fake)) == "done"
    messages, alias = fake.calls[0]
    assert alias == "default"
    assert "reply" in messages[0]["content"].lower() or "JSON" in messages[0]["content"]
    body = json.loads(messages[1]["content"])
    assert body["existing_facts"] == [
        {"ref": "f1", "type": "lesson", "text": "zebra lesson", "node": "zebra-cluster-x"}
    ]
    assert "quokka" in body["general_concepts"]
    assert "id" not in json.dumps(body["existing_facts"])
    with db.ws_tx("alpha") as conn:
        out = conn.execute(
            "SELECT output FROM ws_alpha.generations WHERE revision_id = %s", (rev,)
        ).fetchone()[0]
    assert out["fact_refs"] == {"f1": fid}
    assert [f["supersedes"] for f in out["facts"]] == ["f1", None]


@pytest.mark.parametrize("bad", [[1], {"a": 1}])
def test_parse_generation_unhashable_values_are_dropped(bad):
    reply = {
        "nodes": [{"ref": "n1", "kind": "instance", "type": "t", "name": "ok"}],
        "edges": [
            {"src": bad, "dst": "n1", "type": "uses"},
            {"src": "n1", "dst": bad, "type": "uses"},
            {"src": "n1", "dst": "n1", "type": bad},
        ],
        "facts": [
            {"type": bad, "text": "x"},
            {"type": "fact", "text": "x", "node": bad},
            {"type": "fact", "text": "x", "supersedes": bad},
        ],
        "general": {"facts": [{"type": bad, "text": "x", "concept": "c"}]},
    }
    gen, dropped = distill.parse_generation(json.dumps(reply))
    assert len(gen["nodes"]) == 1 and gen["edges"] == [] and gen["facts"] == []
    assert dropped == 7


def test_parse_generation_deep_and_oversize_replies():
    for text in ("[" * 100000, " " * 200_001):
        gen, dropped = distill.parse_generation(text)
        assert gen == distill.empty_generation() and dropped == 1


def test_parse_generation_rejects_nul_and_surrogates():
    reply = {
        "nodes": [
            {"ref": "n1", "kind": "instance", "type": "t", "name": "a\u0000b"},
            {"ref": "n2", "kind": "instance", "type": "t", "name": "ok", "aliases": ["\ud800"]},
        ],
        "facts": [{"type": "fact", "text": "x\ud800y"}, {"type": "fact", "text": "x\u0000"}],
    }
    gen, dropped = distill.parse_generation(json.dumps(reply))
    assert gen["nodes"] == [] and gen["facts"] == [] and dropped == 4


def test_run_with_nul_reply_completes(db, ctx_a):
    res = save_session(db, ctx_a, "d-nul", {"journal": "x"})
    rev = _rev_id(db, res)
    reply = {"facts": [{"type": "fact", "text": "bad\u0000text"}]}
    fake = FakeBackend([json.dumps(reply)])
    assert distill.run(db, {}, _job("alpha", rev), _factory(fake)) == "done"


def _generation(db, rev, ws="alpha"):
    with db.ws_tx(ws) as conn:
        row = conn.execute(
            f"SELECT output FROM ws_{ws}.generations WHERE revision_id = %s", (rev,)
        ).fetchone()
    return row[0] if row else None


@pytest.mark.parametrize(
    "reply",
    ["not json at all", '{"nodes": [', "[1, 2]", '"just a string"', "x" * 200_001],
)
def test_bad_reply_stores_no_generation(db, ctx_a, reply):
    res = save_session(db, ctx_a, f"d-bad-{len(reply)}-{reply[:3]}", {"journal": "x"})
    rev = _rev_id(db, res)
    with pytest.raises(BackendBadReply):
        distill.run(db, {}, _job("alpha", rev), _factory(FakeBackend([reply])))
    assert _generation(db, rev) is None
    with db.control_tx() as conn:
        n = conn.execute(
            "SELECT count(*) FROM control.jobs WHERE kind = 'apply' AND key = %s", (rev,)
        ).fetchone()[0]
    assert n == 0


def test_bad_reply_keeps_the_previous_generation(db, ctx_a):
    res = save_session(db, ctx_a, "d-bad-keep", {"journal": "worked on prod-eks"})
    rev = _rev_id(db, res)
    assert distill.run(db, {}, _job("alpha", rev), _factory(FakeBackend([json.dumps(REPLY)])))
    with pytest.raises(BackendBadReply):
        distill.run(db, {}, _job("alpha", rev), _factory(FakeBackend(["garbage"])))
    assert _generation(db, rev)["nodes"][0]["name"] == "prod-eks"


def test_handle_bad_reply_fails_with_model_bad_reply_and_dies_at_three(db, ctx_a):
    with db.control_tx() as conn:
        conn.execute("DELETE FROM control.jobs WHERE kind = 'distill'")
    res = save_session(db, ctx_a, "d-bad-handle", {"journal": "x"})
    rev = _rev_id(db, res)
    states = []
    for _ in range(3):
        with db.control_tx() as conn:
            conn.execute(
                "UPDATE control.jobs SET run_after = now() WHERE kind = 'distill' AND key = %s",
                (rev,),
            )
        job = jobs.claim(db, {"distill"})
        assert job.key == rev
        assert distill.handle(db, {}, job, _factory(FakeBackend(["nope"]))) == "model_bad_reply"
        with db.control_tx() as conn:
            states.append(
                conn.execute(
                    "SELECT status, attempts, error_category FROM control.jobs WHERE id = %s",
                    (job.id,),
                ).fetchone()
            )
    assert states == [
        ("queued", 1, "model_bad_reply"),
        ("queued", 2, "model_bad_reply"),
        ("dead", 3, "model_bad_reply"),
    ]
    assert _generation(db, rev) is None


def test_empty_backend_reply_is_model_bad_reply(db, ctx_a):
    with db.control_tx() as conn:
        conn.execute("DELETE FROM control.jobs WHERE kind = 'distill'")
    res = save_session(db, ctx_a, "d-empty", {"journal": "x"})
    rev = _rev_id(db, res)
    job = jobs.claim(db, {"distill"})
    assert job.key == rev
    backend = FakeBackend([BackendBadReply("empty reply")])
    assert distill.handle(db, {}, job, _factory(backend)) == "model_bad_reply"
    assert backend.max_tokens == [None]  # distill uses the backend's default (backends.MAX_TOKENS)


def test_handle_skipped_marks_job_skipped(db, ctx_a):
    with db.control_tx() as conn:
        conn.execute("DELETE FROM control.jobs WHERE kind = 'distill'")
    r1 = save_session(db, ctx_a, "d-skip-handle", {"journal": "first"})
    old = _rev_id(db, r1)
    with db.admin_tx() as conn:  # the revision was dropped after its job was queued
        conn.execute("UPDATE ws_alpha.revisions SET status = 'dropped' WHERE id = %s", (old,))
    job = jobs.claim(db, {"distill"})
    assert job.key == old
    assert distill.handle(db, {}, job, _factory(FakeBackend([]))) == "skipped"
    with db.control_tx() as conn:
        st = conn.execute("SELECT status FROM control.jobs WHERE id = %s", (job.id,)).fetchone()[0]
    assert st == "skipped"


def test_model_unavailable_retries_up_to_twelve_attempts(db, ctx_a):
    with db.control_tx() as conn:
        conn.execute("DELETE FROM control.jobs WHERE kind = 'distill'")
    res = save_session(db, ctx_a, "d-twelve", {"journal": "x"})
    rev = _rev_id(db, res)
    with db.control_tx() as conn:
        conn.execute("UPDATE control.jobs SET attempts = 10 WHERE kind = 'distill'")
    states = []
    for _ in range(2):
        with db.control_tx() as conn:
            conn.execute("UPDATE control.jobs SET run_after = now() WHERE kind = 'distill'")
        job = jobs.claim(db, {"distill"})
        assert job.key == rev
        down = _factory(FakeBackend(unavailable=True))
        assert distill.handle(db, {}, job, down) == "model_unavailable"
        with db.control_tx() as conn:
            states.append(
                conn.execute(
                    "SELECT status, attempts FROM control.jobs WHERE id = %s", (job.id,)
                ).fetchone()
            )
    assert states == [("queued", 11), ("dead", 12)]


# --- final review fixes: scrubbed prompt facts, backfill ------------------------------------


def _terms(db, ws, terms):
    from psycopg.types.json import Jsonb

    with db.admin_tx() as conn:
        conn.execute(
            "UPDATE control.workspaces SET settings = %s WHERE name = %s",
            (Jsonb({"rules": {"terms": terms}}), ws),
        )


def test_existing_facts_are_scrubbed_in_the_prompt(fresh_db, ctx_a):
    db = fresh_db
    with db.ws_tx("alpha") as conn:
        node = graph.add_node(conn, "alpha", "instance", "cluster", "zebra-cluster")
        graph.add_fact(conn, "alpha", "lesson", "ask ops.vintracorp.io first", node_id=node)
        many = " ".join(f"10.0.0.{i}" for i in range(1, 15))
        graph.add_fact(conn, "alpha", "fact", f"hosts {many}", node_id=node)
        graph.add_fact(conn, "alpha", "fact", "plain zebra fact", node_id=node)
    _terms(db, "alpha", ["vintracorp"])
    res = save_session(db, ctx_a, "d-scrub-prompt", {"journal": "fixed zebra-cluster"})
    rev = _rev_id(db, res)
    fake = FakeBackend([json.dumps({})])
    assert distill.run(db, {}, _job("alpha", rev), _factory(fake)) == "done"
    body = json.loads(fake.calls[0][0][1]["content"])
    texts = sorted(f["text"] for f in body["existing_facts"])
    assert texts == ["ask [removed] first", "plain zebra fact"]  # the unsure one is left out
    assert sorted(body["existing_facts"][i]["ref"] for i in range(2)) == ["f1", "f2"]
    out = _generation(db, rev)
    assert set(out["fact_refs"]) == {"f1", "f2"}


def _rev(db, ws, session_client, n=None):
    with db.ws_tx(ws) as conn:
        q = (
            f"SELECT r.id::text FROM ws_{ws}.revisions r JOIN ws_{ws}.sessions s "
            "ON s.id = r.session_id WHERE s.client_session_id = %s ORDER BY r.n"
        )
        ids = [r[0] for r in conn.execute(q, (session_client,))]
    return ids if n is None else ids[n - 1]


def _no_distill_job(db, rev):
    with db.control_tx() as conn:
        conn.execute("DELETE FROM control.jobs WHERE kind = 'distill' AND key = %s", (rev,))


def _distill_jobs(db, ws="alpha"):
    with db.control_tx() as conn:
        return {
            r[0]
            for r in conn.execute(
                "SELECT key FROM control.jobs WHERE kind = 'distill' AND workspace = %s", (ws,)
            )
        }


def test_backfill_queues_rescrubs_and_quarantines(fresh_db, ctx_a):
    from fakes import FakeEmbedder

    from treant import worker

    db = fresh_db
    host = "build.quorvacorp.io"
    save_session(db, ctx_a, "bf-dirty", {"journal": f"deployed on {host}", "notes": host})
    save_session(db, ctx_a, "bf-clean", {"journal": "clean words"})
    save_session(db, ctx_a, "bf-two", {"journal": "first"})
    save_session(db, ctx_a, "bf-two", {"journal": "second"})
    save_session(db, ctx_a, "bf-many", {"journal": " ".join([host] * 13)})
    save_session(db, ctx_a, "bf-hasjob", {"journal": "job exists"})
    save_session(db, ctx_a, "bf-hasgen", {"journal": "generation exists"})
    while worker.run_once(db, FakeEmbedder()):
        pass
    for name in ("bf-dirty", "bf-clean", "bf-two", "bf-many", "bf-hasgen"):
        for rev in _rev(db, "alpha", name):
            _no_distill_job(db, rev)
    with db.ws_tx("alpha") as conn:
        conn.execute(
            "INSERT INTO ws_alpha.generations (revision_id, output) VALUES (%s, '{}')",
            (_rev(db, "alpha", "bf-hasgen", 1),),
        )
    _terms(db, "alpha", ["quorvacorp"])
    before = _distill_jobs(db)

    assert distill.backfill(db, "alpha", dry_run=True) == {
        "candidates": 5,
        "queued": 4,
        "rescrubbed": 2,
        "quarantined": 1,
    }
    assert _distill_jobs(db) == before
    with db.ws_tx("alpha") as conn:
        assert (
            host
            in conn.execute(
                "SELECT text FROM ws_alpha.journal_entries WHERE revision_id = %s",
                (_rev(db, "alpha", "bf-dirty", 1),),
            ).fetchone()[0]
        )

    counts = distill.backfill(db, "alpha")
    assert counts == {"candidates": 5, "queued": 4, "rescrubbed": 2, "quarantined": 1}
    dirty, clean = _rev(db, "alpha", "bf-dirty", 1), _rev(db, "alpha", "bf-clean", 1)
    two_old, two_new = _rev(db, "alpha", "bf-two")
    many = _rev(db, "alpha", "bf-many", 1)
    assert _distill_jobs(db) - before == {dirty, clean, two_old, two_new}  # older revision too
    with db.ws_tx("alpha") as conn:
        sections, status = conn.execute(
            "SELECT sections, status FROM ws_alpha.revisions WHERE id = %s", (dirty,)
        ).fetchone()
        journal_id, journal = conn.execute(
            "SELECT id, text FROM ws_alpha.journal_entries WHERE revision_id = %s", (dirty,)
        ).fetchone()
        embedded = conn.execute(
            "SELECT count(*) FROM ws_alpha.embeddings WHERE item_kind = 'journal' AND item_id = %s",
            (journal_id,),
        ).fetchone()[0]
        many_status = conn.execute(
            "SELECT status FROM ws_alpha.revisions WHERE id = %s", (many,)
        ).fetchone()[0]
    assert status == "stored" and host not in json.dumps(sections) and host not in journal
    assert sections == {"journal": "deployed on [removed]", "notes": "[removed]"}
    assert embedded == 0 and many_status == "quarantined"
    with db.control_tx() as conn:
        embed_keys = {
            r[0] for r in conn.execute("SELECT key FROM control.jobs WHERE kind = 'embed'")
        }
        audit = conn.execute(
            "SELECT detail FROM control.audit WHERE action = 'distill.backfill'"
        ).fetchall()
    assert f"backfill:{dirty}" in embed_keys
    assert audit == [({"workspace": "alpha", **counts},)]
    # a second run finds nothing left to do
    assert distill.backfill(db, "alpha")["candidates"] == 0


def test_backfill_requeues_revision_skipped_under_newest_only(fresh_db, ctx_a):
    db = fresh_db
    save_session(db, ctx_a, "bf-skipped", {"journal": "first"})
    old = _rev(db, "alpha", "bf-skipped", 1)
    with db.control_tx() as conn:  # what the newest-only rule left behind
        conn.execute(
            "UPDATE control.jobs SET status = 'skipped', attempts = 1 "
            "WHERE kind = 'distill' AND key = %s",
            (old,),
        )
    assert distill.backfill(db, "alpha", dry_run=True)["candidates"] == 1
    with db.control_tx() as conn:
        assert conn.execute(
            "SELECT status FROM control.jobs WHERE kind = 'distill' AND key = %s", (old,)
        ).fetchone() == ("skipped",)
    assert distill.backfill(db, "alpha")["queued"] == 1
    with db.control_tx() as conn:
        assert conn.execute(
            "SELECT status, attempts FROM control.jobs WHERE kind = 'distill' AND key = %s", (old,)
        ).fetchall() == [("queued", 0)]
    assert distill.backfill(db, "alpha")["candidates"] == 0


def test_backfill_cli_prints_counts_only(fresh, fresh_db, ctx_a, capsys):
    from treant import cli

    save_session(fresh_db, ctx_a, "bf-cli", {"journal": "secret-ish words"})
    _no_distill_job(fresh_db, _rev(fresh_db, "alpha", "bf-cli", 1))
    capsys.readouterr()
    assert cli.main(["distill", "backfill", "alpha", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "candidates=1" in out and "queued=1" in out and "dry run" in out
    assert "words" not in out
    assert cli.main(["distill", "backfill", "alpha"]) == 0
    assert "queued=1" in capsys.readouterr().out
    assert cli.main(["distill", "backfill", "nosuchws"]) == 1
