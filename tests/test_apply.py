import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from treant import apply, graph, jobs
from treant.sessions import save_session


def _tag() -> str:
    return uuid.uuid4().hex[:8]


def _save(db, ctx, client_session, text="worked"):
    res = save_session(db, ctx, client_session, {"journal": text})
    with db.ws_tx(ctx.workspace) as conn:
        rev = conn.execute(
            f"SELECT id::text FROM ws_{ctx.workspace}.revisions WHERE session_id = %s AND n = %s",
            (res.session_id, res.revision),
        ).fetchone()[0]
    return res.session_id, rev


def _gen(nodes=(), edges=(), facts=(), concepts=(), gfacts=(), fact_refs=None):
    return {
        "nodes": [{"aliases": [], **n} for n in nodes],
        "edges": list(edges),
        "facts": [{"node": None, "supersedes": None, **f} for f in facts],
        "general": {
            "concepts": [{"aliases": [], **c} for c in concepts],
            "facts": list(gfacts),
        },
        "fact_refs": fact_refs or {},
        "dropped": 0,
    }


def _store(db, ws, rev, gen):
    with db.ws_tx(ws) as conn:
        conn.execute(
            f"INSERT INTO ws_{ws}.generations (revision_id, output) VALUES (%s, %s) "
            "ON CONFLICT (revision_id) DO UPDATE SET output = EXCLUDED.output",
            (rev, Jsonb(gen)),
        )


def _job(ws, rev, key=None):
    return jobs.Job(0, ws, "apply", key or rev, {"revision_id": rev}, 0, "lease")


def _q(db, query, params=(), ws="alpha"):
    with db.ws_tx(ws) as conn:
        return conn.execute(query, params).fetchall()


def _ctl(db, query, params=()):
    with db.control_tx() as conn:
        return conn.execute(query, params).fetchall()


def _support(db, rev, ws="alpha"):
    rows = _q(
        db,
        f"SELECT item_kind, count(*) FROM ws_{ws}.support WHERE revision_id = %s GROUP BY 1",
        (rev,),
        ws,
    )
    return dict(rows)


def _node(db, name, ws="alpha"):
    return _q(
        db,
        f"SELECT id::text, kind, status, refines::text FROM ws_{ws}.nodes WHERE name = %s",
        (name,),
        ws,
    )


def _facts(db, text_like, ws="alpha"):
    return _q(
        db,
        f"SELECT id::text, text, node_id::text, status, version, wait_concept, revision_id::text "
        f"FROM ws_{ws}.facts WHERE text LIKE %s ORDER BY created_at",
        (text_like,),
        ws,
    )


def _publish(db, session, rev, source, published=None, ws="alpha"):
    """Simulate Task 7 publishing a fact: queue row (source_text fixed), general fact with the
    possibly rewritten text, origin row pointing at the queue row. Returns (fact id, queue id)."""
    published = published or source
    item = {"kind": "fact", "type": "lesson", "text": published, "source_text": source}
    with db.publisher_tx() as conn:
        qid = conn.execute(
            "INSERT INTO control.general_queue "
            "(workspace, session_id, revision_id, item, item_key, status) "
            "VALUES (%s, %s, %s, %s, %s, 'published') "
            "ON CONFLICT (workspace, revision_id, item_key) DO UPDATE SET item = EXCLUDED.item, "
            "status = 'published' RETURNING id",
            (ws, session, rev, Jsonb(item), apply.item_key("fact", source)),
        ).fetchone()[0]
        fid = graph.add_fact(conn, "general", "lesson", published)
        conn.execute("UPDATE control.general_queue SET published_id = %s WHERE id = %s", (fid, qid))
        conn.execute(
            "INSERT INTO control.general_origin "
            "(item_kind, item_id, workspace, session_id, revision_id, queue_id) "
            "VALUES ('fact', %s, %s, %s, %s, %s)",
            (fid, ws, session, rev, qid),
        )
    return fid, qid


def _general_status(db, ids):
    with db.publisher_tx() as conn:
        return dict(
            conn.execute(
                "SELECT id::text, status FROM general.facts WHERE id = ANY(%s)", (list(ids),)
            ).fetchall()
        )


def _snapshot_counts(db, t, ws="alpha"):
    s = f"ws_{ws}"
    return _q(
        db,
        f"SELECT (SELECT count(*) FROM {s}.nodes WHERE type LIKE %(t)s), "
        f"(SELECT count(*) FROM {s}.edges e JOIN {s}.nodes n ON n.id = e.src "
        f" WHERE n.type LIKE %(t)s), "
        f"(SELECT count(*) FROM {s}.facts WHERE text LIKE %(t)s), "
        f"(SELECT count(*) FROM {s}.versions), (SELECT count(*) FROM {s}.merge_flags), "
        f"(SELECT count(*) FROM {s}.support)",
        {"t": f"%{t}%"},
        ws,
    )[0]


def test_apply_creates_nodes_edges_facts_with_support(db, ctx_a):
    t = _tag()
    _, rev = _save(db, ctx_a, f"a-create-{t}")
    gen = _gen(
        nodes=[
            {"ref": "n1", "kind": "instance", "type": f"cluster{t}", "name": f"eks-{t}"},
            {"ref": "n2", "kind": "instance", "type": f"service{t}", "name": f"api-{t}"},
        ],
        edges=[{"src": "n2", "dst": "n1", "type": "runs_on"}],
        facts=[
            {"type": "lesson", "text": f"pin the chart {t}", "node": "n1"},
            {"type": "fact", "text": f"loose note {t}"},
        ],
    )
    _store(db, "alpha", rev, gen)
    assert apply.run(db, _job("alpha", rev)) == "done"
    ((n1, kind, status, _),) = _node(db, f"eks-{t}")
    ((n2, _, _, _),) = _node(db, f"api-{t}")
    assert (kind, status) == ("instance", "current")
    edges = _q(db, "SELECT src::text, dst::text, type FROM ws_alpha.edges WHERE src = %s", (n2,))
    assert edges == [(n2, n1, "runs_on")]
    facts = {f[1]: f for f in _facts(db, f"%{t}")}
    assert facts[f"pin the chart {t}"][2] == n1
    assert facts[f"loose note {t}"][2] is None
    assert all(f[6] == rev for f in facts.values())
    assert _support(db, rev) == {"node": 2, "edge": 1, "fact": 2}
    payload = _ctl(
        db,
        "SELECT payload FROM control.jobs WHERE kind = 'embed' AND key = %s",
        (f"apply:{rev}",),
    )
    assert payload == [({"revision_id": None},)]


