import json
import uuid

import httpx
import pytest
import respx
from fakes import FakeBackend
from psycopg.types.json import Jsonb

from treant import apply, gate, graph, jobs
from treant.backends import BackendBadReply, BackendRejected, BackendUnavailable
from treant.errors import InvalidInput
from treant.sessions import save_session


def _tag() -> str:
    return uuid.uuid4().hex[:8]


class Recorder:
    """Scripted judges recording every text they were asked about."""

    def __init__(self, jev=0.95, llm=True):
        self.jev_value, self.llm_value = jev, llm
        self.jev_calls: list[str] = []
        self.llm_calls: list[str] = []
        self.kinds: list[tuple[str, str]] = []

    def _next(self, value):
        if isinstance(value, list):
            value = value.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def jev(self, text: str, kind: str) -> float:
        self.jev_calls.append(text)
        self.kinds.append(("jev", kind))
        return self._next(self.jev_value)

    def llm(self, text: str, kind: str) -> bool:
        self.llm_calls.append(text)
        self.kinds.append(("llm", kind))
        return self._next(self.llm_value)

    @property
    def judges(self) -> gate.Judges:
        return gate.Judges(jev=self.jev, llm=self.llm)


def _factory(backend):
    def make(env, ws, s):
        return backend

    return make


def _no_backend(env, ws, s):
    raise AssertionError("the backend must not be used here")


def _row(db, ws, item, status="pending", rewrites=0):
    """Insert a queue row as the publisher; returns (queue id, session id, revision id)."""
    session, rev = str(uuid.uuid4()), str(uuid.uuid4())
    source = item.get("name") if item["kind"] == "concept" else item["text"]
    item = {**item, "source_text": source}
    with db.publisher_tx() as conn:
        qid = conn.execute(
            "INSERT INTO control.general_queue "
            "(workspace, session_id, revision_id, item, item_key, status, rewrites) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (ws, session, rev, Jsonb(item), apply.item_key(item["kind"], source), status, rewrites),
        ).fetchone()[0]
    return qid, session, rev


def _fact(text, concept=None, type_="lesson"):
    return {"kind": "fact", "type": type_, "text": text, "concept": concept}


def _concept(name, aliases=(), type_="tool"):
    return {"kind": "concept", "name": name, "type": type_, "aliases": list(aliases)}


def _job(ws, qid, key=None):
    return jobs.Job(0, ws, "gate", key or str(qid), {"queue_id": qid}, 0, "lease")


def _queue(db, qid):
    with db.control_tx() as conn:
        return conn.execute(
            "SELECT status, item, reasons, rewrites, published_id::text, decided_at "
            "FROM control.general_queue WHERE id = %s",
            (qid,),
        ).fetchone()


def _ctl(db, query, params=()):
    with db.control_tx() as conn:
        return conn.execute(query, params).fetchall()


def _general_fact(db, fid):
    with db.publisher_tx() as conn:
        return conn.execute(
            "SELECT type, text, node_id::text, status FROM general.facts WHERE id = %s", (fid,)
        ).fetchone()


def _general_concept(db, name):
    with db.publisher_tx() as conn:
        return conn.execute(
            "SELECT id::text FROM general.nodes WHERE kind = 'concept' AND name = %s", (name,)
        ).fetchall()


def _set_settings(db, ws, raw):
    with db.admin_tx() as conn:
        conn.execute(
            "UPDATE control.workspaces SET settings = %s WHERE name = %s", (Jsonb(raw), ws)
        )


def _reply(text):
    return json.dumps({"text": text})


def test_gate_passes_and_publishes_with_origin(db):
    t = _tag()
    with db.publisher_tx() as conn:
        concept_id = graph.add_node(conn, "general", "concept", "tool", f"probetool{t}")
    qid, session, rev = _row(db, "alpha", _fact(f"Probe readiness first {t}", f"PROBETOOL{t}"))
    rec = Recorder()
    status = gate.run(db, {}, _job("alpha", qid), rec.judges, backend_factory=_no_backend)
    assert status == "published"
    assert rec.jev_calls == [f"Probe readiness first {t}"] == rec.llm_calls
    st, item, reasons, rewrites, published_id, decided = _queue(db, qid)
    assert (st, reasons, rewrites) == ("published", [], 0) and decided is not None
    assert item["source_text"] == item["text"] == f"Probe readiness first {t}"
    assert _general_fact(db, published_id) == (
        "lesson",
        f"Probe readiness first {t}",
        concept_id,
        "current",
    )
    origin = _ctl(
        db,
        "SELECT item_kind, workspace, session_id::text, revision_id::text, queue_id "
        "FROM control.general_origin WHERE item_id = %s",
        (published_id,),
    )
    assert origin == [("fact", "alpha", session, rev, qid)]

    # a fact whose concept is not in general publishes unattached
    qid2, _, _ = _row(db, "alpha", _fact(f"Lone lesson {t}", f"missingtool{t}"))
    assert gate.run(db, {}, _job("alpha", qid2), rec.judges, _no_backend) == "published"
    assert _general_fact(db, _queue(db, qid2)[4])[2] is None


