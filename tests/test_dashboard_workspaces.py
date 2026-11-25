"""Dashboard workspace, target and token pages."""

import pytest
from dashboard_fakes import PUBLIC_URL, FakeProvider, dash_settings, session_data, set_session
from fakes import FakeEmbedder
from starlette.testclient import TestClient

from treant import owner, settings, tokens


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


def _audit(db, prefix):
    with db.admin_tx() as c:
        return c.execute(
            "SELECT actor, action, detail FROM control.audit WHERE action LIKE %s ORDER BY id",
            (prefix + "%",),
        ).fetchall()


FORM = {
    "backend": "litellm",
    "alias": "default",
    "judge_alias": "default",
    "publish": "auto",
    "domains": "",
    "ticket_prefixes": "",
    "terms": "",
}


def test_workspaces_pages_render(client):
    r = client.get("/workspaces")
    assert r.status_code == 200 and "alpha" in r.text and "beta" in r.text
    r = client.get("/workspaces/alpha")
    assert r.status_code == 200 and 'name="publish"' in r.text
    assert client.get("/workspaces/nosuchws").status_code == 404
    assert client.get("/workspaces/Bad%20Name").status_code == 404


def test_create_and_purge_are_cli_only(client):
    r = client.get("/workspaces")
    assert "sudo treant workspace create" in r.text
    r = client.get("/workspaces/alpha")
    assert "sudo treant workspace purge alpha --confirm alpha" in r.text
    assert _post(client, "/workspaces", name="new").status_code == 405
    assert _post(client, "/workspaces/alpha/purge").status_code in (404, 405)


def test_settings_edit_requires_recent_login_and_validates(fresh_db, client):
    _stale(client)
    r = _post(client, "/workspaces/alpha/settings", **{**FORM, "publish": "review"})
    assert r.status_code == 303 and r.headers["location"].startswith("/login?fresh=1")
    assert settings.load(fresh_db, "alpha").publish == "auto"
    # fresh again
    client.get("/login")
    client.get("/auth/callback")
    r = _post(client, "/workspaces/alpha/settings", **{**FORM, "publish": "review"})
    assert r.status_code == 303 and r.headers["location"] == "/workspaces/alpha"
    assert settings.load(fresh_db, "alpha").publish == "review"
    rows = _audit(fresh_db, "workspace.settings")
    assert rows[-1][0] == "dashboard:alice" or rows[-1][0].startswith("dashboard:")
    assert rows[-1][2]["changed"] == ["publish"]
    # invalid
    _post(client, "/workspaces/alpha/settings", **{**FORM, "publish": "bogus"})
    assert "publish" in " ".join(_flashes(client))
    assert settings.load(fresh_db, "alpha").publish == "review"
    _post(client, "/workspaces/alpha/settings", **{**FORM, "alias": " ", "publish": "review"})
    assert settings.load(fresh_db, "alpha").alias == "default"


def test_settings_unchanged_writes_no_audit(fresh_db, client):
    _post(client, "/workspaces/alpha/settings", **FORM)
    assert "no changes" in " ".join(_flashes(client))
    assert _audit(fresh_db, "workspace.settings") == []


def test_settings_reset(fresh_db, client):
    owner.set_workspace_settings(fresh_db, "cli", "alpha", {"publish": "review"})
    _post(client, "/workspaces/alpha/settings", **{**FORM, "publish": "review", "reset": "1"})
    assert settings.load(fresh_db, "alpha").publish == "auto"
    assert _audit(fresh_db, "workspace.settings")[-1][2]["reset"] is True


def test_rule_lists_roundtrip(fresh_db, client):
    r = _post(
        client,
        "/workspaces/alpha/settings",
        **{
            **FORM,
            "domains": "a.example.com\r\n\r\n  b.example.com  \r\n",
            "ticket_prefixes": "ABC\n",
            "terms": "secret-word\nother term",
        },
    )
    assert r.status_code == 303
    s = settings.load(fresh_db, "alpha")
    assert s.domains == ("a.example.com", "b.example.com")
    assert s.ticket_prefixes == ("ABC",) and s.terms == ("secret-word", "other term")
    page = client.get("/workspaces/alpha").text
    assert "a.example.com\nb.example.com" in page.replace("\r", "")
    n = len(_audit(fresh_db, "workspace.settings"))
    _post(client, "/workspaces/alpha/settings", **{**FORM, "terms": "ab"})
    assert "3 characters" in " ".join(_flashes(client))
    assert settings.load(fresh_db, "alpha").terms == ("secret-word", "other term")
    assert len(_audit(fresh_db, "workspace.settings")) == n