def test_apply_is_idempotent(db, ctx_a):
    t = _tag()
    _, rev = _save(db, ctx_a, f"a-idem-{t}")
    gen = _gen(
        nodes=[
            {"ref": "n1", "kind": "instance", "type": f"cluster{t}", "name": f"eks-{t}"},
            {"ref": "n2", "kind": "instance", "type": f"cluster{t}", "name": f"eks-{t}-b"},
        ],
        edges=[{"src": "n1", "dst": "n2", "type": "depends_on"}],
        facts=[
            {"type": "lesson", "text": f"pin the chart {t}", "node": "n1"},
            {"type": "lesson", "text": f"pin the chart {t}", "node": "n1"},
            {"type": "fact", "text": f"loose note {t}"},
        ],
        concepts=[{"name": f"helmish{t}", "type": "tool"}],
        gfacts=[{"type": "lesson", "text": f"pin chart versions {t}", "concept": f"helmish{t}"}],
    )
    _store(db, "alpha", rev, gen)
    assert apply.run(db, _job("alpha", rev)) == "done"
    first = _snapshot_counts(db, t)
    queue = _ctl(db, "SELECT count(*) FROM control.general_queue WHERE revision_id = %s", (rev,))
    assert apply.run(db, _job("alpha", rev)) == "done"
    assert _snapshot_counts(db, t) == first
    assert first[:3] == (2, 1, 2)  # the two identical facts are one fact
    assert (
        _ctl(db, "SELECT count(*) FROM control.general_queue WHERE revision_id = %s", (rev,))
        == queue
        == [(2,)]
    )


def test_apply_resolves_existing_node_by_alias(db, ctx_a):
    t = _tag()
    with db.ws_tx("alpha") as conn:
        existing = graph.add_node(
            conn, "alpha", "instance", f"cluster{t}", f"Prod-EKS-{t}", aliases=[f"pe{t}"]
        )
        assert apply.resolve_node(conn, "alpha", "instance", f"cluster{t}", f"PE{t}", []) == (
            existing,
            False,
        )
    _, rev = _save(db, ctx_a, f"a-alias-{t}")
    gen = _gen(
        nodes=[{"ref": "n1", "kind": "instance", "type": f"cluster{t}", "name": f"PE{t}"}],
        facts=[{"type": "fact", "text": f"alias fact {t}", "node": "n1"}],
    )
    _store(db, "alpha", rev, gen)
    assert apply.run(db, _job("alpha", rev)) == "done"
    assert _node(db, f"PE{t}") == []
    assert _facts(db, f"alias fact {t}")[0][2] == existing
    assert _q(
        db,
        "SELECT count(*) FROM ws_alpha.support WHERE item_id = %s AND revision_id = %s",
        (existing, rev),
    ) == [(1,)]


def test_apply_near_match_flags_merge(db, ctx_a):
    t = _tag()
    with db.ws_tx("alpha") as conn:
        existing = graph.add_node(conn, "alpha", "instance", f"svc{t}", f"payments-gateway-{t}")
        far = graph.add_node(conn, "alpha", "instance", f"svc{t}", f"zz-unrelated-{t}")
    _, rev = _save(db, ctx_a, f"a-near-{t}")
    gen = _gen(
        nodes=[{"ref": "n1", "kind": "instance", "type": f"svc{t}", "name": f"payments-gatway-{t}"}]
    )
    _store(db, "alpha", rev, gen)
    assert apply.run(db, _job("alpha", rev)) == "done"
    ((new, _, _, _),) = _node(db, f"payments-gatway-{t}")
    assert new != existing
    flags = _q(
        db,
        "SELECT node_a::text, node_b::text, score FROM ws_alpha.merge_flags WHERE node_b = %s",
        (new,),
    )
    assert [(a, b) for a, b, _ in flags] == [(existing, new)]
    assert flags[0][2] >= 0.85
    assert far not in {a for a, _, _ in flags}


def test_local_concept_refines_existing_general_concept(db, ctx_a):
    t = _tag()
    with db.publisher_tx() as conn:
        concept = graph.add_node(
            conn, "general", "concept", "tool", f"signoz{t}", aliases=[f"sz{t}"]
        )
    _, rev = _save(db, ctx_a, f"a-refine-{t}")
    gen = _gen(
        nodes=[
            {
                "ref": "n1",
                "kind": "local_concept",
                "type": "tool",
                "name": f"obs-{t}",
                "concept": f"SZ{t}",
            }
        ],
        facts=[{"type": "lesson", "text": f"obs lesson {t}", "node": "n1"}],
        concepts=[{"name": f"signoz{t}", "type": "tool"}],
    )
    _store(db, "alpha", rev, gen)
    assert apply.run(db, _job("alpha", rev)) == "done"
    ((node, kind, _, refines),) = _node(db, f"obs-{t}")
    assert (kind, refines) == ("local_concept", concept)
    assert _facts(db, f"obs lesson {t}")[0][2] == node
    queued = _ctl(db, "SELECT item FROM control.general_queue WHERE revision_id = %s", (rev,))
    assert queued == []  # the concept already exists in general


