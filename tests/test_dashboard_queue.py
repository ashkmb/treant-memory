"""Dashboard review queue, quarantine and jobs pages."""

import uuid

import pytest
from dashboard_fakes import PUBLIC_URL, FakeProvider, dash_settings, session_data, set_session
from fakes import FakeEmbedder
from psycopg.types.json import Jsonb
from starlette.testclient import TestClient

from treant import apply, jobs, names, owner


@pytest.fixture
def client(fresh_db):
    from treant.dashboard.app import build_dashboard

    app = build_dashboard(fresh_db, FakeEmbedder(), dash_settings(), FakeProvider())
    with TestClient(app, base_url=PUBLIC_URL, follow_redirects=False) as c:
        c.get("/login")
        assert c.get("/auth/callback").status_code == 303
        yield c


def _post(c, path, **data):
    return c.post(path, data={"csrf": session_data(c)["csrf"], **data})


def _stale(c):
    s = session_data(c)
    set_session(c, {**s, "auth_time": s["auth_time"] - 1000})


def _flashes(c):
    return [f[1] for f in session_data(c).get("flash", [])]


def _audit(db, action):
    with db.admin_tx() as c:
        return c.execute(
            "SELECT actor, detail FROM control.audit WHERE action = %s ORDER BY id", (action,)
        ).fetchall()


def _status(db, qid):
    with db.admin_tx() as c:
        return c.execute(
            "SELECT status FROM control.general_queue WHERE id = %s", (qid,)
        ).fetchone()[0]


def _add(db, text, item=None):
    item = item or {
        "kind": "fact",
        "type": "lesson",
        "text": text,
        "concept": None,
        "source_text": text,
    }
    with db.publisher_tx() as conn:
        return conn.execute(
            "INSERT INTO control.general_queue "
            "(workspace, session_id, revision_id, item, item_key, status) "
            "VALUES ('alpha', %s, %s, %s, %s, 'waiting_review') RETURNING id",
            (str(uuid.uuid4()), str(uuid.uuid4()), Jsonb(item), apply.item_key(item["kind"], text)),
        ).fetchone()[0]


def _published(db):
    with db.admin_tx() as c:
        return c.execute(
            "SELECT count(*) FROM control.general_queue WHERE status='published'"
        ).fetchone()[0]


def test_queue_default_lists_waiting_review_with_similar_hint(fresh_db, client):
    a = _add(fresh_db, "always pin the image tag in production deployments")
    b = _add(fresh_db, "always pin the image tag in production deployment")
    c = _add(fresh_db, "<script>alert(1)</script>")
    owner.general_drop(fresh_db, "cli", c)
    body = client.get("/queue").text
    assert f'href="/queue/{a}"' in body and f"similar to #{a}" in body
    assert f'name="ids" value="{c}"' not in body  # dropped is not shown by default
    assert f'name="ids" value="{b}"' in body
    assert "<script>alert" not in client.get("/queue?status=dropped").text
    assert client.get("/queue?status=bogus").status_code == 400


def test_approve_requires_recent_login(fresh_db, client):
    qid = _add(fresh_db, "prefer small commits")
    _stale(client)
    r = _post(client, f"/queue/{qid}/approve")
    assert r.status_code == 303 and r.headers["location"].startswith("/login?fresh=1")
    assert _published(fresh_db) == 0 and _status(fresh_db, qid) == "waiting_review"


def test_approve_flagged_requires_force(fresh_db, queued_item, client):
    page = client.get(f"/queue/{queued_item}").text
    assert "<mark>zorblax</mark>" in page and 'name="force"' in page
    r = _post(client, f"/queue/{queued_item}/approve")
    assert r.headers["location"] == f"/queue/{queued_item}"
    assert any("flagged" in f for f in _flashes(client))
    assert _status(fresh_db, queued_item) == "waiting_review"
    r = _post(client, f"/queue/{queued_item}/approve", force="1")
    assert r.headers["location"] == "/queue" and _status(fresh_db, queued_item) == "published"
    assert _audit(fresh_db, "general.approve")[-1][0] == "dashboard:alice"


