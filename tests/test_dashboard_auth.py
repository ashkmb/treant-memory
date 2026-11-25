"""Dashboard skeleton: settings, OIDC login, sessions, CSRF, recent login, headers."""

import re
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from dashboard_fakes import (
    DASH_ENV,
    PUBLIC_URL,
    FakeProvider,
    dash_settings,
    session_data,
    set_session,
)
from fakes import FakeEmbedder
from starlette.responses import HTMLResponse
from starlette.routing import Route
from starlette.testclient import TestClient

T0 = 1_800_000_000


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def _test_routes(deps):
    """Test-only routes: a page, a sensitive action and view, an action that refuses, a page
    registered for POST too, an action whose ``back`` names a missing parameter, and a raw
    route that raises."""
    from treant.dashboard.security import action, flash, page, redirect
    from treant.owner import Refused

    @page()
    def plain(request, deps, user):
        return HTMLResponse("test page")

    @page()
    def both(request, deps, user):
        return HTMLResponse("page ran")

    @action(back="/x/{missing}")
    def badback(request, deps, user, form):
        return HTMLResponse("action ran")

    async def boom(request):
        raise RuntimeError("secret detail")

    async def stream_boom(request):
        from starlette.responses import StreamingResponse

        async def body():
            yield b"partial"
            raise RuntimeError("mid-stream")

        return StreamingResponse(body())

    @action(sensitive=True, back="/test/page")
    def danger(request, deps, user, form):
        flash(request, "ok", f"done by {user}")
        return redirect("/")

    @page(sensitive=True)
    def secret(request, deps, user):
        return HTMLResponse("held text")

    @action(back="/test/back/{n}")
    def refuse(request, deps, user, form):
        raise Refused("<b>not open</b>")

    return [
        Route("/test/page", plain),
        Route("/test/both", both, methods=["GET", "POST"]),
        Route("/test/badback/{n}", badback, methods=["POST"]),
        Route("/test/boom", boom),
        Route("/test/stream-boom", stream_boom),
        Route("/test/danger", danger, methods=["POST"]),
        Route("/test/secret", secret),
        Route("/test/refuse/{n}", refuse, methods=["POST"]),
    ]


@pytest.fixture
def clock(monkeypatch):
    from treant.dashboard import security

    c = Clock(float(T0))
    monkeypatch.setattr(security, "now", c)
    return c


@pytest.fixture
def fake():
    return FakeProvider()


def _client(db, fake, base_url=PUBLIC_URL):
    from treant.dashboard.app import build_dashboard

    app = build_dashboard(db, FakeEmbedder(), dash_settings(), fake, extra_routes=[_test_routes])
    return TestClient(app, base_url=base_url, follow_redirects=False)


@pytest.fixture
def client(db, fake, clock):
    with _client(db, fake) as c:
        yield c


def _login(client, **params):
    """A login as the browser does it: ``/login`` (sets ``next``), then the callback."""
    client.get("/login", params=params)
    return client.get("/auth/callback")


@pytest.fixture
def logged_in(client, fake):
    r = _login(client)
    assert r.status_code == 303
    fake.fresh_calls.clear()
    return client


def _audit_mark(db) -> int:
    with db.admin_tx() as c:
        return c.execute("SELECT coalesce(max(id), 0) FROM control.audit").fetchone()[0]


def _audit_since(db, mark: int) -> list[tuple]:
    with db.admin_tx() as c:
        return c.execute(
            "SELECT actor, action, detail FROM control.audit WHERE id > %s ORDER BY id", (mark,)
        ).fetchall()


def _csrf(client) -> str:
    return session_data(client)["csrf"]


# --- login, session, owner checks -------------------------------------------------------


def test_unauthenticated_page_redirects_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=%2F")


def test_owner_login_sets_session_and_audits(client, fake, db):
    mark = _audit_mark(db)
    r = client.get("/login", params={"next": "/queue?status=dead"})
    assert r.status_code == 302 and r.headers["location"].startswith("https://auth.test/")
    assert fake.fresh_calls == [False]
    assert fake.redirect_uris == [PUBLIC_URL + "/auth/callback"]

    r = client.get("/auth/callback")
    assert r.status_code == 303 and r.headers["location"] == "/queue?status=dead"
    s = session_data(client)
    assert s["user"] == "alice" and s["auth_time"] == T0 and len(s["csrf"]) >= 32
    assert set(s) <= {"user", "auth_time", "csrf", "reauth_started", "next", "flash"}
    assert "next" not in s and "reauth_started" not in s
    rows = _audit_since(db, mark)
    assert [(a, act) for a, act, _ in rows] == [("dashboard:alice", "dashboard.login")]

    home = client.get("/")
    assert home.status_code == 200
    assert "alpha" in home.text and "beta" in home.text
    assert f'name="csrf" value="{s["csrf"]}"' in home.text


def test_login_rotates_csrf(logged_in):
    before = _csrf(logged_in)
    _login(logged_in)
    assert _csrf(logged_in) != before