def test_gate_review_mode_waits(fresh_db):
    _set_settings(fresh_db, "alpha", {"publish": "review"})
    qid, _, _ = _row(fresh_db, "alpha", _fact("Pin chart versions in CI"))
    rec = Recorder()
    status = gate.run(fresh_db, {}, _job("alpha", qid), rec.judges, _no_backend)
    assert status == "waiting_review"
    st, _, reasons, _, published_id, _ = _queue(fresh_db, qid)
    assert (st, reasons, published_id) == ("waiting_review", [], None)
    with fresh_db.publisher_tx() as conn:
        assert conn.execute("SELECT count(*) FROM general.facts").fetchone()[0] == 0
    # the owner approves later through publish
    gid = gate.publish(fresh_db, qid, actor="owner")
    assert _queue(fresh_db, qid)[0] == "published"
    assert _general_fact(fresh_db, gid)[1] == "Pin chart versions in CI"


def test_gate_rule_failure_rewrites_then_publishes(db):
    t = _tag()
    text = f"Fix for ABC-123 was to pin the chart {t}"
    qid, _, _ = _row(db, "alpha", _fact(text))
    rec = Recorder()
    backend = FakeBackend([_reply(f"Pin the chart version {t}")])
    status = gate.run(db, {}, _job("alpha", qid), rec.judges, _factory(backend))
    assert status == "rewritten"
    assert rec.jev_calls == [] and rec.llm_calls == []  # rules failed: no judge was asked
    st, item, reasons, rewrites, _, _ = _queue(db, qid)
    assert (st, rewrites) == ("pending", 1)
    assert item["text"] == f"Pin the chart version {t}" and item["source_text"] == text
    assert reasons == [{"category": "ticket_id", "start": 8, "end": 15}]
    keys = _ctl(
        db, "SELECT payload FROM control.jobs WHERE kind = 'gate' AND key = %s", (f"{qid}:r1",)
    )
    assert keys == [({"queue_id": qid},)]

    status = gate.run(db, {}, _job("alpha", qid, f"{qid}:r1"), rec.judges, _no_backend)
    assert status == "published"
    assert rec.jev_calls == [f"Pin the chart version {t}"]
    st, item, reasons, rewrites, published_id, _ = _queue(db, qid)
    assert (st, reasons, rewrites) == ("published", [], 1)
    assert item["source_text"] == text
    assert _general_fact(db, published_id)[1] == f"Pin the chart version {t}"


def test_gate_blocks_after_two_rewrites(db):
    t = _tag()
    text = f"Host 10.1.2.3 needed a restart {t}"
    qid, _, _ = _row(db, "alpha", _fact(text))
    backend = FakeBackend([_reply(f"Host 10.1.2.4 needed a restart {t}"), "not json at all"])
    rec = Recorder()
    assert gate.run(db, {}, _job("alpha", qid), rec.judges, _factory(backend)) == "rewritten"
    assert gate.run(db, {}, _job("alpha", qid, f"{qid}:r1"), rec.judges, _factory(backend)) == (
        "rewritten"
    )
    # the bad reply counted as a rewrite and kept the previous text
    st, item, _, rewrites, _, _ = _queue(db, qid)
    assert (st, rewrites, item["text"]) == ("pending", 2, f"Host 10.1.2.4 needed a restart {t}")
    assert _ctl(
        db, "SELECT count(*) FROM control.jobs WHERE kind = 'gate' AND key = %s", (f"{qid}:r2",)
    ) == [(1,)]
    assert gate.run(db, {}, _job("alpha", qid, f"{qid}:r2"), rec.judges, _no_backend) == "blocked"
    st, item, reasons, rewrites, published_id, decided = _queue(db, qid)
    assert (st, rewrites, published_id) == ("blocked", 2, None) and decided is not None
    assert reasons == [{"category": "ip_address", "start": 5, "end": 13}]
    assert item["source_text"] == text
    assert len(backend.calls) == 2 and rec.jev_calls == []


def test_gate_requires_both_judges(db):
    t = _tag()
    qid, _, _ = _row(db, "alpha", _fact(f"Our team prefers small pull requests {t}"))
    rec = Recorder(jev=0.97, llm=False)
    backend = FakeBackend([_reply(f"Small pull requests review faster {t}")])
    assert gate.run(db, {}, _job("alpha", qid), rec.judges, _factory(backend)) == "rewritten"
    assert len(rec.jev_calls) == 0 and len(rec.llm_calls) == 1  # a "no" from the LLM: no Jev
    text = f"Our team prefers small pull requests {t}"
    assert _queue(db, qid)[2] == [{"category": "judge:llm", "start": 0, "end": len(text)}]

    # Jev below the threshold fails too (the LLM said yes, so Jev is asked)
    qid2, _, _ = _row(db, "alpha", _fact(f"Another lesson {t}"))
    rec2 = Recorder(jev=0.5, llm=True)
    backend2 = FakeBackend([_reply(f"Another rewritten lesson {t}")])
    assert gate.run(db, {}, _job("alpha", qid2), rec2.judges, _factory(backend2)) == "rewritten"
    assert len(rec2.jev_calls) == 1 and len(rec2.llm_calls) == 1
    assert [r["category"] for r in _queue(db, qid2)[2]] == ["judge:jev"]