def test_approve_edited_text_is_rechecked(fresh_db, queued_item, client):
    r = _post(client, f"/queue/{queued_item}/approve", text="still zorblax here")
    assert r.headers["location"] == f"/queue/{queued_item}"
    assert any("edited text" in f for f in _flashes(client))
    _post(client, f"/queue/{queued_item}/approve", text="a clean rewrite")
    assert _status(fresh_db, queued_item) == "published"
    assert _audit(fresh_db, "general.approve")[-1][1]["edited"] is True


def test_approve_after_drop_is_a_flash_not_500(fresh_db, client):
    qid = _add(fresh_db, "prefer small commits")
    owner.general_drop(fresh_db, "cli", qid)
    r = _post(client, f"/queue/{qid}/approve")
    assert r.status_code == 303 and any("not open" in f for f in _flashes(client))


def test_bad_ids(client):
    assert client.get("/queue/abc").status_code == 404
    assert client.get(f"/queue/{2**63}").status_code == 404
    r = _post(client, "/queue/abc/drop")
    assert r.status_code == 303 and "invalid id" in _flashes(client)
    assert _post(client, "/queue/bulk", op="drop", ids="x").status_code == 303
    assert _flashes(client)[-1] == "ids"


def test_drop_item_and_bulk_drop(fresh_db, client):
    a, b = _add(fresh_db, "one"), _add(fresh_db, "two")
    assert _post(client, f"/queue/{a}/drop").status_code == 303
    assert _status(fresh_db, a) == "dropped"
    _stale(client)  # dropping is not sensitive
    r = _post(client, "/queue/bulk", op="drop", ids=[str(a), str(b)])
    assert r.headers["location"] == "/queue" and _status(fresh_db, b) == "dropped"
    assert f"#{a} (not open)" in _flashes(client)[-1]


def test_bulk_approve_skips_flagged(fresh_db, queued_item, client):
    ok = _add(fresh_db, "prefer small commits")
    gone = _add(fresh_db, "another clean one")
    owner.general_drop(fresh_db, "cli", gone)
    _post(client, "/queue/bulk", op="approve", ids=[str(ok), str(queued_item), str(gone)])
    assert _status(fresh_db, ok) == "published"
    assert _status(fresh_db, queued_item) == "waiting_review"
    msg = _flashes(client)[-1]
    assert msg.startswith("published 1; skipped")
    assert f"#{queued_item} (flagged)" in msg and f"#{gone} (not open)" in msg


def test_bulk_approve_is_sensitive_and_capped(fresh_db, client):
    ok = _add(fresh_db, "prefer small commits")
    _stale(client)
    r = _post(client, "/queue/bulk", op="approve", ids=str(ok))
    assert r.headers["location"].startswith("/login?fresh=1")
    assert _status(fresh_db, ok) == "waiting_review"
    r = _post(client, "/queue/bulk", op="drop", ids=[str(i) for i in range(201)])
    assert _flashes(client)[-1] == "ids"


def _quarantine(db):
    tok = owner.create_token(db, "cli", "alpha", "lbl", {"read"})
    with db.ws_tx("alpha") as conn:
        sid = conn.execute(
            "INSERT INTO ws_alpha.sessions (token_id, client_session_id) VALUES (%s, 'cs1') "
            "RETURNING id",
            (tok.split("_")[1],),
        ).fetchone()[0]
        return str(
            conn.execute(
                "INSERT INTO ws_alpha.revisions (session_id, n, sections, status, removed) "
                "VALUES (%s, 1, %s, 'quarantined', 1) RETURNING id",
                (sid, Jsonb({"journal": "key <b>SECRET</b>", "facts": [{"t": "x"}]})),
            ).fetchone()[0]
        )


def _rev_status(db, rev):
    with db.admin_tx() as c:
        return c.execute("SELECT status FROM ws_alpha.revisions WHERE id = %s", (rev,)).fetchone()[
            0
        ]