@pytest.mark.parametrize(
    ("username", "groups", "reason"),
    [("mallory", ("treant",), "not_owner"), ("alice", ("admins",), "not_in_group")],
)
def test_non_owner_or_missing_group_is_refused(client, fake, db, username, groups, reason):
    fake.username, fake.groups = username, frozenset(groups)
    mark = _audit_mark(db)
    r = _login(client)
    assert r.status_code == 403
    assert "user" not in session_data(client)
    rows = _audit_since(db, mark)
    assert len(rows) == 1
    _, act, detail = rows[0]
    assert act == "dashboard.login_refused"
    assert detail == {"username": username, "reason": reason}
    assert client.get("/").status_code == 303


def test_refusal_clears_an_existing_login(logged_in, fake):
    fake.username = "mallory"
    assert _login(logged_in).status_code == 403
    assert "user" not in session_data(logged_in)


def test_provider_failure_is_403_and_audited_as_provider(client, fake, db):
    fake.fail = True
    mark = _audit_mark(db)
    r = _login(client)
    assert r.status_code == 403 and "login failed" in r.text
    rows = _audit_since(db, mark)
    assert [(act, d) for _, act, d in rows] == [("dashboard.login_refused", {"reason": "provider"})]


@pytest.mark.parametrize("value", ["//evil.com", "https://evil.com", "/\\evil.com", "javascript:x"])
def test_next_must_be_local(client, value):
    client.get("/login", params={"next": value})
    r = client.get("/auth/callback")
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_safe_next():
    from treant.dashboard.security import safe_next

    assert safe_next(None) == "/"
    assert safe_next("") == "/"
    assert safe_next("/queue/3?x=1") == "/queue/3?x=1"
    for bad in ["//x", "/\\x", "\\x", "x", "/a\\b", "/a\nb", "https://x/", " /x", "/" + "a" * 3000]:
        assert safe_next(bad) == "/", bad


# --- CSRF, host, headers --------------------------------------------------------------


def test_post_without_csrf_is_403(logged_in):
    assert logged_in.post("/logout").status_code == 403
    assert logged_in.post("/logout", data={"csrf": "wrong"}).status_code == 403
    assert session_data(logged_in)["user"] == "alice"


def test_logout_with_csrf_clears_session_and_audits(logged_in, db):
    mark = _audit_mark(db)
    r = logged_in.post("/logout", data={"csrf": _csrf(logged_in)})
    assert r.status_code == 200 and "signed out" in r.text.lower()
    assert "user" not in session_data(logged_in)
    assert [(a, act) for a, act, _ in _audit_since(db, mark)] == [
        ("dashboard:alice", "dashboard.logout")
    ]


def test_wrong_host_is_400(db, fake, clock):
    with _client(db, fake, base_url="https://other.test") as c:
        assert c.get("/").status_code == 400
        assert c.get("/login").status_code == 400
        assert c.get("/healthz").status_code == 200


def test_security_headers(logged_in):
    for r in (logged_in.get("/"), logged_in.get("/healthz"), logged_in.post("/logout")):
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "same-origin"
        assert r.headers["content-security-policy"] == (
            "default-src 'self'; form-action 'self' https://auth.test; frame-ancestors 'none'; "
            "base-uri 'none'"
        )


def test_session_cookie_flags(logged_in):
    r = _login(logged_in)
    cookie = r.headers["set-cookie"].lower()
    assert cookie.startswith("treant_dash=")
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    assert "max-age=28800" in cookie


def test_static_css_is_served_and_self_contained(client):
    r = client.get("/static/app.css")
    assert r.status_code == 200 and "text/css" in r.headers["content-type"]
    # Compiled Tailwind + daisyUI: no remote imports or fonts, which the CSP would block.
    assert ".btn" in r.text and "@import" not in r.text
    urls = re.findall(r"url\(\s*['\"]?([^'\")]*)", r.text)
    assert all(u.startswith(("data:", "#", "%23")) for u in urls), urls


def test_pages_link_the_compiled_stylesheet(logged_in):
    assert '<link rel="stylesheet" href="/static/app.css">' in logged_in.get("/").text


# --- recent login --------------------------------------------------------------------


def test_stale_login_sensitive_route_redirects_to_fresh_login(logged_in, fake, clock):
    clock.advance(301)
    r = logged_in.post("/test/danger", data={"csrf": _csrf(logged_in)})
    assert r.status_code == 303
    assert r.headers["location"] == "/login?fresh=1&next=%2Ftest%2Fpage"
    r = logged_in.get(r.headers["location"])
    assert r.status_code == 302
    assert fake.fresh_calls == [True]
    assert session_data(logged_in)["reauth_started"] == T0 + 301

    # The provider confirms a fresh login: the owner re-submits and it goes through.
    r = logged_in.get("/auth/callback")
    assert r.status_code == 303 and r.headers["location"] == "/test/page"
    assert "reauth_started" not in session_data(logged_in)
    page = logged_in.get(r.headers["location"])  # `next` is a GET page, not the POST URL
    assert page.status_code == 200 and "test page" in page.text
    r = logged_in.post("/test/danger", data={"csrf": _csrf(logged_in)})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "done by alice" in logged_in.get("/").text