def test_gate_job_retries_when_judge_down(fresh_db):
    db = fresh_db
    qid, _, _ = _row(db, "alpha", _fact("Pin chart versions in CI"))
    with db.publisher_tx() as conn:
        jobs.enqueue(conn, "alpha", "gate", str(qid), {"queue_id": qid})
    job = jobs.claim(db, {"gate"})
    rec = Recorder(jev=BackendUnavailable("down"))
    assert gate.handle(db, {}, job, rec.judges, _no_backend) == "judge_unavailable"
    row = _ctl(
        db, "SELECT status, attempts, error_category FROM control.jobs WHERE id = %s", (job.id,)
    )
    assert row == [("queued", 1, "judge_unavailable")]
    assert _queue(db, qid)[0] == "pending"

    # judge_unavailable uses max_attempts=20: attempt 19 is still queued, attempt 20 dead-letters
    with db.control_tx() as conn:
        conn.execute(
            "UPDATE control.jobs SET attempts = 18, run_after = now() WHERE id = %s", (job.id,)
        )
    job = jobs.claim(db, {"gate"})
    rec = Recorder(llm=BackendUnavailable("down"))
    assert gate.handle(db, {}, job, rec.judges, _no_backend) == "judge_unavailable"
    row = _ctl(
        db, "SELECT status, attempts, error_category FROM control.jobs WHERE id = %s", (job.id,)
    )
    assert row == [("queued", 19, "judge_unavailable")]
    with db.control_tx() as conn:
        conn.execute("UPDATE control.jobs SET run_after = now() WHERE id = %s", (job.id,))
    job = jobs.claim(db, {"gate"})
    assert gate.handle(db, {}, job, Recorder(jev=BackendUnavailable("x")).judges) == (
        "judge_unavailable"
    )
    assert _ctl(db, "SELECT status FROM control.jobs WHERE id = %s", (job.id,)) == [("dead",)]
    assert _queue(db, qid)[0] == "pending"


def test_rewrite_backend_down_retries(fresh_db):
    db = fresh_db
    qid, _, _ = _row(db, "alpha", _fact("Ticket ABC-77 was about chart pins"))
    with db.publisher_tx() as conn:
        jobs.enqueue(conn, "alpha", "gate", str(qid), {"queue_id": qid})
    job = jobs.claim(db, {"gate"})
    backend = FakeBackend(unavailable=True)
    assert gate.handle(db, {}, job, Recorder().judges, _factory(backend)) == "model_unavailable"
    assert _ctl(db, "SELECT status, attempts FROM control.jobs WHERE id = %s", (job.id,)) == [
        ("queued", 1)
    ]
    assert _queue(db, qid)[0] == "pending" and _queue(db, qid)[3] == 0


def test_settings_invalid_fails_job(fresh_db):
    db = fresh_db
    qid, _, _ = _row(db, "alpha", _fact("Pin chart versions in CI"))
    with db.publisher_tx() as conn:
        jobs.enqueue(conn, "alpha", "gate", str(qid), {"queue_id": qid})
    _set_settings(db, "gamma", {"bogus": 1})  # another workspace: the deny list fails closed
    job = jobs.claim(db, {"gate"})
    rec = Recorder()
    assert gate.handle(db, {}, job, rec.judges, _no_backend) == "settings_invalid"
    assert _ctl(db, "SELECT status, error_category FROM control.jobs WHERE id = %s", (job.id,)) == [
        ("queued", "settings_invalid")
    ]
    assert rec.jev_calls == [] and _queue(db, qid)[0] == "pending"


def test_backend_none_blocks_without_judges(fresh_db):
    db = fresh_db
    _set_settings(db, "alpha", {"backend": "none"})
    qid, _, _ = _row(db, "alpha", _fact("Pin chart versions in CI"))
    rec = Recorder()
    assert gate.run(db, {}, _job("alpha", qid), rec.judges, _no_backend) == "blocked"
    assert rec.jev_calls == [] and rec.llm_calls == []
    st, _, reasons, rewrites, _, _ = _queue(db, qid)
    assert (st, rewrites) == ("blocked", 0)
    assert [r["category"] for r in reasons] == ["judge:none"]


