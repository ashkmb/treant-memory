"""Dashboard mistakes, measurements and audit pages."""

import pytest
from dashboard_fakes import (
    PUBLIC_URL,
    FakeProvider,
    dash_settings,
    session_data,
    set_session,
)
from fakes import FakeEmbedder
from starlette.testclient import TestClient

from treant import owner


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


def _metric(db, name, value, hours_ago):
    with db.admin_tx() as c:
        c.execute(
            "INSERT INTO control.metrics (name, workspace, value, at) "
            "VALUES (%s, NULL, %s, now() - make_interval(hours => %s))",
            (name, value, hours_ago),
        )


def _mistake_rows(db):
    with db.admin_tx() as c:
        return c.execute("SELECT 1 FROM control.mistakes").fetchall()


def _flashes(c):
    return [f[1] for f in session_data(c).get("flash", [])]


def test_log_mistake_from_dashboard(client, fresh_db):
    r = _post(client, "/mistakes", workspace="alpha", text="line1\r\n<b>x</b>")
    assert r.status_code == 303 and r.headers["location"] == "/mistakes"
    with fresh_db.admin_tx() as c:
        rows = c.execute("SELECT workspace, text FROM control.mistakes").fetchall()
        audit = c.execute(
            "SELECT actor, detail FROM control.audit WHERE action = 'mistake.log'"
        ).fetchall()
    assert rows == [("alpha", "line1\n<b>x</b>")]
    assert audit[0][0].startswith("dashboard:") and "text" not in audit[0][1]
    page = client.get("/mistakes")
    assert "&lt;b&gt;x&lt;/b&gt;" in page.text and "<b>x</b>" not in page.text
    assert "alpha" in client.get("/mistakes?ws=alpha").text
    assert "<b>x</b>" not in client.get("/mistakes?ws=beta").text
    # refusal flashes and goes back
    r = _post(client, "/mistakes", workspace="nosuch", text="x")
    assert r.status_code == 303
    r = _post(client, "/mistakes", workspace="Bad Name!", text="x")
    assert r.status_code == 303 and r.headers["location"] == "/mistakes"
    assert _flashes(client)[-1] == "invalid workspace name"
    r = _post(client, "/mistakes", workspace="alpha", text="")
    assert r.status_code == 303 and len(_mistake_rows(fresh_db)) == 1
    assert client.post("/mistakes", data={"workspace": "alpha", "text": "x"}).status_code == 403


def test_measurements_flags_stale_isolation(client, fresh_db):
    _metric(fresh_db, "isolation.faults", 0, 40)
    _metric(fresh_db, "isolation.checks", 5, 40)
    r = client.get("/measurements")
    assert r.status_code == 200 and 'class="bad"' in r.text
    assert "not collected yet" in r.text


def test_isolation_age_boundary(client, fresh_db, monkeypatch):
    import time

    from treant.dashboard import security

    _metric(fresh_db, "isolation.faults", 0, 0)
    _metric(fresh_db, "isolation.checks", 5, 0)
    base = time.time()
    monkeypatch.setattr(security, "now", lambda: base + 35 * 3600)
    set_session(client, {**session_data(client), "auth_time": int(base + 35 * 3600)})
    assert 'class="ok"' in client.get("/measurements").text
    monkeypatch.setattr(security, "now", lambda: base + 37 * 3600)
    set_session(client, {**session_data(client), "auth_time": int(base + 37 * 3600)})
    assert 'class="bad"' in client.get("/measurements").text


def test_invalid_ws_filter_shows_notice(client):
    assert "unknown workspace filter ignored" in client.get("/mistakes?ws=Bad%20Name").text
    assert "unknown workspace filter ignored" not in client.get("/mistakes?ws=alpha").text
    assert "unknown workspace filter ignored" not in client.get("/mistakes").text


def test_pager_amp_escaped(client, fresh_db):
    for i in range(101):
        owner.log_mistake(fresh_db, "cli", "alpha", f"m{i}")
    for path in ("/mistakes?ws=alpha", "/audit"):
        text = client.get(path).text
        assert "&amp;offset=100" in text and "&offset=" not in text


def test_measurements_fresh_ok_and_faults_bad(client, fresh_db):
    _metric(fresh_db, "isolation.faults", 0, 1)
    _metric(fresh_db, "isolation.checks", 5, 1)
    text = client.get("/measurements").text
    assert 'class="ok"' in text and 'class="bad"' not in text
    _metric(fresh_db, "isolation.faults", 2, 0)
    assert 'class="bad"' in client.get("/measurements").text


def test_measurements_empty_db_renders(client):
    r = client.get("/measurements")
    assert r.status_code == 200 and 'class="bad"' in r.text
    assert "not collected" in r.text and "alpha" in r.text


def test_audit_filter_and_paging(client, fresh_db):
    for i in range(105):
        owner.log_mistake(fresh_db, "cli", "alpha", f"m{i}")
    with fresh_db.admin_tx() as c:
        owner.audit(c, "cli", "other.thing", {"k": "<i>v</i>"})
    r = client.get("/audit")
    assert r.status_code == 200 and "offset=100" in r.text
    assert "&lt;i&gt;v&lt;/i&gt;" in r.text
    r = client.get("/audit?action=mistake.")
    assert "other.thing" not in r.text and "mistake.log" in r.text
    r2 = client.get("/audit?action=mistake.&offset=100")
    assert r2.status_code == 200 and "mistake.log" in r2.text
    assert client.get("/audit?action=" + "a" * 65).status_code == 200


def test_mistakes_page_has_no_inline_style_and_styled_notice(client, fresh_db):
    owner.log_mistake(fresh_db, "cli", "alpha", "line one\nline two")
    body = client.get("/mistakes?ws=Bad!").text
    assert "style=" not in body
    assert (
        'class="notice alert alert-warning alert-soft">unknown workspace filter ignored</p>' in body
    )
    assert '<td class="text-block">' in client.get("/mistakes").text


def test_isolation_row_classes_are_styled():
    from pathlib import Path

    import treant.dashboard as d

    css = (Path(d.__file__).parent / "static" / "app.css").read_text()
    assert "tr.bad" in css and "tr.ok" in css