def test_recent_login_passes_within_window(logged_in, clock):
    clock.advance(300)
    r = logged_in.post("/test/danger", data={"csrf": _csrf(logged_in)})
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_sensitive_action_checks_csrf_before_anything(logged_in, clock):
    assert logged_in.post("/test/danger", data={"csrf": "nope"}).status_code == 403


def test_unauthenticated_post_redirects_to_login(client):
    r = client.post("/test/danger", data={"csrf": "x"})
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Ftest%2Fpage"


def test_reauth_with_old_auth_time_is_refused(logged_in, fake, clock, db):
    clock.advance(400)
    logged_in.get("/login", params={"fresh": "1", "next": "/test/page"})
    fake.auth_time = T0  # the provider did not re-prompt: auth_time predates the re-auth start
    mark = _audit_mark(db)
    r = logged_in.get("/auth/callback")
    assert r.status_code == 403 and "re-authentication not confirmed" in r.text
    s = session_data(logged_in)
    assert s["user"] == "alice" and s["auth_time"] == T0 and "reauth_started" not in s
    rows = _audit_since(db, mark)
    assert [(act, d) for _, act, d in rows] == [
        ("dashboard.login_refused", {"username": "alice", "reason": "reauth_not_confirmed"})
    ]
    # Still logged in, still not recent.
    assert logged_in.get("/").status_code == 200
    r = logged_in.post("/test/danger", data={"csrf": _csrf(logged_in)})
    assert r.headers["location"].startswith("/login?fresh=1")


def test_sensitive_view_is_no_store_and_needs_recent_login(logged_in, clock):
    r = logged_in.get("/test/secret")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    clock.advance(301)
    r = logged_in.get("/test/secret", params={"a": "1"})
    assert r.status_code == 303
    assert r.headers["location"] == "/login?fresh=1&next=%2Ftest%2Fsecret%3Fa%3D1"
    assert "held text" not in r.text


def test_future_auth_time_is_clamped_to_now(client, fake, clock):
    fake.auth_time = T0 + 10_000
    _login(client)
    assert session_data(client)["auth_time"] == T0


# --- refusals become flashes ------------------------------------------------------------


def test_invalid_input_becomes_escaped_flash_and_redirect_back(logged_in):
    r = logged_in.post("/test/refuse/7", data={"csrf": _csrf(logged_in)})
    assert r.status_code == 303 and r.headers["location"] == "/test/back/7"
    home = logged_in.get("/")
    assert "&lt;b&gt;not open&lt;/b&gt;" in home.text and "<b>not open" not in home.text
    assert "not open" not in logged_in.get("/").text  # shown once


# --- settings ---------------------------------------------------------------------------


def test_settings_from_env_defaults():
    s = dash_settings()
    assert s.public_url == PUBLIC_URL and s.bind == "0.0.0.0:8001"
    assert s.client_id == "treant" and s.group == "treant" and s.reauth_s == 300
    assert s.owners == frozenset({"alice", "other"})
    assert s.issuer == "https://auth.test"


def test_settings_repr_hides_secrets():
    s = dash_settings()
    assert "client-secret-value" not in repr(s)
    assert "s" * 40 not in repr(s)
    assert "client-secret-value" not in str(s)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"TREANT_DASHBOARD_URL": ""}, "missing required setting TREANT_DASHBOARD_URL"),
        ({"TREANT_OIDC_ISSUER": ""}, "missing required setting TREANT_OIDC_ISSUER"),
        ({"TREANT_OIDC_CLIENT_SECRET": ""}, "missing required setting TREANT_OIDC_CLIENT_SECRET"),
        ({"TREANT_DASHBOARD_OWNERS": " , "}, "TREANT_DASHBOARD_OWNERS"),
        ({"TREANT_DASHBOARD_URL": "http://x.test"}, "TREANT_DASHBOARD_URL"),
        ({"TREANT_DASHBOARD_SESSION_SECRET": "short"}, "TREANT_DASHBOARD_SESSION_SECRET"),
        ({"TREANT_DASHBOARD_REAUTH_S": "abc"}, "TREANT_DASHBOARD_REAUTH_S"),
    ],
)
def test_settings_validation(overrides, message):
    from treant.dashboard.config import DashboardSettings

    with pytest.raises(ValueError, match=message) as e:
        DashboardSettings.from_env({**DASH_ENV, **overrides})
    assert "client-secret-value" not in str(e.value)


# --- the real provider against a mocked Authelia / Authentik -----------------------------

AUTHELIA_ISSUER = "https://auth.test"
# Authentik: one issuer per application, with a trailing slash; discovery below it; the
# authorize, token and userinfo endpoints are shared by all applications.
AUTHENTIK_ISSUER = "https://auth.test/application/o/treant/"