def test_concept_failing_is_blocked_without_rewrite(db):
    t = _tag()
    with db.ws_tx("beta") as conn:
        graph.add_node(conn, "beta", "instance", "cluster", f"teal-gke-{t}")
    qid, _, _ = _row(db, "alpha", _concept(f"teal-gke-{t}"))
    rec = Recorder()
    assert gate.run(db, {}, _job("alpha", qid), rec.judges, _no_backend) == "blocked"
    st, item, reasons, rewrites, _, _ = _queue(db, qid)
    assert (st, rewrites, item["name"]) == ("blocked", 0, f"teal-gke-{t}")
    assert [r["category"] for r in reasons] == ["instance_name"]

    # a judge failure blocks a concept as well
    qid2, _, _ = _row(db, "alpha", _concept(f"oddtool{t}"))
    rec2 = Recorder(llm=False)
    assert gate.run(db, {}, _job("alpha", qid2), rec2.judges, _no_backend) == "blocked"
    assert [r["category"] for r in _queue(db, qid2)[2]] == ["judge:llm"]


def _save(db, ctx, client_session):
    res = save_session(db, ctx, client_session, {"journal": "worked"})
    with db.ws_tx(ctx.workspace) as conn:
        rev = conn.execute(
            f"SELECT id::text FROM ws_{ctx.workspace}.revisions WHERE session_id = %s AND n = %s",
            (res.session_id, res.revision),
        ).fetchone()[0]
    return rev


def test_publish_concept_reapplies_waiting_revision(db, ctx_a):
    t = _tag()
    rev = _save(db, ctx_a, f"g-wait-{t}")
    concept = f"grafanix{t}"
    gen = {
        "nodes": [
            {
                "ref": "n1",
                "kind": "local_concept",
                "type": "tool",
                "name": f"dashy-{t}",
                "aliases": [],
                "concept": concept,
            }
        ],
        "edges": [],
        "facts": [
            {"type": "lesson", "text": f"dashy lesson {t}", "node": "n1", "supersedes": None}
        ],
        "general": {
            "concepts": [{"name": concept, "type": "tool", "aliases": []}],
            "facts": [
                {"type": "lesson", "text": f"Dashboards need owners {t}", "concept": concept}
            ],
        },
        "fact_refs": {},
        "dropped": 0,
    }
    with db.ws_tx("alpha") as conn:
        conn.execute(
            "INSERT INTO ws_alpha.generations (revision_id, output) VALUES (%s, %s)",
            (rev, Jsonb(gen)),
        )
    assert apply.run(db, jobs.Job(0, "alpha", "apply", rev, {"revision_id": rev}, 0, "l")) == "done"
    rows = _ctl(
        db,
        "SELECT id, item->>'kind' FROM control.general_queue WHERE revision_id = %s ORDER BY id",
        (rev,),
    )
    assert [r[1] for r in rows] == ["concept", "fact"]
    (cq, _), (fq, _) = rows
    with db.ws_tx("alpha") as conn:
        assert conn.execute(
            "SELECT count(*) FROM ws_alpha.nodes WHERE name = %s", (f"dashy-{t}",)
        ).fetchone() == (0,)

    rec = Recorder()
    assert gate.run(db, {}, _job("alpha", cq), rec.judges, _no_backend) == "published"
    ((general_id,),) = _general_concept(db, concept)
    assert _queue(db, cq)[4] == general_id
    assert _ctl(
        db,
        "SELECT item_kind, queue_id FROM control.general_origin WHERE item_id = %s",
        (general_id,),
    ) == [("concept", cq)]
    ((job_id, payload, ws),) = _ctl(
        db,
        "SELECT id, payload, workspace FROM control.jobs WHERE kind = 'apply' AND key = %s",
        (f"concept:{cq}",),
    )
    assert (payload, ws) == ({"revision_id": rev}, "alpha")

    # the general fact now attaches to the published concept
    assert gate.run(db, {}, _job("alpha", fq), rec.judges, _no_backend) == "published"
    assert _general_fact(db, _queue(db, fq)[4])[2] == general_id

    # the re-applied revision creates the local concept and attaches the waiting fact
    job = jobs.Job(job_id, "alpha", "apply", f"concept:{cq}", payload, 0, "l")
    assert apply.run(db, job) == "done"
    with db.ws_tx("alpha") as conn:
        ((local, refines),) = conn.execute(
            "SELECT id::text, refines::text FROM ws_alpha.nodes WHERE name = %s", (f"dashy-{t}",)
        ).fetchall()
        facts = conn.execute(
            "SELECT node_id::text, wait_concept FROM ws_alpha.facts WHERE text = %s",
            (f"dashy lesson {t}",),
        ).fetchall()
    assert refines == general_id
    assert facts == [(local, None)]
    # nothing was queued again for the published items
    assert _ctl(
        db, "SELECT count(*) FROM control.general_queue WHERE revision_id = %s", (rev,)
    ) == [(2,)]