def test_local_concept_waits_for_missing_concept(db, ctx_a):
    t = _tag()
    _, rev = _save(db, ctx_a, f"a-wait-{t}")
    concept = f"grafanox{t}"
    gen = _gen(
        nodes=[
            {
                "ref": "n1",
                "kind": "local_concept",
                "type": "tool",
                "name": f"dash-{t}",
                "concept": concept,
            },
            {"ref": "n2", "kind": "instance", "type": f"cluster{t}", "name": f"eks-{t}"},
        ],
        edges=[{"src": "n1", "dst": "n2", "type": "runs_on"}],
        facts=[{"type": "lesson", "text": f"dash lesson {t}", "node": "n1"}],
    )
    _store(db, "alpha", rev, gen)
    assert apply.run(db, _job("alpha", rev)) == "done"
    assert _node(db, f"dash-{t}") == []
    ((fid, _, node_id, _, _, wait, _),) = _facts(db, f"dash lesson {t}")
    assert (node_id, wait) == (None, concept)
    ((n2, _, _, _),) = _node(db, f"eks-{t}")
    assert _q(db, "SELECT count(*) FROM ws_alpha.edges WHERE dst = %s", (n2,)) == [(0,)]
    rows = _ctl(
        db, "SELECT id, item, status FROM control.general_queue WHERE revision_id = %s", (rev,)
    )
    assert [(r[1]["kind"], r[1]["name"], r[2]) for r in rows] == [("concept", concept, "pending")]
    qid = rows[0][0]

    # The gate publishes the concept and re-runs apply for the waiting revision.
    with db.publisher_tx() as conn:
        general_id = graph.add_node(conn, "general", "concept", "tool", concept)
    assert apply.run(db, _job("alpha", rev, key=f"concept:{qid}")) == "done"
    ((local, kind, _, refines),) = _node(db, f"dash-{t}")
    assert (kind, refines) == ("local_concept", general_id)
    facts = _facts(db, f"dash lesson {t}")
    assert [(f[0], f[2], f[5]) for f in facts] == [(fid, local, None)]
    assert _q(db, "SELECT count(*) FROM ws_alpha.edges WHERE dst = %s", (n2,)) == [(1,)]
    assert _ctl(
        db, "SELECT count(*) FROM control.general_queue WHERE revision_id = %s", (rev,)
    ) == [(1,)]


def test_newer_revision_keeps_unproduced_items(db, ctx_a):
    t = _tag()
    sid = f"a-newer-{t}"
    _, rev1 = _save(db, ctx_a, sid, "first")
    a = {"ref": "a", "kind": "instance", "type": f"cluster{t}", "name": f"keep-{t}"}
    b = {"ref": "b", "kind": "instance", "type": f"cluster{t}", "name": f"gone-{t}"}
    _store(
        db,
        "alpha",
        rev1,
        _gen(
            nodes=[a, b],
            edges=[{"src": "a", "dst": "b", "type": "uses"}],
            facts=[
                {"type": "lesson", "text": f"always pin the helm chart {t}", "node": "a"},
                {"type": "fact", "text": f"gone fact {t}", "node": "b"},
            ],
        ),
    )
    assert apply.run(db, _job("alpha", rev1)) == "done"
    ((keep_fact, *_),) = _facts(db, f"always pin%{t}")
    ((gone_fact, *_),) = _facts(db, f"gone fact {t}")
    ((gone_node, *_),) = _node(db, f"gone-{t}")

    _, rev2 = _save(db, ctx_a, sid, "second")
    _store(
        db,
        "alpha",
        rev2,
        _gen(
            nodes=[a],
            facts=[{"type": "lesson", "text": f"always pin the helm charts {t}", "node": "a"}],
        ),
    )
    assert apply.run(db, _job("alpha", rev2)) == "done"
    ((fid, text, _, status, version, _, _),) = _facts(db, f"always pin%{t}")
    assert (fid, text, status, version) == (
        keep_fact,
        f"always pin the helm charts {t}",
        "current",
        2,
    )
    # a save is additive: what the later revision does not mention stays, with its support
    assert _facts(db, f"gone fact {t}")[0][3] == "current"
    assert _node(db, f"gone-{t}")[0][2] == "current"
    assert _node(db, f"keep-{t}")[0][2] == "current"
    edge = _q(db, "SELECT status FROM ws_alpha.edges WHERE dst = %s", (gone_node,))
    assert edge == [("current",)]
    reasons = _q(
        db,
        "SELECT reason FROM ws_alpha.versions WHERE item_id = %s AND item_kind = 'fact'",
        (gone_fact,),
    )
    assert reasons == []
    assert _support(db, rev1) == {"node": 2, "edge": 1, "fact": 2}


def _last_reason(db, fact_id, ws="alpha"):
    rows = _q(
        db,
        f"SELECT reason FROM ws_{ws}.versions WHERE item_kind = 'fact' AND item_id = %s "
        "ORDER BY version DESC LIMIT 1",
        (fact_id,),
        ws,
    )
    return rows[0][0] if rows else None


def test_newer_revision_replaces_open_threads_only(db, ctx_a):
    t = _tag()
    sid = f"a-threads-{t}"
    _, rev1 = _save(db, ctx_a, sid, "first")
    _store(
        db,
        "alpha",
        rev1,
        _gen(
            facts=[
                {"type": "open_thread", "text": f"finish the migration {t}"},
                {"type": "decision", "text": f"use postgres {t}"},
            ]
        ),
    )
    assert apply.run(db, _job("alpha", rev1)) == "done"
    _, rev2 = _save(db, ctx_a, sid, "second")
    _store(
        db,
        "alpha",
        rev2,
        _gen(facts=[{"type": "open_thread", "text": f"write the runbook {t}"}]),
    )
    assert apply.run(db, _job("alpha", rev2)) == "done"
    ((old_thread, _, _, status, *_),) = _facts(db, f"finish the migration {t}")
    assert status == "superseded" and _last_reason(db, old_thread) == "reconcile"
    assert _facts(db, f"use postgres {t}")[0][3] == "current"
    assert _facts(db, f"write the runbook {t}")[0][3] == "current"


def test_older_revision_applied_late_supersedes_nothing(db, ctx_a):
    t = _tag()
    sid = f"a-late-{t}"
    _, rev1 = _save(db, ctx_a, sid, "first")
    _store(
        db,
        "alpha",
        rev1,
        _gen(
            facts=[
                {"type": "open_thread", "text": f"migrate the cluster {t}"},
                {"type": "lesson", "text": f"older lesson {t}"},
            ]
        ),
    )
    _, rev2 = _save(db, ctx_a, sid, "second")
    _store(
        db,
        "alpha",
        rev2,
        _gen(facts=[{"type": "open_thread", "text": f"write up the incident {t}"}]),
    )
    assert apply.run(db, _job("alpha", rev2)) == "done"
    assert apply.run(db, _job("alpha", rev1)) == "done"  # not skipped: every revision applies
    assert _facts(db, f"write up the incident {t}")[0][3] == "current"
    assert _facts(db, f"older lesson {t}")[0][3] == "current"
    # the newer save already said what is open: the late revision opens no thread
    assert _facts(db, f"migrate the cluster {t}") == []