def test_quarantine_view_is_sensitive_and_no_store(fresh_db, client):
    rev = _quarantine(fresh_db)
    listing = client.get("/quarantine")
    assert rev in listing.text and "SECRET" not in listing.text
    r = client.get(f"/quarantine/alpha/{rev}")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert "key &lt;b&gt;SECRET&lt;/b&gt;" in r.text and "<b>SECRET" not in r.text
    assert "&#34;t&#34;: &#34;x&#34;" in r.text or "&quot;t&quot;" in r.text
    _stale(client)
    r = client.get(f"/quarantine/alpha/{rev}")
    assert r.status_code == 303 and "fresh=1" in r.headers["location"]
    assert "SECRET" not in r.text


def test_release_and_drop_quarantine(fresh_db, client):
    rev = _quarantine(fresh_db)
    _stale(client)
    r = _post(client, f"/quarantine/alpha/{rev}/release")
    assert "fresh=1" in r.headers["location"] and _rev_status(fresh_db, rev) == "quarantined"
    s = session_data(client)
    set_session(client, {**s, "auth_time": s["auth_time"] + 1000})
    assert _post(client, f"/quarantine/alpha/{rev}/release").headers["location"] == "/quarantine"
    assert _rev_status(fresh_db, rev) == "stored"
    rev2 = _quarantine(fresh_db)
    _stale(client)  # drop is not sensitive
    _post(client, f"/quarantine/alpha/{rev2}/drop")
    assert _rev_status(fresh_db, rev2) == "dropped"
    release = _audit(fresh_db, "quarantine.release")
    assert [r[0] for r in release] == ["dashboard:alice"]
    assert [r[0] for r in _audit(fresh_db, "quarantine.drop")] == ["dashboard:alice"]


def _dead(db, key, ws="alpha", kind="distill"):
    with db.control_tx() as conn:
        jobs.enqueue(conn, ws, kind, key, {})
    with db.admin_tx() as c:
        c.execute("UPDATE control.jobs SET status='dead' WHERE key = %s", (key,))


def test_retry_dead_jobs_audits_ids(fresh_db, client):
    _dead(fresh_db, "k1")
    _dead(fresh_db, "k2", kind="embed")
    _dead(fresh_db, "k3", ws="beta")
    body = client.get("/jobs").text
    assert "k1" in body and "k2" in body
    assert "k1" not in client.get("/jobs?status=queued").text
    assert client.get("/jobs?status=zzz").status_code == 400
    r = _post(client, "/jobs/retry-dead", ws="alpha", kind="distill")
    assert r.status_code == 303
    rows = _audit(fresh_db, "jobs.retry")
    assert rows[-1][0] == "dashboard:alice" and rows[-1][1]["count"] == 1
    with fresh_db.admin_tx() as c:
        left = c.execute("SELECT key FROM control.jobs WHERE status='dead' ORDER BY key").fetchall()
        one = c.execute("SELECT id FROM control.jobs WHERE key='k2'").fetchone()[0]
    assert [k[0] for k in left] == ["k2", "k3"]
    _post(client, f"/jobs/{one}/retry")
    assert _audit(fresh_db, "jobs.retry")[-1][1]["ids"] == [one]
    _post(client, "/jobs/999999/retry")
    assert "not found" in _flashes(client)[-1]


def test_untouched_multiline_fact_is_not_an_edit(fresh_db, client):
    text = "first line\nsecond line\n\nthird"
    qid = _add(fresh_db, text)
    assert "second line" in client.get(f"/queue/{qid}").text
    _post(client, f"/queue/{qid}/approve", text=text.replace("\n", "\r\n"))
    assert _status(fresh_db, qid) == "published"
    assert _audit(fresh_db, "general.approve")[-1][1]["edited"] is False
    with fresh_db.admin_tx() as c:
        pub = c.execute(
            "SELECT f.text FROM general.facts f JOIN control.general_queue q "
            "ON q.published_id = f.id WHERE q.id = %s",
            (qid,),
        ).fetchone()[0]
    assert pub == text


