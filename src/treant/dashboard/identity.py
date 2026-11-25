"""Who is logging in: the ``IdentityProvider`` protocol and its OIDC implementation (Authelia,
Authentik).

The app only sees an ``Identity``; tokens from the provider never leave ``complete`` (they are
not stored in the session, logged or returned).
"""

import logging
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from authlib.integrations.starlette_client import OAuth
from starlette.requests import Request
from starlette.responses import Response

from treant.dashboard.config import DashboardSettings

log = logging.getLogger("treant.dashboard.identity")

MAX_USERNAME = 128
_STATE_PREFIX = "_state_oidc_"


@dataclass(frozen=True)
class Identity:
    username: str
    groups: frozenset[str]
    auth_time: int


class LoginFailed(Exception):
    """The provider flow failed or returned something unusable. The message is a category."""


class IdentityProvider(Protocol):
    async def login_redirect(self, request: Request, redirect_uri: str, fresh: bool) -> Response:
        """Start a login: a redirect to the provider. ``fresh`` demands a new authentication
        (re-auth for a sensitive action) instead of reusing the provider's SSO session."""
        ...

    async def complete(self, request: Request) -> Identity:
        """Finish the login on the callback request; raises ``LoginFailed``."""
        ...

    async def end_session_url(self) -> str | None:
        """Where to send the browser after a local sign-out so the provider's session ends too,
        or None (sign out locally only). Never raises."""
        ...


def _clear_flow_state(request: Request) -> None:
    for key in [k for k in request.session if k.startswith(_STATE_PREFIX)]:
        request.session.pop(key, None)


class NoProvider:
    """The provider of a dashboard without login (``TREANT_DASHBOARD_AUTH=none``). The login
    routes never call it; if they did, the login fails rather than letting anyone in."""

    async def login_redirect(self, request: Request, redirect_uri: str, fresh: bool) -> Response:
        raise LoginFailed("no provider")

    async def complete(self, request: Request) -> Identity:
        raise LoginFailed("no provider")

    async def end_session_url(self) -> str | None:
        return None


class OidcProvider:
    """Authorization-code flow with PKCE (S256) against an OIDC provider, via authlib.

    authlib keeps the state, nonce and PKCE verifier in the session between the redirect and the
    callback; ``complete`` removes them whatever the outcome. ``authorize_access_token`` checks
    the state, exchanges the code (``client_secret_post``) and validates the ID token (signature
    from JWKS, issuer, audience, expiry, nonce). ``auth_time`` comes from the ID token; the
    username and groups come from userinfo (Authelia keeps the ID token minimal; Authentik puts
    them there from its ``profile`` scope), whose ``sub`` must match the ID token's. The
    discovery document's ``issuer`` must equal the configured issuer exactly (authlib checks the
    ID token's ``iss`` against the discovery document).

    Discovery is at ``<issuer without trailing slash>/.well-known/openid-configuration``: for
    Authelia ``https://auth.<domain>/.well-known/...``, for Authentik (issuer
    ``https://auth.<domain>/application/o/<slug>/``) ``.../application/o/<slug>/.well-known/...``.
    A fresh login sends ``prompt=login`` and ``max_age=0``; Authentik 2026.8 acts on
    ``prompt=login`` only (it ignores ``max_age=0``), Authelia on both. Either way the app
    refuses an ``auth_time`` older than the re-auth start.
    """

    def __init__(self, settings: DashboardSettings, *, transport: Any = None) -> None:
        """``transport`` (an httpx2 transport) replaces the network; tests pass a mock."""
        client_kwargs: dict[str, Any] = {
            "scope": "openid profile email groups",
            "code_challenge_method": "S256",
            "token_endpoint_auth_method": "client_secret_post",
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        self._issuer = settings.issuer
        self._origin = settings.issuer_origin
        self._oauth = OAuth()
        discovery = settings.issuer.rstrip("/") + "/.well-known/openid-configuration"
        self._client: Any = self._oauth.register(
            "oidc",
            client_id=settings.client_id,
            client_secret=settings.client_secret,
            server_metadata_url=discovery,
            client_kwargs=client_kwargs,
        )

    async def _check_issuer(self) -> None:
        metadata = await self._client.load_server_metadata()
        if metadata.get("issuer") != self._issuer:
            raise LoginFailed("issuer mismatch")

    async def end_session_url(self) -> str | None:
        """The discovery document's ``end_session_endpoint`` when it is https on the issuer's
        own origin; None otherwise or when discovery fails (sign-out still works locally)."""
        try:
            await self._check_issuer()
            url = (await self._client.load_server_metadata()).get("end_session_endpoint")
        except Exception as e:  # noqa: BLE001 - local sign-out must not fail on the provider
            log.warning("end session lookup failed: %s", type(e).__name__)
            return None
        if not isinstance(url, str):
            return None
        parts = urlsplit(url)
        if parts.scheme != "https" or f"https://{(parts.netloc or '').lower()}" != self._origin:
            return None
        return url

    async def login_redirect(self, request: Request, redirect_uri: str, fresh: bool) -> Response:
        extra: dict[str, str] = {"max_age": "0", "prompt": "login"} if fresh else {}
        try:
            await self._check_issuer()
            return await self._client.authorize_redirect(request, redirect_uri, **extra)
        except LoginFailed:
            raise
        except Exception as e:  # noqa: BLE001 - fail closed, log the category only
            log.warning("login redirect failed: %s", type(e).__name__)
            raise LoginFailed("provider") from None

    async def complete(self, request: Request) -> Identity:
        try:
            return await self._complete(request)
        except LoginFailed:
            raise
        except Exception as e:  # noqa: BLE001 - fail closed, log the category only
            # Only the exception type: messages from below may quote tokens or responses.
            log.warning("login failed: %s", type(e).__name__)
            raise LoginFailed("provider") from None
        finally:
            _clear_flow_state(request)

    async def _complete(self, request: Request) -> Identity:
        await self._check_issuer()
        token = await self._client.authorize_access_token(request)
        # authlib validates the ID token only when there is one; "userinfo" holds its claims.
        if "id_token" not in token:
            raise LoginFailed("no id token")
        claims = token.get("userinfo")
        if not claims:
            raise LoginFailed("no id token")
        auth_time = claims.get("auth_time")
        if type(auth_time) is not int:
            raise LoginFailed("no auth_time")
        info = await self._client.userinfo(token=token)
        if not claims.get("sub") or info.get("sub") != claims.get("sub"):
            raise LoginFailed("subject mismatch")
        username = info.get("preferred_username")
        groups = info.get("groups")
        if not isinstance(username, str) or not 0 < len(username) <= MAX_USERNAME:
            raise LoginFailed("no username")
        if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
            raise LoginFailed("no groups")
        return Identity(username, frozenset(groups), auth_time)


# The name the provider had when Authelia was the only one.
AutheliaProvider = OidcProvider