def test_publish_concept_reuses_existing(db):
    t = _tag()
    qid, _, _ = _row(db, "alpha", _concept(f"helmlike{t}", aliases=[f"hl{t}"]))
    with db.publisher_tx() as conn:
        existing = graph.add_node(conn, "general", "concept", "tool", f"HL{t}")
    assert gate.run(db, {}, _job("alpha", qid), Recorder().judges, _no_backend) == "published"
    assert _queue(db, qid)[4] == existing
    assert _general_concept(db, f"helmlike{t}") == []
    assert _ctl(db, "SELECT item_kind FROM control.general_origin WHERE queue_id = %s", (qid,)) == [
        ("concept",)
    ]
    assert _ctl(
        db,
        "SELECT count(*) FROM control.jobs WHERE kind = 'apply' AND key = %s",
        (f"concept:{qid}",),
    ) == [(1,)]


def test_rewrite_prompt_has_no_names(db):
    t = _tag()
    with db.ws_tx("beta") as conn:
        graph.add_node(conn, "beta", "instance", "cluster", f"amber-eks-{t}")
        graph.add_node(conn, "beta", "instance", "repo", f"secret-repo-{t}")
    _, other_session, other_rev = _row(db, "alpha", _fact(f"Unrelated queued lesson {t}"))
    text = f"Scale amber-eks-{t} before the batch window"
    qid, session, rev = _row(db, "alpha", _fact(text))
    backend = FakeBackend([_reply("Scale the cluster before the batch window")])
    assert gate.run(db, {}, _job("alpha", qid), Recorder().judges, _factory(backend)) == (
        "rewritten"
    )
    ((messages, alias),) = backend.calls
    assert alias == "default"
    blob = json.dumps(messages, ensure_ascii=False)
    for absent in (
        "alpha",
        "beta",
        f"secret-repo-{t}",
        f"Unrelated queued lesson {t}",
        session,
        rev,
        other_session,
        other_rev,
    ):
        assert absent not in blob
    body = json.loads(messages[-1]["content"])
    start = text.index("amber")
    assert body == {
        "text": text,
        "findings": [
            {
                "category": "instance_name",
                "start": start,
                "end": start + len(f"amber-eks-{t}"),
                "span": f"amber-eks-{t}",
            }
        ],
    }


def test_stale_item_is_noop(db):
    t = _tag()
    qid, _, _ = _row(db, "alpha", _fact(f"Old lesson {t}"), status="stale")
    rec = Recorder()
    assert gate.run(db, {}, _job("alpha", qid), rec.judges, _no_backend) == "stale"
    assert rec.jev_calls == [] and rec.llm_calls == []
    assert _queue(db, qid)[0] == "stale"
    assert gate.run(db, {}, _job("alpha", 987654321), rec.judges, _no_backend) == "stale"


def test_handle_completes_job(fresh_db):
    db = fresh_db
    qid, _, _ = _row(db, "alpha", _fact("Pin chart versions in CI"), status="dropped")
    with db.publisher_tx() as conn:
        jobs.enqueue(conn, "alpha", "gate", str(qid), {"queue_id": qid})
    job = jobs.claim(db, {"gate"})
    assert gate.handle(db, {}, job, Recorder().judges, _no_backend) == "stale"
    assert _ctl(db, "SELECT status FROM control.jobs WHERE id = %s", (job.id,)) == [("done",)]


def test_publish_override_and_closed_rows(db):
    t = _tag()
    qid, _, _ = _row(db, "alpha", _fact(f"Blocked lesson ABC-9 {t}"), status="blocked")
    gid = gate.publish(db, qid, text_override=f"Owner wording {t}", actor="owner")
    st, item, _, _, published_id, _ = _queue(db, qid)
    assert (st, published_id) == ("published", gid)
    assert item["text"] == f"Owner wording {t}"
    assert item["source_text"] == f"Blocked lesson ABC-9 {t}"
    assert _general_fact(db, gid)[1] == f"Owner wording {t}"
    with pytest.raises(InvalidInput):
        gate.publish(db, qid)  # already published
    cq, _, _ = _row(db, "alpha", _concept(f"cname{t}"), status="waiting_review")
    with pytest.raises(InvalidInput):
        gate.publish(db, cq, text_override="other name")
    with pytest.raises(InvalidInput):
        gate.publish(db, _row(db, "alpha", _fact(f"x {t}"))[0], text_override="\x00")


def test_llm_judge_parses_json_and_fails_closed():
    backend = FakeBackend(['{"ok": true}', '```json\n{"ok": false}\n```', '{"ok": "yes"}', "sure"])
    judge = gate.llm_judge(backend, "judge-alias")
    assert judge("Pin chart versions", "fact") is True
    assert judge("Pin chart versions", "fact") is False
    for _ in range(2):
        with pytest.raises(BackendUnavailable):
            judge("Pin chart versions", "fact")
    messages, alias = backend.calls[0]
    assert alias == "judge-alias"
    body = json.loads(messages[-1]["content"])
    assert body == {
        "subject": "fact",
        "question": gate.criteria()["question"],
        "text": "Pin chart versions",
    }
    assert backend.max_tokens == [gate.JUDGE_MAX_TOKENS] * 4


class ClosingBackend(FakeBackend):
    def __init__(self, replies=None):
        super().__init__(replies)
        self.closed = 0

    def close(self):
        self.closed += 1


