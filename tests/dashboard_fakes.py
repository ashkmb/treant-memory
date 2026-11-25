"""Test doubles and helpers for the dashboard tests."""

import base64
import json

from starlette.responses import RedirectResponse

PUBLIC_URL = "https://treant-ui.test"
SESSION_SECRET = "s" * 40

DASH_ENV = {
    "TREANT_DASHBOARD_URL": PUBLIC_URL,
    "TREANT_OIDC_ISSUER": "https://auth.test",
    "TREANT_OIDC_CLIENT_SECRET": "client-secret-value",
    "TREANT_DASHBOARD_SESSION_SECRET": SESSION_SECRET,
    "TREANT_DASHBOARD_OWNERS": "alice, other",
}


def dash_settings(**overrides):
    from treant.dashboard.config import DashboardSettings

    return DashboardSettings.from_env({**DASH_ENV, **overrides})


class FakeProvider:
    """An ``IdentityProvider`` returning a configurable identity.

    ``auth_time=None`` means "authenticated just now" (``security.now()`` at callback time).
    ``fail=True`` makes ``complete`` raise ``LoginFailed``. ``fresh_calls`` records the
    ``fresh`` flag of each ``login_redirect``.
    """

    def __init__(
        self, username="alice", groups=("treant",), auth_time=None, fail=False, end_session=None
    ):
        self.username = username
        self.groups = frozenset(groups)
        self.auth_time = auth_time
        self.fail = fail
        self.end_session = end_session
        self.fresh_calls: list[bool] = []
        self.redirect_uris: list[str] = []

    async def login_redirect(self, request, redirect_uri, fresh):
        self.fresh_calls.append(fresh)
        self.redirect_uris.append(redirect_uri)
        return RedirectResponse("https://auth.test/authorize", status_code=302)

    async def end_session_url(self):
        return self.end_session

    async def complete(self, request):
        from treant.dashboard import security
        from treant.dashboard.identity import Identity, LoginFailed

        if self.fail:
            raise LoginFailed("provider")
        at = int(security.now()) if self.auth_time is None else self.auth_time
        return Identity(self.username, self.groups, at)


def session_data(client) -> dict:
    """Decode the (signed, not encrypted) Starlette session cookie of a TestClient."""
    raw = client.cookies.get("treant_dash")
    if not raw:
        return {}
    payload = raw.split(".")[0]
    return json.loads(base64.b64decode(payload + "=" * (-len(payload) % 4)))


def set_session(client, data: dict) -> None:
    """Replace a TestClient's session cookie with ``data``, signed like SessionMiddleware."""
    from itsdangerous import TimestampSigner

    payload = base64.b64encode(json.dumps(data).encode())
    value = TimestampSigner(SESSION_SECRET).sign(payload).decode()
    client.cookies.set("treant_dash", value, domain="treant-ui.test", path="/")