def test_targets_add_remove(fresh_db, client):
    from treant import browse

    r = _post(client, "/workspaces/alpha/targets/add", kind="kube_context", value="ctx1")
    assert r.status_code == 303
    assert browse.targets(fresh_db, "alpha") == [{"kind": "kube_context", "value": "ctx1"}]
    assert "ctx1" in client.get("/workspaces/alpha").text
    _post(client, "/workspaces/alpha/targets/add", kind="bogus", value="x")
    assert "kind" in " ".join(_flashes(client))
    _post(client, "/workspaces/alpha/targets/add", kind="git_host", value="  ")
    assert len(browse.targets(fresh_db, "alpha")) == 1
    _stale(client)
    r = _post(client, "/workspaces/alpha/targets/remove", kind="kube_context", value="ctx1")
    assert r.headers["location"].startswith("/login?fresh=1")
    client.get("/login")
    client.get("/auth/callback")
    _post(client, "/workspaces/alpha/targets/remove", kind="kube_context", value="ctx1")
    assert browse.targets(fresh_db, "alpha") == []
    assert [a[1] for a in _audit(fresh_db, "target.")] == ["target.add", "target.remove"]


def test_archive_requires_typed_name(fresh_db, client):
    owner.create_token(fresh_db, "cli", "alpha", "l", {"read"})
    _post(client, "/workspaces/alpha/archive", confirm="beta")
    assert "type the workspace name to confirm" in " ".join(_flashes(client))
    assert _audit(fresh_db, "workspace.archive") == []
    r = _post(client, "/workspaces/alpha/archive", confirm="alpha")
    assert r.status_code == 303
    assert len(_audit(fresh_db, "workspace.archive")) == 1
    page = client.get("/workspaces/alpha")
    assert page.status_code == 200 and "archived" in page.text
    assert 'action="/workspaces/alpha/settings"' not in page.text
    assert 'action="/workspaces/alpha/targets/add"' not in page.text
    # refused afterwards
    _post(client, "/workspaces/alpha/settings", **{**FORM, "publish": "review"})
    _post(client, "/workspaces/alpha/targets/add", kind="git_host", value="h")
    from treant import browse

    assert browse.targets(fresh_db, "alpha") == []
    assert _audit(fresh_db, "workspace.settings") == []


def test_token_create_shown_once_no_store(fresh_db, client):
    r = _create(client, workspace="alpha", label=" laptop ", actions=["read", "create"])
    assert r.status_code == 200
    assert "no-store" in r.headers["cache-control"] and r.headers["pragma"] == "no-cache"
    token = next(w for w in r.text.replace('"', " ").split() if w.startswith("trt_"))
    secret = token.split("_", 2)[2]
    assert "treant-client token set alpha" in r.text and "not be shown again" in r.text
    assert tokens.verify_token(fresh_db, token) is not None
    listing = client.get("/tokens")
    assert secret not in listing.text and "laptop" in listing.text
    assert token.split("_")[1] in listing.text
    for _, _, detail in _audit(fresh_db, "token."):
        assert secret not in str(detail)
    assert secret not in client.cookies.get("treant_dash", "")
    assert secret not in str(session_data(client))
    assert token.split("_")[1] in str(_audit(fresh_db, "token.create"))