def test_older_revision_applied_late_changes_no_text(db, ctx_a, ctx_a2):
    t = _tag()
    sid = f"a-revert-{t}"
    _, rev1 = _save(db, ctx_a, sid, "first")
    _store(
        db,
        "alpha",
        rev1,
        _gen(facts=[{"type": "fact", "text": f"the api runs with 3 replicas {t}"}]),
    )
    assert apply.run(db, _job("alpha", rev1)) == "done"
    ((old_id, *_),) = _facts(db, f"the api runs with%{t}")
    _, other = _save(db, ctx_a2, f"a-revert-other-{t}")
    _store(db, "alpha", other, _gen(facts=[{"type": "fact", "text": f"unrelated note {t}"}]))
    assert apply.run(db, _job("alpha", other)) == "done"
    ((victim, *_),) = _facts(db, f"unrelated note {t}")
    _, rev2 = _save(db, ctx_a, sid, "second")
    _store(
        db,
        "alpha",
        rev2,
        _gen(facts=[{"type": "fact", "text": f"the api runs with 5 replicas {t}"}]),
    )
    assert apply.run(db, _job("alpha", rev2)) == "done"
    # rev1 runs again (a retry, or its concept was published): with a supersedes reference too
    _store(
        db,
        "alpha",
        rev1,
        _gen(
            facts=[
                {"type": "fact", "text": f"the api runs with 3 replicas {t}", "supersedes": "f1"}
            ],
            fact_refs={"f1": victim},
        ),
    )
    assert apply.run(db, _job("alpha", rev1)) == "done"
    ((fid, text, _, status, *_),) = _facts(db, f"the api runs with%{t}")
    assert (fid, text, status) == (old_id, f"the api runs with 5 replicas {t}", "current")
    assert _facts(db, f"unrelated note {t}")[0][3] == "current"  # no supersede by reference


def test_rerun_does_not_recreate_retracted_or_superseded_facts(db, ctx_a):
    t = _tag()
    sid = f"a-gone-{t}"
    _, rev1 = _save(db, ctx_a, sid, "first")
    gen1 = _gen(
        facts=[
            {"type": "lesson", "text": f"a claim the owner retracts {t}"},
            {"type": "open_thread", "text": f"a thread that gets closed {t}"},
            {"type": "lesson", "text": f"a lesson that stays {t}"},
        ]
    )
    _store(db, "alpha", rev1, gen1)
    assert apply.run(db, _job("alpha", rev1)) == "done"
    ((retracted, *_),) = _facts(db, f"a claim the owner retracts {t}")
    with db.ws_tx("alpha") as conn:
        graph.retract(conn, "alpha", "fact", retracted)
    assert apply.run(db, _job("alpha", rev1)) == "done"  # re-run while still the newest
    assert [r[3] for r in _facts(db, f"a claim the owner retracts {t}")] == ["retracted"]

    _, rev2 = _save(db, ctx_a, sid, "second")
    _store(db, "alpha", rev2, _gen())
    assert apply.run(db, _job("alpha", rev2)) == "done"  # supersedes the open thread
    assert apply.run(db, _job("alpha", rev1)) == "done"  # late re-run of the older revision
    assert [r[3] for r in _facts(db, f"a claim the owner retracts {t}")] == ["retracted"]
    assert [r[3] for r in _facts(db, f"a thread that gets closed {t}")] == ["superseded"]
    assert [r[3] for r in _facts(db, f"a lesson that stays {t}")] == ["current"]


def test_item_supported_by_other_session_survives(db, ctx_a, ctx_a2):
    t = _tag()
    node = {"ref": "x", "kind": "instance", "type": f"cluster{t}", "name": f"shared-{t}"}
    fact = {"type": "open_thread", "text": f"shared fact {t}", "node": "x"}
    _, s1r1 = _save(db, ctx_a, f"a-s1-{t}")
    _store(db, "alpha", s1r1, _gen(nodes=[node], facts=[fact]))
    assert apply.run(db, _job("alpha", s1r1)) == "done"
    _, s2r1 = _save(db, ctx_a2, f"a-s2-{t}")
    _store(db, "alpha", s2r1, _gen(nodes=[node], facts=[fact]))
    assert apply.run(db, _job("alpha", s2r1)) == "done"
    assert len(_node(db, f"shared-{t}")) == 1 and len(_facts(db, f"shared fact {t}")) == 1

    _, s1r2 = _save(db, ctx_a, f"a-s1-{t}", "second")
    _store(db, "alpha", s1r2, _gen())
    assert apply.run(db, _job("alpha", s1r2)) == "done"
    assert _node(db, f"shared-{t}")[0][2] == "current"
    assert _facts(db, f"shared fact {t}")[0][3] == "current"
    assert _support(db, s1r1) == {"node": 1}  # only the open thread loses this session's support
    assert _support(db, s2r1) == {"node": 1, "fact": 1}