class CountingFactory:
    def __init__(self, backend):
        self.backend, self.calls = backend, 0

    def __call__(self, env, ws, s):
        self.calls += 1
        return self.backend


@pytest.fixture
def jev_instances(monkeypatch):
    """Record every JevJudge built and whether it was closed."""
    from treant import backends

    made = []
    real = backends.JevJudge

    class Recording(real):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.closed = 0
            made.append(self)

        def close(self):
            self.closed += 1
            super().close()

    monkeypatch.setattr(backends, "JevJudge", Recording)
    return made


@respx.mock
def test_default_judges_use_jev_and_origin_backend(jev_instances):
    route = respx.post("http://jev.test/v1/systemone").mock(
        return_value=httpx.Response(200, json={"answers": {"general": {"noul": 0.9}}})
    )
    backend = ClosingBackend(['{"ok": true}', '{"ok": false}'])
    factory = CountingFactory(backend)
    env = {"TREANT_JEV_URL": "http://jev.test", "TREANT_JEV_API_KEY": "k"}
    from treant.settings import WorkspaceSettings

    with gate.default_judges(env, "alpha", WorkspaceSettings(judge_alias="j"), factory) as judges:
        assert judges.jev("text", "fact") == 0.9 and judges.llm("text", "fact") is True
        assert judges.jev("text", "fact") == 0.9 and judges.llm("text", "fact") is False
    sent = json.loads(route.calls[0].request.content)
    assert sent["state"] == "text"
    assert sent["questions"]["general"]["instructions"] == gate.criteria()["question"]
    assert backend.calls[0][1] == "j"
    # built once per default_judges, closed afterwards
    assert factory.calls == 1 and backend.closed == 1
    assert len(jev_instances) == 1 and jev_instances[0].closed == 1
    with (
        pytest.raises(BackendUnavailable),
        gate.default_judges({}, "alpha", WorkspaceSettings(), factory) as judges,
    ):
        judges.jev("text", "fact")


@respx.mock
def test_run_builds_clients_once_and_closes(db, jev_instances):
    respx.post("http://jev.test/v1/systemone").mock(
        return_value=httpx.Response(200, json={"answers": {"general": {"noul": 0.5}}})
    )
    t = _tag()
    qid, _, _ = _row(db, "alpha", _fact(f"Our team likes small changes {t}"))
    backend = ClosingBackend(['{"ok": true}', _reply(f"Small changes review faster {t}")])
    factory = CountingFactory(backend)
    env = {"TREANT_JEV_URL": "http://jev.test", "TREANT_JEV_API_KEY": "k"}
    assert gate.run(db, env, _job("alpha", qid), None, factory) == "rewritten"
    assert len(backend.calls) == 2  # judge, then rewrite, on one backend
    assert factory.calls == 1 and backend.closed == 1
    assert len(jev_instances) == 1 and jev_instances[0].closed == 1


def test_jev_url_and_model_come_from_config():
    from treant import config

    assert config.jev_endpoint({}) == (config.Settings.jev_url, config.Settings.jev_model)
    env = {"TREANT_JEV_URL": "http://j", "TREANT_JEV_MODEL": "m"}
    assert config.jev_endpoint(env) == ("http://j", "m")


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -0.1, 1.5, True])
def test_bad_jev_score_fails_closed(db, score):
    t = _tag()
    qid, _, _ = _row(db, "alpha", _fact(f"Scored lesson {t}"))
    rec = Recorder(jev=score)
    with pytest.raises(gate.Retry) as exc:
        gate.run(db, {}, _job("alpha", qid), rec.judges, _no_backend)
    assert exc.value.category == "judge_unavailable"
    assert _queue(db, qid)[0] == "pending"


def test_same_concept_published_twice_is_one_node(db):
    t = _tag()
    q1, _, _ = _row(db, "alpha", _concept(f"twinkle{t}"))
    q2, _, _ = _row(db, "beta", _concept(f"TWINKLE{t}"))
    g1 = gate.publish(db, q1, actor="owner")
    g2 = gate.publish(db, q2, actor="owner")
    assert g1 == g2
    with db.publisher_tx() as conn:
        assert conn.execute(
            "SELECT count(*) FROM general.nodes WHERE lower(name) = %s", (f"twinkle{t}",)
        ).fetchone() == (1,)
    assert _ctl(
        db,
        "SELECT queue_id FROM control.general_origin WHERE item_id = %s ORDER BY queue_id",
        (g1,),
    ) == [(q1,), (q2,)]


def test_publish_waits_on_concept_lock(db):
    import threading

    from treant.reconcile import normalise

    t = _tag()
    qid, _, _ = _row(db, "alpha", _concept(f"Locky{t}"))
    done = threading.Event()
    with db.publisher_tx() as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            ("treant.concept:" + normalise(f"Locky{t}"),),
        )
        worker = threading.Thread(target=lambda: (gate.publish(db, qid, actor="owner"), done.set()))
        worker.start()
        assert not done.wait(0.5)  # blocked on the lock held here
    worker.join(5)
    assert done.is_set() and _queue(db, qid)[0] == "published"