def test_edited_text_has_newlines_normalised(fresh_db, client):
    qid = _add(fresh_db, "one\ntwo")
    _post(client, f"/queue/{qid}/approve", text="one\r\ntwo\rthree")
    assert _audit(fresh_db, "general.approve")[-1][1]["edited"] is True
    with fresh_db.admin_tx() as c:
        pub = c.execute(
            "SELECT f.text FROM general.facts f JOIN control.general_queue q "
            "ON q.published_id = f.id WHERE q.id = %s",
            (qid,),
        ).fetchone()[0]
    assert pub == "one\ntwo\nthree"


def test_queue_and_jobs_render_with_only_these_modules(fresh_db, monkeypatch):
    from treant.dashboard import app as dash
    from treant.dashboard.routes import ops, queue

    monkeypatch.setattr(dash, "ROUTE_MODULES", [queue.routes, ops.routes])
    app = dash.build_dashboard(fresh_db, FakeEmbedder(), dash_settings(), FakeProvider())
    with TestClient(app, base_url=PUBLIC_URL, follow_redirects=False) as c:
        c.get("/login")
        c.get("/auth/callback")
        _dead(fresh_db, "kk")
        _add(fresh_db, "prefer small commits")
        assert c.get("/queue").status_code == 200
        assert c.get("/jobs").status_code == 200


def test_segments():
    from treant.dashboard.routes.queue import segments

    t = "0123456789"
    assert segments(t, [(6, 8), (1, 3)]) == [
        ("0", False),
        ("12", True),
        ("345", False),
        ("67", True),
        ("89", False),
    ]
    assert segments(t, [(1, 5), (4, 7)]) == [("0", False), ("123456", True), ("789", False)]
    assert segments(t, [(-5, -1), (20, 30), (5, 3)]) == [(t, False)]
    assert segments(t, [(-3, 2), (8, 99)]) == [("01", True), ("234567", False), ("89", True)]
    assert segments("", [(0, 1)]) == []


def test_flagged_html_is_escaped_inside_mark(fresh_db, client):
    qid = _add(fresh_db, "use zorblax <img src=x> now")
    with fresh_db.ws_tx("alpha") as conn:
        from treant import graph

        graph.add_node(conn, "alpha", "instance", "cluster", "zorblax")
    body = client.get(f"/queue/{qid}").text
    assert "<mark>zorblax</mark>" in body and "<img src=x>" not in body
    assert "&lt;img src=x&gt;" in body


def test_bulk_names_other_refusals_by_category(fresh_db, client):
    ok = _add(fresh_db, "prefer small commits")
    _post(client, "/queue/bulk", op="approve", ids=[str(ok), "999999"])
    assert "#999999 (not open)" in _flashes(client)[-1]


def test_concept_ignores_text(fresh_db, client):
    item = {
        "kind": "concept",
        "type": "service",
        "name": "widgetron",
        "aliases": [],
        "text": None,
        "concept": None,
        "source_text": "widgetron",
    }
    qid = _add(fresh_db, "widgetron", item)
    body = client.get(f"/queue/{qid}").text
    assert 'name="text"' not in body
    _post(client, f"/queue/{qid}/approve", text="hijacked")
    assert _status(fresh_db, qid) == "published"
    with fresh_db.admin_tx() as c:
        it = c.execute("SELECT item FROM control.general_queue WHERE id = %s", (qid,)).fetchone()[0]
    assert it["name"] == "widgetron" and "hijacked" not in str(it)


def test_post_with_oversized_id_flashes_invalid(client):
    r = _post(client, f"/queue/{2**63}/approve")
    assert r.status_code == 303 and _flashes(client)[-1] == "invalid id"
    _post(client, f"/jobs/{2**63}/retry")
    assert _flashes(client)[-1] == "invalid id"