def test_supersedes_ref_marks_conflict(db, ctx_a, ctx_a2):
    t = _tag()
    node = {"ref": "n1", "kind": "instance", "type": f"cluster{t}", "name": f"gitops-{t}"}
    _, other = _save(db, ctx_a2, f"a-conf-other-{t}")
    _store(
        db,
        "alpha",
        other,
        _gen(
            nodes=[node],
            facts=[{"type": "decision", "text": f"deploy with flux {t}", "node": "n1"}],
        ),
    )
    assert apply.run(db, _job("alpha", other)) == "done"
    ((old, *_),) = _facts(db, f"deploy with flux {t}")

    sid = f"a-conf-{t}"
    _, rev = _save(db, ctx_a, sid)
    _store(
        db,
        "alpha",
        rev,
        _gen(
            nodes=[node],
            facts=[
                {
                    "type": "decision",
                    "text": f"deploy with argo {t}",
                    "node": "n1",
                    "supersedes": "f1",
                }
            ],
            fact_refs={"f1": old},
        ),
    )
    assert apply.run(db, _job("alpha", rev)) == "done"
    ((new, _, _, status, *_),) = _facts(db, f"deploy with argo {t}")
    assert new != old and status == "current"
    assert _facts(db, f"deploy with flux {t}")[0][3] == "superseded"
    conflicts = _q(
        db,
        "SELECT new_fact::text, old_fact::text FROM ws_alpha.conflicts WHERE old_fact = %s",
        (old,),
    )
    assert conflicts == [(new, old)]

    # Superseding a fact of the same session is not a conflict.
    _, rev2 = _save(db, ctx_a, sid, "second")
    _store(
        db,
        "alpha",
        rev2,
        _gen(
            nodes=[node],
            facts=[
                {"type": "decision", "text": f"deploy with kustomize {t}", "node": "n1",
                 "supersedes": "f1"}
            ],
            fact_refs={"f1": new},
        ),
    )  # fmt: skip
    assert apply.run(db, _job("alpha", rev2)) == "done"
    assert _facts(db, f"deploy with argo {t}")[0][3] == "superseded"
    assert _q(db, "SELECT count(*) FROM ws_alpha.conflicts WHERE old_fact = %s", (new,)) == [(0,)]


def test_apply_keeps_result_of_older_revision(db, ctx_a):
    t = _tag()
    sid = f"a-old-{t}"
    _, rev1 = _save(db, ctx_a, sid, "first")
    _store(
        db,
        "alpha",
        rev1,
        _gen(
            nodes=[{"ref": "n1", "kind": "instance", "type": f"cluster{t}", "name": f"old-{t}"}],
            facts=[{"type": "fact", "text": f"old fact {t}", "node": "n1"}],
            concepts=[{"name": f"oldconcept{t}", "type": "tool"}],
            gfacts=[{"type": "lesson", "text": f"old general {t}", "concept": f"oldconcept{t}"}],
        ),
    )
    _save(db, ctx_a, sid, "second")  # stored while the first one's model call was in flight
    assert apply.run(db, _job("alpha", rev1)) == "done"
    assert _node(db, f"old-{t}")[0][2] == "current"
    assert _facts(db, f"old fact {t}")[0][3] == "current"
    assert _support(db, rev1) == {"node": 1, "fact": 1}
    assert _ctl(
        db, "SELECT count(*) FROM control.general_queue WHERE revision_id = %s", (rev1,)
    ) == [(2,)]


def test_general_half_queued_with_gate_jobs(db, ctx_a):
    t = _tag()
    with db.publisher_tx() as conn:
        graph.add_node(conn, "general", "concept", "tool", f"known{t}", aliases=[f"kn{t}"])
    o_session, o_rev = _save(db, ctx_a, f"a-general-other-{t}")
    _publish(db, o_session, o_rev, f"Already  Published {t}", f"rewritten by the gate {t}")
    _, rev = _save(db, ctx_a, f"a-general-{t}")
    gen = _gen(
        concepts=[
            {"name": f"kubeprobe{t}", "type": "tool", "aliases": [f"kp{t}"]},
            {"name": f"KN{t}", "type": "tool"},
        ],
        gfacts=[
            {"type": "lesson", "text": f"Probe  readiness first {t}", "concept": f"kubeprobe{t}"},
            {"type": "lesson", "text": f" already published\n{t}", "concept": f"known{t}"},
        ],
    )
    _store(db, "alpha", rev, gen)
    assert apply.run(db, _job("alpha", rev)) == "done"
    rows = _ctl(
        db,
        "SELECT id, workspace, session_id::text, item, status, item_key "
        "FROM control.general_queue WHERE revision_id = %s ORDER BY id",
        (rev,),
    )
    items = [r[3] for r in rows]
    assert items == [
        {
            "kind": "concept",
            "name": f"kubeprobe{t}",
            "type": "tool",
            "aliases": [f"kp{t}"],
            "source_text": f"kubeprobe{t}",
        },
        {
            "kind": "fact",
            "type": "lesson",
            "text": f"Probe  readiness first {t}",
            "concept": f"kubeprobe{t}",
            "source_text": f"Probe  readiness first {t}",
        },
    ]
    assert {r[4] for r in rows} == {"pending"} and {r[1] for r in rows} == {"alpha"}
    assert rows[0][5].startswith("concept:") and rows[1][5].startswith("fact:")
    gates = _ctl(
        db,
        "SELECT key, workspace FROM control.jobs WHERE kind = 'gate' AND key = ANY(%s)",
        ([str(r[0]) for r in rows],),
    )
    assert sorted(gates) == sorted((str(r[0]), "alpha") for r in rows)

    assert apply.run(db, _job("alpha", rev)) == "done"
    again = _ctl(db, "SELECT id FROM control.general_queue WHERE revision_id = %s", (rev,))
    assert sorted(r[0] for r in again) == sorted(r[0] for r in rows)


def test_older_revision_queue_items_stay_live(db, ctx_a):
    t = _tag()
    sid = f"a-stale-{t}"
    _, rev1 = _save(db, ctx_a, sid, "first")
    _store(
        db,
        "alpha",
        rev1,
        _gen(
            concepts=[{"name": f"stalec{t}", "type": "tool"}],
            gfacts=[
                {"type": "lesson", "text": f"{t}{t} first", "concept": f"stalec{t}"},
                {"type": "lesson", "text": f"published one {t}", "concept": f"stalec{t}"},
            ],
        ),
    )
    assert apply.run(db, _job("alpha", rev1)) == "done"
    rows = _ctl(
        db, "SELECT id, item FROM control.general_queue WHERE revision_id = %s ORDER BY id", (rev1,)
    )
    published_q = next(r[0] for r in rows if r[1].get("text") == f"published one {t}")
    with db.control_tx() as conn:
        conn.execute(
            "UPDATE control.general_queue SET status = 'published' WHERE id = %s", (published_q,)
        )

    _, rev2 = _save(db, ctx_a, sid, "second")
    _store(
        db,
        "alpha",
        rev2,
        _gen(gfacts=[{"type": "lesson", "text": f"quite another thing {t}", "concept": "x"}]),
    )
    assert apply.run(db, _job("alpha", rev2)) == "done"
    statuses = dict(
        _ctl(db, "SELECT id, status FROM control.general_queue WHERE revision_id = %s", (rev1,))
    )
    assert statuses.pop(published_q) == "published"
    assert set(statuses.values()) == {"pending"} and len(statuses) == 2
    new = _ctl(db, "SELECT status FROM control.general_queue WHERE revision_id = %s", (rev2,))
    assert new == [("pending",)]