def test_existing_concept_published_without_judging(db):
    t = _tag()
    with db.publisher_tx() as conn:
        existing = graph.add_node(conn, "general", "concept", "tool", f"knowntool{t}")
    with db.ws_tx("beta") as conn:  # even a name the rules would flag: no new text enters
        graph.add_node(conn, "beta", "instance", "cluster", f"knowntool{t}")
    qid, _, rev = _row(db, "alpha", _concept(f"KnownTool{t}"))
    rec = Recorder(jev=0.0, llm=False)
    assert gate.run(db, {}, _job("alpha", qid), rec.judges, _no_backend) == "published"
    assert rec.jev_calls == [] and rec.llm_calls == []
    assert _queue(db, qid)[4] == existing
    assert _ctl(db, "SELECT item_kind FROM control.general_origin WHERE queue_id = %s", (qid,)) == [
        ("concept",)
    ]
    assert _ctl(
        db,
        "SELECT payload FROM control.jobs WHERE kind = 'apply' AND key = %s",
        (f"concept:{qid}",),
    ) == [({"revision_id": rev},)]


def test_existing_concept_published_when_backend_none(fresh_db):
    db = fresh_db
    _set_settings(db, "alpha", {"backend": "none", "publish": "review"})
    with db.publisher_tx() as conn:
        existing = graph.add_node(conn, "general", "concept", "tool", "kubectl", aliases=["kc"])
    qid, _, _ = _row(db, "alpha", _concept("KC"))
    rec = Recorder()
    assert gate.run(db, {}, _job("alpha", qid), rec.judges, _no_backend) == "published"
    assert rec.jev_calls == [] and _queue(db, qid)[4] == existing
    assert _ctl(
        db,
        "SELECT count(*) FROM control.jobs WHERE kind = 'apply' AND key = %s",
        (f"concept:{qid}",),
    ) == [(1,)]


def test_two_gate_jobs_on_one_row_publish_once(db):
    t = _tag()
    text = f"Double gated lesson {t}"
    qid, _, _ = _row(db, "alpha", _fact(text))
    first = Recorder()
    statuses = []

    def llm(_text, _kind):  # the second job finishes while the first is still judging
        statuses.append(
            gate.run(db, {}, _job("alpha", qid, f"{qid}:dup"), first.judges, _no_backend)
        )
        return True

    slow = gate.Judges(jev=lambda _t, _k: 0.99, llm=llm)
    statuses.append(gate.run(db, {}, _job("alpha", qid), slow, _no_backend))
    assert statuses == ["published", "stale"]
    assert _ctl(db, "SELECT count(*) FROM control.general_origin WHERE queue_id = %s", (qid,)) == [
        (1,)
    ]
    with db.publisher_tx() as conn:
        assert conn.execute(
            "SELECT count(*) FROM general.facts WHERE text = %s", (text,)
        ).fetchone() == (1,)


def test_criteria_values():
    c = gate.criteria()
    assert c["question"] == (
        "Is this free of anything specific to an employer, project, customer or person?"
    )
    assert c["jev_threshold"] == 0.8
    assert c["concept_question"] == (
        "Is this the name of a widely known, generic technology, tool or idea, not something "
        "named or built inside one organisation, project or team?"
    )


def test_row_changed_during_judging_is_stale(db):
    t = _tag()
    qid, _, _ = _row(db, "alpha", _fact(f"Racing lesson {t}"))

    def llm(text, kind):  # another job rewrote the row while the judges ran
        with db.publisher_tx() as conn:
            conn.execute("UPDATE control.general_queue SET rewrites = 1 WHERE id = %s", (qid,))
        return True

    judges = gate.Judges(jev=lambda text, kind: 0.99, llm=llm)
    assert gate.run(db, {}, _job("alpha", qid), judges, _no_backend) == "stale"
    st, _, _, rewrites, published_id, _ = _queue(db, qid)
    assert (st, rewrites, published_id) == ("pending", 1, None)
    assert _ctl(db, "SELECT count(*) FROM control.general_origin WHERE queue_id = %s", (qid,)) == [
        (0,)
    ]


def test_reuse_only_publish_never_creates_a_concept(db):
    t = _tag()
    qid, _, _ = _row(db, "alpha", _concept(f"ghosttool{t}"))
    with pytest.raises(gate._NotExisting):
        gate._publish(db, qid, None, "gate", expect=("pending", 0), reuse_only=True)
    assert _general_concept(db, f"ghosttool{t}") == []
    assert _queue(db, qid)[0] == "pending"
    assert _ctl(db, "SELECT count(*) FROM control.general_origin WHERE queue_id = %s", (qid,)) == [
        (0,)
    ]


# --- final review fixes: concept question, judge order, bad replies -------------------------