def test_bad_ws_filter_shows_notice_everywhere(client):
    for path in ("/queue?ws=Bad!", "/jobs?ws=Bad!", "/quarantine?ws=Bad!", "/mistakes?ws=Bad!"):
        r = client.get(path)
        assert r.status_code == 200 and "unknown workspace filter ignored" in r.text, path
    for path in ("/queue?ws=alpha", "/jobs?ws=alpha", "/quarantine?ws=alpha"):
        assert "unknown workspace filter ignored" not in client.get(path).text, path


def test_quarantine_item_with_bad_workspace_is_404(client):
    assert client.get("/quarantine/Bad!/x").status_code == 404


def test_retry_only_offered_for_dead_jobs(fresh_db, client):
    with fresh_db.admin_tx() as c:
        dead, queued = (
            c.execute(
                "INSERT INTO control.jobs (workspace, kind, key, payload, status) "
                "VALUES ('alpha', 'embed', %s, '{}', %s) RETURNING id",
                (key, status),
            ).fetchone()[0]
            for key, status in (("k-dead", "dead"), ("k-queued", "queued"))
        )
    body = client.get("/jobs?status=all").text
    assert f"/jobs/{dead}/retry" in body and f"/jobs/{queued}/retry" not in body


def test_approve_with_emptied_text_is_refused(fresh_db, client):
    qid = _add(fresh_db, "a clean lesson")
    r = _post(client, f"/queue/{qid}/approve", text="  \r\n ")
    assert r.status_code == 303 and r.headers["location"] == f"/queue/{qid}"
    assert _flashes(client)[-1] == "text is empty"
    assert _status(fresh_db, qid) == "waiting_review"


def test_approve_textarea_is_capped_at_the_gate_limit(fresh_db, client):
    from treant import gate

    qid = _add(fresh_db, "a clean lesson")
    assert f'maxlength="{gate.MAX_TEXT}"' in client.get(f"/queue/{qid}").text


def _concept(db, name="Kubectl"):
    item = {"kind": "concept", "type": "tool", "name": name, "aliases": ["kc"]}
    return _add(db, name, item)


def _wait_fact(db, ws, wait, text, status="current"):
    from treant import graph

    with db.ws_tx(ws) as conn:
        fid = graph.add_fact(conn, ws, "lesson", text, wait_concept=wait)
        if status != "current":
            conn.execute(
                f"UPDATE {names.ws_schema(ws)}.facts SET status = %s WHERE id = %s", (status, fid)
            )


def test_item_page_explains_fact(fresh_db, client):
    qid = _add(fresh_db, "pin image tags")
    body = client.get(f"/queue/{qid}").text
    assert "What approving does" in body
    assert "Shares this sentence with every workspace." in body
    assert "reveals nothing about where it came from" in body
    assert "When unsure, drop" in body
    assert "waiting on this concept" not in body


def test_item_page_explains_concept_with_waiting_count(fresh_db, client):
    qid = _concept(fresh_db, "Kubectl")
    _wait_fact(fresh_db, "alpha", "kubectl", "use kubectl diff")
    _wait_fact(fresh_db, "alpha", "KUBECTL", "use kubectl apply carefully")
    _wait_fact(fresh_db, "alpha", "kubectl", "old", status="superseded")
    _wait_fact(fresh_db, "alpha", "helm", "other concept")
    _wait_fact(fresh_db, "beta", "kubectl", "other workspace")
    body = client.get(f"/queue/{qid}").text
    assert "What approving does" in body
    assert "Adds a shared topic named Kubectl;" in body
    assert "a public thing anyone could know" in body
    assert "When unsure, drop" in body
    assert "2 facts in alpha are waiting on this concept" in body
    assert "Shares this sentence" not in body


def test_item_page_concept_with_no_waiting_facts(fresh_db, client):
    qid = _concept(fresh_db, "Postgres")
    assert "No facts in alpha are waiting on this concept" in client.get(f"/queue/{qid}").text