def test_published_fact_survives_session_not_repeating_it(db, ctx_a):
    t = _tag()
    sid = f"a-greco-{t}"
    session, rev1 = _save(db, ctx_a, sid, "first")
    _store(db, "alpha", rev1, _gen())
    assert apply.run(db, _job("alpha", rev1)) == "done"
    quiet, _ = _publish(db, session, rev1, f"rotate tokens weekly {t}")
    kept, _ = _publish(db, session, rev1, f"Always set resource limits {t}")
    _, rev2 = _save(db, ctx_a, sid, "second")
    _store(
        db,
        "alpha",
        rev2,
        _gen(
            gfacts=[{"type": "lesson", "text": f"always set resource limits! {t}", "concept": "x"}]
        ),
    )
    assert apply.run(db, _job("alpha", rev2)) == "done"
    with db.publisher_tx() as conn:
        versions = conn.execute(
            "SELECT reason FROM general.versions WHERE item_id = %s", (quiet,)
        ).fetchall()
    assert _general_status(db, [quiet, kept]) == {quiet: "current", kept: "current"}
    assert versions == []
    queued = _ctl(db, "SELECT item FROM control.general_queue WHERE revision_id = %s", (rev2,))
    assert queued == []  # a rephrasing of what this session already published


def test_rephrased_general_fact_is_not_queued_twice(db, ctx_a):
    t = _tag()
    sid = f"a-rephrase-{t}"
    _, rev1 = _save(db, ctx_a, sid, "first")
    _store(
        db,
        "alpha",
        rev1,
        _gen(gfacts=[{"type": "lesson", "text": f"pin {t}{t} chart versions", "concept": "x"}]),
    )
    assert apply.run(db, _job("alpha", rev1)) == "done"
    _, rev2 = _save(db, ctx_a, sid, "second")
    _store(
        db,
        "alpha",
        rev2,
        _gen(
            gfacts=[
                {"type": "lesson", "text": f"pin the {t}{t} chart versions", "concept": "x"},
                {"type": "lesson", "text": f"pin the {t}{t} chart version", "concept": "x"},
            ]
        ),
    )
    assert apply.run(db, _job("alpha", rev2)) == "done"
    rows = _ctl(
        db,
        "SELECT revision_id::text FROM control.general_queue "
        "WHERE item->>'kind' = 'fact' AND item->>'text' LIKE %s",
        (f"%{t}{t}%",),
    )
    assert rows == [(rev1,)]


def test_dropped_general_fact_is_not_queued_again(db, ctx_a):
    t = _tag()
    sid = f"a-dropped-{t}"
    _, rev1 = _save(db, ctx_a, sid, "first")
    fact = {"type": "lesson", "text": f"drop {t}{t} me", "concept": "x"}
    _store(db, "alpha", rev1, _gen(gfacts=[fact]))
    assert apply.run(db, _job("alpha", rev1)) == "done"
    with db.control_tx() as conn:
        conn.execute(
            "UPDATE control.general_queue SET status = 'dropped' WHERE revision_id = %s", (rev1,)
        )
    _, rev2 = _save(db, ctx_a, sid, "second")
    _store(db, "alpha", rev2, _gen(gfacts=[fact]))
    assert apply.run(db, _job("alpha", rev2)) == "done"
    assert _ctl(
        db, "SELECT count(*) FROM control.general_queue WHERE revision_id = %s", (rev2,)
    ) == [(0,)]


def test_same_concept_from_two_sessions_queued_once(db, ctx_a, ctx_a2):
    t = _tag()
    concept = {"name": f"dedupc{t}", "type": "tool"}
    _, r1 = _save(db, ctx_a, f"a-dc1-{t}")
    _store(db, "alpha", r1, _gen(concepts=[concept]))
    assert apply.run(db, _job("alpha", r1)) == "done"
    _, r2 = _save(db, ctx_a2, f"a-dc2-{t}")
    _store(db, "alpha", r2, _gen(concepts=[{"name": f"DedupC{t}", "type": "tool"}]))
    assert apply.run(db, _job("alpha", r2)) == "done"
    rows = _ctl(
        db,
        "SELECT revision_id::text FROM control.general_queue "
        "WHERE item->>'kind' = 'concept' AND lower(item->>'name') = %s",
        (f"dedupc{t}",),
    )
    assert rows == [(r1,)]


def test_rewritten_published_fact_is_not_churned(db, ctx_a):
    t = _tag()
    sid = f"a-rewrite-{t}"
    session, rev1 = _save(db, ctx_a, sid, "first")
    source = f"pin acme-{t} chart versions in prod"
    _store(db, "alpha", rev1, _gen(gfacts=[{"type": "lesson", "text": source, "concept": "x"}]))
    assert apply.run(db, _job("alpha", rev1)) == "done"
    ((qid, item, key),) = _ctl(
        db,
        "SELECT id, item, item_key FROM control.general_queue WHERE revision_id = %s",
        (rev1,),
    )
    assert item["source_text"] == source and item["text"] == source
    # The gate rewrites the text (item.text only) and publishes it.
    rewritten = f"pin chart versions {t}"
    with db.publisher_tx() as conn:
        fid = graph.add_fact(conn, "general", "lesson", rewritten)
        conn.execute(
            "UPDATE control.general_queue SET item = jsonb_set(item, '{text}', to_jsonb(%s::text)), "
            "status = 'published', published_id = %s WHERE id = %s",
            (rewritten, fid, qid),
        )
        conn.execute(
            "INSERT INTO control.general_origin "
            "(item_kind, item_id, workspace, session_id, revision_id, queue_id) "
            "VALUES ('fact', %s, 'alpha', %s, %s, %s)",
            (fid, session, rev1, qid),
        )
    _, rev2 = _save(db, ctx_a, sid, "second")
    _store(db, "alpha", rev2, _gen(gfacts=[{"type": "lesson", "text": source, "concept": "x"}]))
    assert apply.run(db, _job("alpha", rev2)) == "done"
    assert _general_status(db, [fid]) == {fid: "current"}
    assert _ctl(
        db, "SELECT count(*) FROM control.general_queue WHERE revision_id = %s", (rev2,)
    ) == [(0,)]
    assert _ctl(
        db, "SELECT item_key, item->>'source_text' FROM control.general_queue WHERE id = %s", (qid,)
    ) == [(key, source)]


