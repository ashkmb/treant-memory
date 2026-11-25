"""Dashboard with TREANT_DASHBOARD_AUTH=none: no login provider, one fixed owner, CSRF kept."""

import re

import pytest
from dashboard_fakes import DASH_ENV, PUBLIC_URL, FakeProvider
from fakes import FakeEmbedder
from starlette.responses import HTMLResponse
from starlette.routing import Route
from starlette.testclient import TestClient

NOAUTH_ENV = {
    "TREANT_DASHBOARD_URL": PUBLIC_URL,
    "TREANT_DASHBOARD_SESSION_SECRET": DASH_ENV["TREANT_DASHBOARD_SESSION_SECRET"],
    "TREANT_DASHBOARD_OWNERS": "alice",
    "TREANT_DASHBOARD_AUTH": "none",
}


def _settings(**overrides):
    from treant.dashboard.config import DashboardSettings

    return DashboardSettings.from_env({**NOAUTH_ENV, **overrides})


def _routes(deps):
    from treant.dashboard.security import action, flash, page, redirect

    @page(sensitive=True)
    def secret(request, deps, user):
        return HTMLResponse(f"held text for {user}")

    @action(sensitive=True, back="/test/secret")
    def danger(request, deps, user, form):
        flash(request, "ok", f"done by {user}")
        return redirect("/test/secret")

    return [
        Route("/test/secret", secret),
        Route("/test/danger", danger, methods=["POST"]),
    ]


@pytest.fixture
def client(db):
    from treant.dashboard.app import build_dashboard

    app = build_dashboard(db, FakeEmbedder(), _settings(), FakeProvider(), extra_routes=[_routes])
    with TestClient(app, base_url=PUBLIC_URL, follow_redirects=False) as c:
        yield c


def _csrf(client) -> str:
    html = client.get("/").text
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def test_settings_need_no_oidc_values():
    s = _settings()
    assert s.auth == "none"
    assert s.issuer == ""
    assert s.owner == "alice"


def test_settings_none_mode_needs_exactly_one_owner():
    with pytest.raises(ValueError, match="exactly one"):
        _settings(TREANT_DASHBOARD_OWNERS="alice, other")


def test_settings_unknown_auth_mode_is_refused():
    with pytest.raises(ValueError, match="TREANT_DASHBOARD_AUTH"):
        _settings(TREANT_DASHBOARD_AUTH="maybe")


def test_oidc_mode_still_needs_the_issuer():
    from treant.dashboard.config import DashboardSettings

    env = {k: v for k, v in DASH_ENV.items() if k != "TREANT_OIDC_ISSUER"}
    with pytest.raises(ValueError, match="TREANT_OIDC_ISSUER"):
        DashboardSettings.from_env(env)


def test_pages_open_without_login_as_the_owner(client):
    r = client.get("/test/secret")
    assert r.status_code == 200
    assert r.text == "held text for alice"
    home = client.get("/")
    assert home.status_code == 200
    assert "Sign out" not in home.text


def test_sensitive_action_runs_without_reauth_but_needs_csrf(client):
    assert client.post("/test/danger", data={"csrf": "wrong"}).status_code == 403
    r = client.post("/test/danger", data={"csrf": _csrf(client)})
    assert r.status_code == 303
    assert r.headers["location"] == "/test/secret"


def test_login_and_callback_do_not_reach_a_provider(client):
    r = client.get("/login", params={"next": "/test/secret"})
    assert (r.status_code, r.headers["location"]) == (303, "/test/secret")
    assert client.get("/auth/callback").status_code == 404


def test_csp_allows_forms_to_this_origin_only(client):
    csp = client.get("/").headers["content-security-policy"]
    assert "form-action 'self';" in csp


def test_audit_actor_is_the_owner(client, db):
    from treant.dashboard.security import actor

    assert actor("alice") == "dashboard:alice"
    r = client.post("/logout", data={"csrf": _csrf(client)})
    assert (r.status_code, r.headers["location"]) == (303, "/")