def _mock_idp(issuer: str, discovery: str, endpoints: dict) -> dict:
    """A mocked OIDC provider behind an ``httpx2.MockTransport`` (authlib 1.9 talks httpx2,
    which respx does not patch): discovery, JWKS, token (an ID token signed by a fresh RSA key)
    and userinfo, each answered only at its exact URL. ``state["claims"]`` /
    ``state["userinfo"]`` override what is returned (``None`` drops a claim);
    ``state["nonce"]`` is read from the authorize URL by ``_start``; every token request is
    kept in ``state["token_requests"]``."""
    import httpx2
    from joserfc import jwt
    from joserfc.jwk import RSAKey

    key = RSAKey.generate_key(2048, parameters={"kid": "k1"})
    state: dict = {"claims": {}, "userinfo": {}, "token_requests": [], "key": key}
    state["sign"] = lambda header, claims: jwt.encode(header, claims, key)
    metadata = {
        "issuer": issuer,
        **endpoints,
        "id_token_signing_alg_values_supported": ["RS256"],
    }

    def token() -> dict:
        now = int(time.time())
        claims = {
            "iss": issuer,
            "aud": ["treant"],
            "sub": "sub-alice",
            "iat": now,
            "exp": now + 300,
            "auth_time": now,
            "nonce": state["nonce"],
        }
        claims.update(state["claims"])
        claims = {k: v for k, v in claims.items() if v is not None}
        body = {
            "access_token": "access-token-value",
            "token_type": "Bearer",
            "expires_in": 3600,
            "id_token": state["sign"]({"alg": "RS256", "kid": "k1"}, claims),
        }
        if state.get("no_id_token"):
            del body["id_token"]
        if state.get("body_userinfo"):
            # Unsigned claims smuggled into the token response itself.
            body["userinfo"] = {"sub": "sub-alice", "auth_time": now, "nonce": state["nonce"]}
        return body

    def userinfo(request) -> dict:
        assert request.headers["authorization"] == "Bearer access-token-value"
        body = {"sub": "sub-alice", "preferred_username": "alice", "groups": ["treant"]}
        body.update(state["userinfo"])
        return body

    def handler(request):
        url = str(request.url)
        if request.method == "GET" and url == discovery:
            return httpx2.Response(200, json={**metadata, **state.get("metadata", {})})
        if request.method == "GET" and url == endpoints["jwks_uri"]:
            return httpx2.Response(200, json={"keys": [key.as_dict(private=False)]})
        if request.method == "POST" and url == endpoints["token_endpoint"]:
            state["token_requests"].append(request.read().decode())
            return httpx2.Response(200, json=token())
        if request.method == "GET" and url == endpoints["userinfo_endpoint"]:
            return httpx2.Response(200, json=userinfo(request))
        return httpx2.Response(404)

    state["transport"] = httpx2.MockTransport(handler)
    return state


@pytest.fixture
def authelia():
    """Authelia's shape: the issuer is an origin (no trailing slash), everything below it."""
    i = AUTHELIA_ISSUER
    return _mock_idp(
        i,
        f"{i}/.well-known/openid-configuration",
        {
            "authorization_endpoint": f"{i}/api/oidc/authorization",
            "token_endpoint": f"{i}/api/oidc/token",
            "userinfo_endpoint": f"{i}/api/oidc/userinfo",
            "jwks_uri": f"{i}/jwks.json",
        },
    )


@pytest.fixture
def authentik():
    """Authentik 2026.8's shape (provider ``issuer_mode`` per_provider, the default)."""
    return _mock_idp(
        AUTHENTIK_ISSUER,
        f"{AUTHENTIK_ISSUER}.well-known/openid-configuration",
        {
            "authorization_endpoint": "https://auth.test/application/o/authorize/",
            "token_endpoint": "https://auth.test/application/o/token/",
            "userinfo_endpoint": "https://auth.test/application/o/userinfo/",
            "jwks_uri": f"{AUTHENTIK_ISSUER}jwks/",
            "end_session_endpoint": f"{AUTHENTIK_ISSUER}end-session/",
        },
    )


def _authelia_client(db, authelia):
    from treant.dashboard.app import build_dashboard
    from treant.dashboard.identity import AutheliaProvider

    s = dash_settings()
    provider = AutheliaProvider(s, transport=authelia["transport"])
    app = build_dashboard(db, FakeEmbedder(), s, provider)
    return TestClient(app, base_url=PUBLIC_URL, follow_redirects=False)


def _authentik_client(db, idp, issuer=AUTHENTIK_ISSUER):
    from treant.dashboard.app import build_dashboard
    from treant.dashboard.identity import OidcProvider

    s = dash_settings(TREANT_OIDC_ISSUER=issuer)
    provider = OidcProvider(s, transport=idp["transport"])
    app = build_dashboard(db, FakeEmbedder(), s, provider)
    return TestClient(app, base_url=PUBLIC_URL, follow_redirects=False)