def test_crash_between_transactions_rerun_has_no_duplicates(db, ctx_a, monkeypatch):
    t = _tag()
    _, rev = _save(db, ctx_a, f"a-crash-{t}")
    _store(
        db,
        "alpha",
        rev,
        _gen(
            nodes=[{"ref": "n1", "kind": "instance", "type": f"cluster{t}", "name": f"eks-{t}"}],
            facts=[{"type": "fact", "text": f"crash fact {t}", "node": "n1"}],
            concepts=[{"name": f"crashc{t}", "type": "tool"}],
            gfacts=[{"type": "lesson", "text": f"crash general {t}", "concept": f"crashc{t}"}],
        ),
    )
    real = apply._apply_general
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("crash")
        return real(*a, **k)

    monkeypatch.setattr(apply, "_apply_general", flaky)
    with pytest.raises(RuntimeError):
        apply.run(db, _job("alpha", rev))
    assert _ctl(
        db, "SELECT count(*) FROM control.general_queue WHERE revision_id = %s", (rev,)
    ) == [(0,)]
    assert apply.run(db, _job("alpha", rev)) == "done"
    assert apply.run(db, _job("alpha", rev)) == "done"
    assert _snapshot_counts(db, t)[:3] == (1, 0, 1)
    rows = _ctl(db, "SELECT id FROM control.general_queue WHERE revision_id = %s", (rev,))
    assert len(rows) == 2
    gates = _ctl(
        db,
        "SELECT count(*) FROM control.jobs WHERE kind = 'gate' AND key = ANY(%s)",
        ([str(r[0]) for r in rows],),
    )
    assert gates == [(2,)]


def test_supersedes_target_without_revision_is_a_conflict(db, ctx_a):
    t = _tag()
    with db.ws_tx("alpha") as conn:
        node = graph.add_node(conn, "alpha", "instance", f"cluster{t}", f"manual-{t}")
        old = graph.add_fact(conn, "alpha", "decision", f"hand written decision {t}", node_id=node)
    _, rev = _save(db, ctx_a, f"a-manual-{t}")
    _store(
        db,
        "alpha",
        rev,
        _gen(
            nodes=[{"ref": "n1", "kind": "instance", "type": f"cluster{t}", "name": f"manual-{t}"}],
            facts=[{"type": "decision", "text": f"new call {t}", "node": "n1", "supersedes": "f1"}],
            fact_refs={"f1": old},
        ),
    )
    assert apply.run(db, _job("alpha", rev)) == "done"
    ((new, *_),) = _facts(db, f"new call {t}")
    assert _facts(db, f"hand written decision {t}")[0][3] == "superseded"
    assert _q(db, "SELECT new_fact::text FROM ws_alpha.conflicts WHERE old_fact = %s", (old,)) == [
        (new,)
    ]


def test_waiting_concept_type_comes_from_general_half(db, ctx_a):
    t = _tag()
    _, rev = _save(db, ctx_a, f"a-wtype-{t}")
    node = {"kind": "local_concept", "type": f"nodetype{t}"}
    _store(
        db,
        "alpha",
        rev,
        _gen(
            nodes=[
                {**node, "ref": "n1", "name": f"l1-{t}", "concept": f"Listed{t}"},
                {**node, "ref": "n2", "name": f"l2-{t}", "concept": f"unlisted{t}"},
            ],
            concepts=[{"name": f"LISTED{t}", "type": "pattern"}],
        ),
    )
    assert apply.run(db, _job("alpha", rev)) == "done"
    items = sorted(
        (r[0]["name"], r[0]["type"])
        for r in _ctl(db, "SELECT item FROM control.general_queue WHERE revision_id = %s", (rev,))
    )
    assert items == [(f"LISTED{t}", "pattern"), (f"unlisted{t}", "concept")]


def test_normalisation_handles_nbsp(db, ctx_a):
    t = _tag()
    with db.ws_tx("alpha") as conn:
        node = graph.add_node(conn, "alpha", "instance", f"cluster{t}", f"Prod EKS {t}")
        fid = graph.add_fact(conn, "alpha", "lesson", f"Pin the chart {t}", node_id=node)
    _, rev = _save(db, ctx_a, f"a-nbsp-{t}")
    _store(
        db,
        "alpha",
        rev,
        _gen(
            nodes=[
                {"ref": "n1", "kind": "instance", "type": f"cluster{t}", "name": f"prod\xa0eks {t}"}
            ],
            facts=[{"type": "lesson", "text": f"pin\xa0the  CHART {t}", "node": "n1"}],
        ),
    )
    assert apply.run(db, _job("alpha", rev)) == "done"
    assert _q(db, "SELECT count(*) FROM ws_alpha.nodes WHERE type = %s", (f"cluster{t}",)) == [(1,)]
    facts = _q(db, "SELECT id::text, version FROM ws_alpha.facts WHERE node_id = %s", (node,))
    assert facts == [(fid, 1)]  # same normalised text: same fact, no new version
    assert apply.normalise("a\xa0B\u2003c ") == "a b c"


def test_workspace_role_cannot_read_queue_or_origin(db):
    for table in ("general_queue", "general_origin"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), db.ws_tx("alpha") as conn:
            conn.execute(f"SELECT * FROM control.{table}").fetchall()