def test_token_actions_validated(fresh_db, client):
    r = _create(client, workspace="alpha", label="x")
    assert r.status_code == 303 and r.headers["location"] == "/tokens"
    assert "unknown action" in " ".join(_flashes(client))
    _create(client, workspace="alpha", label="x", actions=["fly"])
    assert tokens.list_tokens(fresh_db, "alpha") == []
    _create(client, workspace="alpha", label="  ", actions=["read"])
    _create(client, workspace="alpha", label="x" * 101, actions=["read"])
    _create(client, workspace="nosuch", label="x", actions=["read"])
    assert tokens.list_tokens(fresh_db, "alpha") == []
    assert _audit(fresh_db, "token.create") == []


def test_token_create_and_revoke_need_recent_login(fresh_db, client):
    _stale(client)
    r = _post(client, "/tokens", workspace="alpha", label="x", actions=["read"])
    assert r.status_code == 303 and r.headers["location"].startswith("/login?fresh=1")
    tok = owner.create_token(fresh_db, "cli", "alpha", "l", {"read"})
    tid = tok.split("_")[1]
    r = _post(client, f"/tokens/{tid}/revoke")
    assert r.headers["location"].startswith("/login?fresh=1")
    client.get("/login")
    client.get("/auth/callback")
    r = _post(client, f"/tokens/{tid}/revoke")
    assert r.status_code == 303
    assert tokens.verify_token(fresh_db, tok) is None
    assert _audit(fresh_db, "token.revoke")[0][2] == {"token_id": tid}
    _post(client, "/tokens/nothexid/revoke")
    assert _flashes(client)


def _create(c, **data):
    """Create a token as the browser does: render /tokens (one-shot nonce), then submit."""
    c.get("/tokens")
    return _post(c, "/tokens", nonce=session_data(c).get("token_nonce", ""), **data)


def test_token_create_resubmission_is_refused(fresh_db, client):
    client.get("/tokens")
    s = session_data(client)
    form = {
        "csrf": s["csrf"],
        "nonce": s["token_nonce"],
        "workspace": "alpha",
        "label": "mac",
        "actions": ["read"],
    }
    assert client.post("/tokens", data=form).status_code == 200
    r = client.post("/tokens", data=form)  # the browser re-sends the same POST
    assert r.status_code == 303 and r.headers["location"] == "/tokens"
    assert _flashes(client)[-1] == "form already used; create again"
    r = _post(client, "/tokens", workspace="alpha", label="mac", actions=["read"])  # no nonce
    assert r.status_code == 303 and _flashes(client)[-1] == "form already used; create again"
    assert len(tokens.list_tokens(fresh_db, "alpha")) == 1
    assert len(_audit(fresh_db, "token.create")) == 1


def test_token_create_logs_no_token(fresh_db, client, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    r = _create(client, workspace="alpha", label="laptop", actions=["read"])
    token = next(w for w in r.text.replace('"', " ").split() if w.startswith("trt_"))
    assert token.split("_", 2)[2] not in caplog.text


def test_token_create_on_archived_workspace_is_refused(fresh_db, client):
    owner.archive_workspace(fresh_db, "cli", "gamma")
    r = _create(client, workspace="gamma", label="x", actions=["read"])
    assert r.status_code == 303 and _flashes(client)[-1] == "workspace not available"
    assert tokens.list_tokens(fresh_db, "gamma") == []


def test_archive_twice_is_a_flash(fresh_db, client):
    _post(client, "/workspaces/gamma/archive", confirm="gamma")
    r = _post(client, "/workspaces/gamma/archive", confirm="gamma")
    assert r.status_code == 303 and _flashes(client)[-1] == "workspace not available"
    assert len(_audit(fresh_db, "workspace.archive")) == 1


def test_unchanged_non_empty_rules_are_no_changes(fresh_db, client):
    rules = {**FORM, "domains": "a.example.com", "terms": "secret-word\r\nother term"}
    _post(client, "/workspaces/alpha/settings", **rules)
    n = len(_audit(fresh_db, "workspace.settings"))
    _post(client, "/workspaces/alpha/settings", **rules)
    assert _flashes(client)[-1] == "no changes"
    assert len(_audit(fresh_db, "workspace.settings")) == n


def test_target_kinds_come_from_names():
    from treant import cli, names
    from treant.dashboard.routes import workspaces

    assert cli.TARGET_KINDS is names.TARGET_KINDS is workspaces.TARGET_KINDS