def _start(c, authelia, **params):
    r = c.get("/login", params=params)
    assert r.status_code == 302
    loc = urlsplit(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(loc.query).items()}
    authelia["nonce"] = q["nonce"]
    return loc, q


def test_authelia_provider_flow(db, authelia):
    with _authelia_client(db, authelia) as c:
        loc, q = _start(c, authelia, fresh="1", next="/queue")
        assert (
            f"{loc.scheme}://{loc.netloc}{loc.path}" == "https://auth.test/api/oidc/authorization"
        )
        assert q["client_id"] == "treant" and q["response_type"] == "code"
        assert q["redirect_uri"] == PUBLIC_URL + "/auth/callback"
        assert set(q["scope"].split()) == {"openid", "profile", "email", "groups"}
        assert q["code_challenge_method"] == "S256" and q["code_challenge"]
        assert q["max_age"] == "0" and q["prompt"] == "login"

        r = c.get("/auth/callback", params={"code": "the-code", "state": q["state"]})
        assert r.status_code == 303 and r.headers["location"] == "/queue"
        form = parse_qs(authelia["token_requests"][-1])
        assert form["client_secret"] == ["client-secret-value"]
        assert form["code_verifier"][0] and form["code"] == ["the-code"]

        s = session_data(c)
        assert s["user"] == "alice"
        assert not [k for k in s if k.startswith("_state_")]
        raw = c.cookies.get("treant_dash")
        assert "access-token-value" not in str(s) and "access-token-value" not in raw

        # A normal (not fresh) login does not ask for a new prompt.
        _, q2 = _start(c, authelia)
        assert "max_age" not in q2 and "prompt" not in q2


@pytest.mark.parametrize(
    "tamper",
    [
        {"claims": {"auth_time": None}},
        {"claims": {"auth_time": "123"}},
        {"claims": {"nonce": "other-nonce"}},
        {"claims": {"aud": ["someone-else"]}},
        {"userinfo": {"sub": "sub-other"}},
        {"userinfo": {"preferred_username": None}},
        {"userinfo": {"groups": "treant"}},
    ],
)
def test_authelia_provider_fails_closed(db, authelia, tamper):
    with _authelia_client(db, authelia) as c:
        _, q = _start(c, authelia)
        authelia["claims"].update(tamper.get("claims", {}))
        authelia["userinfo"].update(tamper.get("userinfo", {}))
        r = c.get("/auth/callback", params={"code": "c", "state": q["state"]})
        assert r.status_code == 403 and "login failed" in r.text
        s = session_data(c)
        assert "user" not in s
        assert not [k for k in s if k.startswith("_state_")]


def test_authelia_callback_with_unknown_state_fails(db, authelia):
    with _authelia_client(db, authelia) as c:
        _start(c, authelia)
        r = c.get("/auth/callback", params={"code": "c", "state": "forged"})
        assert r.status_code == 403
        s = session_data(c)
        assert "user" not in s
        assert not [k for k in s if k.startswith("_state_")]  # the real flow's state is gone too
        assert authelia["token_requests"] == []


# --- CLI ----------------------------------------------------------------------------------


def test_cli_dashboard_runs_uvicorn_on_bind(monkeypatch, login_dsn, prefix, db):
    import uvicorn
    from starlette.applications import Starlette

    from treant import cli
    from treant.dashboard import identity

    seen = {}

    class Recording(identity.OidcProvider):
        def __init__(self, settings, **kw):
            seen["provider_settings"] = settings
            super().__init__(settings, **kw)

    monkeypatch.setattr(identity, "OidcProvider", Recording)

    def fake_run(app, **kwargs):
        seen["app"], seen["kwargs"] = app, kwargs

    monkeypatch.setattr(uvicorn, "run", fake_run)
    for k, v in {
        **DASH_ENV,
        "TREANT_DATABASE_URL": login_dsn,
        "TREANT_ROLE_PREFIX": prefix,
        "TREANT_EMBED_URL": "http://127.0.0.1:1",
        "TREANT_DASHBOARD_BIND": "127.0.0.1:8123",
    }.items():
        monkeypatch.setenv(k, v)
    assert cli.main(["dashboard"]) == 0
    assert isinstance(seen["app"], Starlette)
    # no access log: the login callback's query string carries the OIDC code and state
    assert seen["kwargs"] == {"host": "127.0.0.1", "port": 8123, "access_log": False}
    assert seen["provider_settings"].public_url == PUBLIC_URL
    assert not hasattr(seen["app"].state, "provider")