@respx.mock
@pytest.mark.parametrize("kind", ["fact", "concept"])
def test_judges_get_the_question_for_the_item_kind(db, kind):
    t = _tag()
    jev = respx.post("http://jev.test/v1/systemone").mock(
        return_value=httpx.Response(200, json={"answers": {"general": {"noul": 0.95}}})
    )
    item = _fact(f"Pin chart versions {t}") if kind == "fact" else _concept(f"quasitool{t}")
    qid, _, _ = _row(db, "alpha", item)
    backend = FakeBackend(['{"ok": true}'])
    env = {"TREANT_JEV_URL": "http://jev.test", "TREANT_JEV_API_KEY": "k"}
    assert gate.run(db, env, _job("alpha", qid), None, _factory(backend)) == "published"
    c = gate.criteria()
    want = c["concept_question"] if kind == "concept" else c["question"]
    other = c["question"] if kind == "concept" else c["concept_question"]
    ((messages, _),) = backend.calls
    body = json.loads(messages[-1]["content"])
    assert body["question"] == want and body["subject"] == kind
    system = messages[0]["content"]
    assert "concept" in system and "know-how" in system  # the prompt says what it judges
    sent = json.loads(jev.calls[0].request.content)
    assert sent["questions"]["general"]["instructions"] == want
    assert other not in json.dumps(sent) and other not in json.dumps(messages)


def test_injected_judges_get_the_item_kind(db):
    t = _tag()
    qid, _, _ = _row(db, "alpha", _concept(f"kindtool{t}"))
    rec = Recorder()
    assert gate.run(db, {}, _job("alpha", qid), rec.judges, _no_backend) == "published"
    assert rec.kinds == [("llm", "concept"), ("jev", "concept")]  # the LLM is asked first
    qid2, _, _ = _row(db, "alpha", _fact(f"Kind lesson {t}"))
    rec2 = Recorder()
    assert gate.run(db, {}, _job("alpha", qid2), rec2.judges, _no_backend) == "published"
    assert rec2.kinds == [("llm", "fact"), ("jev", "fact")]


def test_llm_judge_extracts_the_first_object():
    backend = FakeBackend(
        [
            'Sure. {"ok": true} Hope that helps.',
            'Answer: {"ok": false}. Also {"ok": true}',
            'The text "a {b}" is fine: {"ok": true}',
            '{not json} then {"ok": false}',
            "no object here",
            '{"ok": "yes"} and {"ok": true}',
            '{"verdict": true}',
        ]
    )
    judge = gate.llm_judge(backend, "j")
    assert judge("t", "fact") is True
    assert judge("t", "fact") is False
    assert judge("t", "fact") is True
    assert judge("t", "fact") is False
    for _ in range(3):
        with pytest.raises(BackendBadReply):
            judge("t", "fact")


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (BackendBadReply("bad"), "judge_bad_reply"),
        (BackendRejected("rejected"), "judge_rejected"),
    ],
)
def test_deterministic_judge_failures_die_after_three(fresh_db, error, category):
    db = fresh_db
    qid, _, _ = _row(db, "alpha", _fact("Pin chart versions in CI"))
    with db.publisher_tx() as conn:
        jobs.enqueue(conn, "alpha", "gate", str(qid), {"queue_id": qid})
    states = []
    for _ in range(3):
        with db.control_tx() as conn:
            conn.execute("UPDATE control.jobs SET run_after = now() WHERE kind = 'gate'")
        job = jobs.claim(db, {"gate"})
        rec = Recorder(llm=error)
        assert gate.handle(db, {}, job, rec.judges, _no_backend) == category
        assert rec.jev_calls == []  # Jev is never paid for when the LLM did not say yes
        states += _ctl(db, "SELECT status, attempts FROM control.jobs WHERE id = %s", (job.id,))
    assert states == [("queued", 1), ("queued", 2), ("dead", 3)]
    assert _queue(db, qid)[0] == "pending"


def test_llm_bad_reply_from_backend_is_judge_bad_reply(db):
    t = _tag()
    qid, _, _ = _row(db, "alpha", _fact(f"Bad reply lesson {t}"))
    backend = FakeBackend(["I think it is fine"])
    with pytest.raises(gate.Retry) as exc:
        gate.run(db, {}, _job("alpha", qid), None, _factory(backend))
    assert exc.value.category == "judge_bad_reply"


def test_empty_rewrite_reply_counts_as_a_bad_rewrite(db):
    t = _tag()
    qid, _, _ = _row(db, "alpha", _fact(f"Ticket ABC-123 lesson {t}"))
    backend = FakeBackend([BackendBadReply("empty reply")])
    assert gate.run(db, {}, _job("alpha", qid), Recorder().judges, _factory(backend)) == (
        "rewritten"
    )
    st, item, _, rewrites, _, _ = _queue(db, qid)
    assert (st, rewrites, item["text"]) == ("pending", 1, f"Ticket ABC-123 lesson {t}")
    assert backend.max_tokens == [gate.REWRITE_MAX_TOKENS]