def test_publisher_inserts_jobs_but_cannot_read_them(db):
    with db.publisher_tx() as conn:
        jobs.enqueue(conn, "beta", "gate", f"probe-{_tag()}", {"queue_id": 0})
        jobs.enqueue(conn, "beta", "apply", f"concept:probe-{_tag()}", {"revision_id": None})
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.publisher_tx() as conn:
        jobs.enqueue(conn, "beta", "embed", f"probe-{_tag()}", {"revision_id": None})
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.publisher_tx() as conn:
        conn.execute("SELECT * FROM control.jobs").fetchall()


def test_handle_completes_job(db, ctx_a):
    t = _tag()
    _, rev = _save(db, ctx_a, f"a-handle-{t}")
    _store(db, "alpha", rev, _gen())
    with db.control_tx() as conn:
        conn.execute("DELETE FROM control.jobs WHERE kind = 'apply' AND key = %s", (rev,))
        jobs.enqueue(conn, "alpha", "apply", rev, {"revision_id": rev}, run_after_s=-86400)
    job = jobs.claim(db, {"apply"})
    assert job.key == rev
    assert apply.handle(db, job) == "done"
    assert _ctl(db, "SELECT status FROM control.jobs WHERE id = %s", (job.id,)) == [("done",)]


def test_mostly_dropped_generation_supersedes_nothing(db, ctx_a):
    """D > K: the kept items are applied and queued, but the open-thread reconcile does not
    run, so a reply the model mostly botched cannot close the threads older revisions left."""
    t = _tag()
    sid = f"a-dk-{t}"
    _, rev1 = _save(db, ctx_a, sid, "first")
    _store(db, "alpha", rev1, _gen(facts=[{"type": "open_thread", "text": f"dk old thread {t}"}]))
    assert apply.run(db, _job("alpha", rev1)) == "done"

    _, rev2 = _save(db, ctx_a, sid, "second")
    gen = _gen(
        gfacts=[{"type": "lesson", "text": f"rotate credentials monthly {t}", "concept": "x"}],
    )
    gen["stats"] = {"kept": 2, "dropped": 3}
    _store(db, "alpha", rev2, gen)
    assert apply.run(db, _job("alpha", rev2)) == "done"
    assert _facts(db, f"dk old thread {t}")[0][3] == "current"
    new = _ctl(db, "SELECT item FROM control.general_queue WHERE revision_id = %s", (rev2,))
    assert [r[0]["text"] for r in new] == [f"rotate credentials monthly {t}"]  # kept items queued

    # D <= K reconciles the open threads
    _, rev3 = _save(db, ctx_a, sid, "third")
    gen = _gen()
    gen["stats"] = {"kept": 1, "dropped": 1}
    _store(db, "alpha", rev3, gen)
    assert apply.run(db, _job("alpha", rev3)) == "done"
    assert _facts(db, f"dk old thread {t}")[0][3] == "superseded"


def test_handle_skipped_marks_job_skipped(db, ctx_a):
    t = _tag()
    _, rev = _save(db, ctx_a, f"a-skip-{t}")  # no generation stored: apply skips
    with db.control_tx() as conn:
        jobs.enqueue(conn, "alpha", "apply", rev, {"revision_id": rev}, run_after_s=-86400)
    job = jobs.claim(db, {"apply"})
    assert job.key == rev
    assert apply.handle(db, job) == "skipped"
    assert _ctl(db, "SELECT status FROM control.jobs WHERE id = %s", (job.id,)) == [("skipped",)]


def test_waiting_fact_matches_concept_by_alias_on_reapply(db, ctx_a):
    """A fact waiting for concept "k8s" is the same fact once a later revision names the
    general concept by its name ("kubernetes") of which "k8s" is an alias."""
    t = _tag()
    sid = f"a-alias-{t}"
    _, rev1 = _save(db, ctx_a, sid, "first")
    local = {"ref": "n1", "kind": "local_concept", "type": "tool", "name": f"ourk-{t}"}
    fact = {"type": "lesson", "text": f"drain nodes before upgrades {t}", "node": "n1"}
    _store(db, "alpha", rev1, _gen(nodes=[{**local, "concept": f"k8s{t}"}], facts=[fact]))
    assert apply.run(db, _job("alpha", rev1)) == "done"
    ((fid, _, node_id, _, _, wait, _),) = _facts(db, f"drain nodes%{t}")
    assert (node_id, wait) == (None, f"k8s{t}")

    with db.publisher_tx() as conn:  # published under its full name, the short one an alias
        general_id = graph.add_node(
            conn, "general", "concept", "tool", f"Kubernetes{t}", aliases=[f"K8S{t}"]
        )
    _, rev2 = _save(db, ctx_a, sid, "second")
    _store(db, "alpha", rev2, _gen(nodes=[{**local, "concept": f"kubernetes{t}"}], facts=[fact]))
    assert apply.run(db, _job("alpha", rev2)) == "done"
    ((local_id, _, _, refines),) = _node(db, f"ourk-{t}")
    assert refines == general_id
    facts = _facts(db, f"drain nodes%{t}")
    assert [(f[0], f[2], f[3], f[5]) for f in facts] == [(fid, local_id, "current", None)]


def test_edge_between_refs_of_one_node_is_skipped(db, ctx_a):
    t = _tag()
    _, rev = _save(db, ctx_a, f"a-loop-{t}")
    gen = _gen(
        nodes=[
            {"ref": "n1", "kind": "instance", "type": f"svc{t}", "name": f"loop-{t}"},
            {"ref": "n2", "kind": "instance", "type": f"svc{t}", "name": f"LOOP-{t}"},
        ],
        edges=[{"src": "n1", "dst": "n2", "type": "uses"}],
        facts=[{"type": "fact", "text": f"loop fact {t}", "node": "n1"}],
    )
    _store(db, "alpha", rev, gen)
    assert apply.run(db, _job("alpha", rev)) == "done"
    ((n1, _, _, _),) = _node(db, f"loop-{t}")
    assert _q(db, "SELECT count(*) FROM ws_alpha.edges WHERE src = %s", (n1,)) == [(0,)]
    assert len(_facts(db, f"loop fact {t}")) == 1