def test_cli_dashboard_missing_setting_names_it(monkeypatch, capsys, login_dsn):
    from treant import cli

    for k, v in {**DASH_ENV, "TREANT_DATABASE_URL": login_dsn, "TREANT_EMBED_URL": "x"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("TREANT_OIDC_CLIENT_SECRET")
    assert cli.main(["dashboard"]) == 2
    err = capsys.readouterr().err
    assert "missing required setting TREANT_OIDC_CLIENT_SECRET" in err
    assert "s" * 40 not in err


# --- review round 1 ---------------------------------------------------------------------


class BrokenDb:
    """A database whose every transaction fails (the audit write cannot happen)."""

    def admin_tx(self):
        import psycopg

        raise psycopg.OperationalError("connection refused to secret-host")


_HEADERS = {
    "content-security-policy": (
        "default-src 'self'; form-action 'self' https://auth.test; frame-ancestors 'none'; "
        "base-uri 'none'"
    ),
    "x-frame-options": "DENY",
    "referrer-policy": "same-origin",
}


def _has_security_headers(r) -> bool:
    return all(r.headers.get(k) == v for k, v in _HEADERS.items())


def test_unhandled_exception_is_500_with_security_headers(client):
    r = client.get("/test/boom")
    assert r.status_code == 500 and r.text == "internal error"
    assert _has_security_headers(r)
    assert "secret detail" not in r.text


def test_callback_audit_failure_is_500_with_headers_and_no_login(fake, clock):
    from treant.dashboard.app import build_dashboard

    app = build_dashboard(BrokenDb(), FakeEmbedder(), dash_settings(), fake)
    with TestClient(app, base_url=PUBLIC_URL, follow_redirects=False) as c:
        r = _login(c)
        assert r.status_code == 500 and _has_security_headers(r)
        assert "secret-host" not in r.text
        assert "user" not in session_data(c)


def test_page_refuses_methods_other_than_get_and_head(logged_in):
    assert logged_in.get("/test/both").status_code == 200
    assert logged_in.head("/test/both").status_code == 200
    r = logged_in.post("/test/both", data={"csrf": _csrf(logged_in)})
    assert r.status_code == 405 and "page ran" not in r.text


def test_action_requires_back():
    from treant.dashboard.security import action

    with pytest.raises(TypeError):
        action()  # type: ignore[call-arg]


def test_action_back_with_missing_parameter_falls_back_to_root(client):
    r = client.post("/test/badback/3", data={"csrf": "x"})
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2F"


def test_session_without_auth_time_is_logged_out(client):
    set_session(client, {"user": "alice", "csrf": "c" * 43})
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")
    assert "user" not in session_data(client)


def test_session_older_than_eight_hours_is_logged_out(logged_in, clock):
    clock.advance(8 * 3600)
    assert logged_in.get("/").status_code == 200
    clock.advance(1)
    r = logged_in.get("/")
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")
    assert "user" not in session_data(logged_in) and "auth_time" not in session_data(logged_in)


def test_provider_auth_time_older_than_session_lifetime_forces_fresh_login(client, fake, db):
    fake.auth_time = T0 - 8 * 3600 - 1  # the provider's SSO session is older than 8 h
    mark = _audit_mark(db)
    r = _login(client, next="/test/page")
    assert r.status_code == 303 and r.headers["location"] == "/login?fresh=1&next=%2Ftest%2Fpage"
    assert "user" not in session_data(client)
    assert _audit_since(db, mark) == []
    client.get(r.headers["location"])
    assert fake.fresh_calls[-1] is True
    fake.auth_time = None  # the provider re-authenticated
    r = client.get("/auth/callback")
    assert r.status_code == 303 and r.headers["location"] == "/test/page"
    assert session_data(client)["user"] == "alice"


def test_callback_without_login_in_flight_is_400_and_changes_nothing(logged_in, fake, db):
    fake.username = "mallory"
    before = session_data(logged_in)
    mark = _audit_mark(db)
    r = logged_in.get("/auth/callback")
    assert r.status_code == 400
    assert session_data(logged_in) == before
    assert _audit_since(db, mark) == []


def test_every_page_and_logout_is_no_store(logged_in):
    assert logged_in.get("/").headers["cache-control"] == "no-store"
    assert logged_in.get("/test/page").headers["cache-control"] == "no-store"
    r = logged_in.post("/logout", data={"csrf": _csrf(logged_in)})
    assert r.headers["cache-control"] == "no-store"


def test_overlong_username_is_a_login_failure(client, fake, db):
    fake.username = "a" * 200
    mark = _audit_mark(db)
    r = _login(client)
    assert r.status_code == 403 and "login failed" in r.text
    assert [(act, d) for _, act, d in _audit_since(db, mark)] == [
        ("dashboard.login_refused", {"reason": "provider"})
    ]


def test_settings_direct_construction_is_validated_and_normalised():
    from treant.dashboard.config import DashboardSettings

    base = {"client_secret": "c", "session_secret": "s" * 40, "owners": frozenset({"alice"})}
    s = DashboardSettings(public_url="https://x.test/", issuer="https://auth.test/", **base)
    assert s.public_url == "https://x.test" and s.issuer == "https://auth.test"
    assert s.redirect_uri == "https://x.test/auth/callback"
    for bad in (
        {"public_url": "http://x.test", "issuer": "https://auth.test"},
        {"public_url": "https://x.test", "issuer": "http://auth.test"},
        {"public_url": "https://x.test/sub", "issuer": "https://auth.test"},
    ):
        with pytest.raises(ValueError):
            DashboardSettings(**bad, **base)


def _b64(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _sign_none(header, claims) -> str:
    import json

    h = _b64(json.dumps({"alg": "none", "kid": "k1"}).encode())
    return f"{h}.{_b64(json.dumps(claims).encode())}."


def _sign_hs256_with_public_key(state):
    def sign(header, claims) -> str:
        import hashlib
        import hmac
        import json

        secret = state["key"].as_pem(private=False)
        h = _b64(json.dumps({"alg": "HS256", "kid": "k1"}).encode())
        p = _b64(json.dumps(claims).encode())
        mac = hmac.new(secret, f"{h}.{p}".encode(), hashlib.sha256).digest()
        return f"{h}.{p}.{_b64(mac)}"

    return sign


def _sign_with_other_key(header, claims) -> str:
    from joserfc import jwt
    from joserfc.jwk import RSAKey

    return jwt.encode(header, claims, RSAKey.generate_key(2048, parameters={"kid": "k1"}))


@pytest.mark.parametrize(
    "case",
    [
        "other_key",
        "wrong_iss",
        "expired",
        "alg_none",
        "hs256_public_key",
        "no_id_token",
        "userinfo_in_token_body",
    ],
)
def test_authelia_id_token_tampering_fails_closed(db, authelia, case):
    now = int(time.time())
    with _authelia_client(db, authelia) as c:
        _, q = _start(c, authelia)
        if case == "other_key":
            authelia["sign"] = _sign_with_other_key
        elif case == "wrong_iss":
            authelia["claims"]["iss"] = "https://evil.test"
        elif case == "expired":
            authelia["claims"].update({"iat": now - 3600, "exp": now - 600})
        elif case == "alg_none":
            authelia["sign"] = _sign_none
        elif case == "hs256_public_key":
            authelia["sign"] = _sign_hs256_with_public_key(authelia)
        elif case == "no_id_token":
            authelia["no_id_token"] = True
        elif case == "userinfo_in_token_body":
            authelia["no_id_token"] = authelia["body_userinfo"] = True
        r = c.get("/auth/callback", params={"code": "c", "state": q["state"]})
        assert r.status_code == 403 and "login failed" in r.text
        assert "user" not in session_data(c)


def test_authelia_discovery_issuer_must_match_settings(db, authelia):
    authelia["metadata"] = {"issuer": "https://evil.test"}
    with _authelia_client(db, authelia) as c:
        r = c.get("/login")
        assert r.status_code == 502
        assert not r.headers.get("location")


# --- final review fixes ---------------------------------------------------------------------


def test_exception_after_response_started_is_reraised(client):
    # Headers already went out: nothing can be answered, so the error is not swallowed.
    with pytest.raises(RuntimeError, match="mid-stream"):
        client.get("/test/stream-boom")


def test_provider_auth_time_leaving_less_than_reauth_window_forces_fresh_login(client, fake):
    fake.auth_time = T0 - (8 * 3600 - 299)  # under 300 s of session lifetime would be left
    r = _login(client, next="/test/page")
    assert r.status_code == 303 and r.headers["location"] == "/login?fresh=1&next=%2Ftest%2Fpage"
    assert "user" not in session_data(client)
    fake.auth_time = T0 - (8 * 3600 - 300)  # exactly the window left: accepted
    r = _login(client, next="/test/page")
    assert r.status_code == 303 and r.headers["location"] == "/test/page"


def test_provider_failure_keeps_an_existing_login(logged_in, fake):
    before = session_data(logged_in)
    fake.fail = True
    assert _login(logged_in).status_code == 403
    assert session_data(logged_in)["user"] == "alice"
    assert session_data(logged_in)["auth_time"] == before["auth_time"]
    assert logged_in.get("/test/page").status_code == 200


def test_when_filter_shortens_timestamps():
    from datetime import UTC, datetime, timedelta, timezone

    from treant.dashboard.app import when

    assert when(datetime(2026, 11, 3, 13, 29, 3, 766852, tzinfo=UTC)) == "2026-11-03 13:29 UTC"
    cet = timezone(timedelta(hours=1))
    assert when(datetime(2026, 11, 3, 14, 29, tzinfo=cet)) == "2026-11-03 13:29 UTC"
    assert when(None) == "" and when("") == ""
    assert when("not a date") == "not a date"


# --- Authentik (per-application issuer with a path and a trailing slash) --------------------


def test_authentik_provider_flow(db, authentik):
    with _authentik_client(db, authentik) as c:
        loc, q = _start(c, authentik, fresh="1", next="/queue")
        assert (
            f"{loc.scheme}://{loc.netloc}{loc.path}" == "https://auth.test/application/o/authorize/"
        )
        assert set(q["scope"].split()) == {"openid", "profile", "email", "groups"}
        assert q["code_challenge_method"] == "S256" and q["code_challenge"]
        assert q["prompt"] == "login"

        r = c.get("/auth/callback", params={"code": "the-code", "state": q["state"]})
        assert r.status_code == 303 and r.headers["location"] == "/queue"
        assert session_data(c)["user"] == "alice"
        assert not [k for k in session_data(c) if k.startswith("_state_")]


@pytest.mark.parametrize("where", ["discovery", "id_token", "settings"])
def test_authentik_issuer_must_match_exactly(db, authentik, where):
    """The trailing slash is part of Authentik's issuer: dropping it anywhere is a mismatch."""
    stripped = AUTHENTIK_ISSUER.rstrip("/")
    if where == "discovery":
        authentik["metadata"] = {"issuer": stripped}
    with _authentik_client(
        db, authentik, stripped if where == "settings" else AUTHENTIK_ISSUER
    ) as c:
        if where in ("discovery", "settings"):
            r = c.get("/login")
            assert r.status_code == 502 and not r.headers.get("location")
            return
        _, q = _start(c, authentik)
        authentik["claims"]["iss"] = stripped
        r = c.get("/auth/callback", params={"code": "c", "state": q["state"]})
        assert r.status_code == 403 and "login failed" in r.text
        assert "user" not in session_data(c)


@pytest.mark.parametrize(
    ("issuer", "origin"),
    [
        (AUTHENTIK_ISSUER, "https://auth.test"),
        ("https://Auth.test:8443/application/o/treant/", "https://auth.test:8443"),
        (AUTHELIA_ISSUER, "https://auth.test"),
    ],
)
def test_csp_form_action_is_the_issuer_origin_only(db, fake, clock, issuer, origin):
    from treant.dashboard.app import build_dashboard

    s = dash_settings(TREANT_OIDC_ISSUER=issuer)
    app = build_dashboard(db, FakeEmbedder(), s, fake)
    with TestClient(app, base_url=PUBLIC_URL, follow_redirects=False) as c:
        r = c.get("/healthz")
    assert r.headers["content-security-policy"] == (
        f"default-src 'self'; form-action 'self' {origin}; frame-ancestors 'none'; base-uri 'none'"
    )


def test_settings_accept_a_path_issuer_and_keep_it_exactly():
    s = dash_settings(TREANT_OIDC_ISSUER=AUTHENTIK_ISSUER)
    assert s.issuer == AUTHENTIK_ISSUER and s.issuer_origin == "https://auth.test"
    no_slash = AUTHENTIK_ISSUER.rstrip("/")
    assert dash_settings(TREANT_OIDC_ISSUER=no_slash).issuer == no_slash
    # An origin-only issuer (Authelia) is still normalised to no trailing slash.
    assert dash_settings(TREANT_OIDC_ISSUER="https://auth.test/").issuer == "https://auth.test"
    none = dash_settings(TREANT_DASHBOARD_AUTH="none", TREANT_DASHBOARD_OWNERS="alice")
    assert none.issuer == "" and none.issuer_origin == ""


@pytest.mark.parametrize(
    "issuer",
    [
        "http://auth.test/application/o/treant/",
        "https://auth.test/application/o/treant/?x=1",
        "https://auth.test/application/o/treant/#f",
        "https:///application/o/treant/",
        "https://user@auth.test/application/o/treant/",
    ],
)
def test_settings_refuse_a_bad_path_issuer(issuer):
    with pytest.raises(ValueError, match="TREANT_OIDC_ISSUER"):
        dash_settings(TREANT_OIDC_ISSUER=issuer)


# --- sign out ends the provider session too ----------------------------------------------


def _login_via(c, idp):
    _, q = _start(c, idp)
    r = c.get("/auth/callback", params={"code": "the-code", "state": q["state"]})
    assert r.status_code == 303 and session_data(c)["user"] == "alice"


def test_logout_sends_the_browser_to_the_provider_end_session(db, authentik):
    with _authentik_client(db, authentik) as c:
        _login_via(c, authentik)
        mark = _audit_mark(db)
        r = c.post("/logout", data={"csrf": _csrf(c)})
        assert r.status_code == 303
        assert r.headers["location"] == f"{AUTHENTIK_ISSUER}end-session/"
        assert "user" not in session_data(c)
        assert [a for _, a, _ in _audit_since(db, mark)] == ["dashboard.logout"]


def test_logout_stays_local_without_an_end_session_endpoint(db, authelia):
    with _authelia_client(db, authelia) as c:
        _login_via(c, authelia)
        r = c.post("/logout", data={"csrf": _csrf(c)})
        assert r.status_code == 200 and "signed out" in r.text.lower()
        assert "user" not in session_data(c)


def test_logout_ignores_an_end_session_endpoint_on_another_origin(db, authentik):
    authentik["metadata"] = {"end_session_endpoint": "https://evil.test/logout"}
    with _authentik_client(db, authentik) as c:
        _login_via(c, authentik)
        r = c.post("/logout", data={"csrf": _csrf(c)})
        assert r.status_code == 200 and "signed out" in r.text.lower()
        assert "user" not in session_data(c)
