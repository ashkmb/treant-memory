"""Static bearer-token ASGI middleware.

Deliberately not the mcp library's AuthSettings: that path advertises OAuth metadata and a
WWW-Authenticate challenge, which would send clients into an OAuth login. A failure here is a
bare ``401 unauthorized`` with nothing for a client to follow.
"""

import logging
from contextvars import ContextVar

from anyio import to_thread
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from starlette.types import ASGIApp, Receive, Scope, Send

from treant.db import Database
from treant.tokens import TokenContext, verify_token

log = logging.getLogger(__name__)

SCOPE_KEY = "treant_token"

current_token: ContextVar[TokenContext] = ContextVar("treant_current_token")


def current() -> TokenContext:
    """The verified token of the request being served; raises LookupError outside one."""
    return current_token.get()


def _bearer(scope: Scope) -> str | None:
    value = None
    for name, raw in scope.get("headers", []):
        if name == b"authorization":
            value = raw.decode("latin-1")
            break
    if value is None:
        return None
    scheme, sep, token = value.partition(" ")
    if not sep or scheme.lower() != "bearer" or not token or token != token.strip():
        return None
    return token


async def _deny(send: Send) -> None:
    body = b"unauthorized"
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"text/plain"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BearerMiddleware:
    def __init__(self, app: ASGIApp, db: Database) -> None:
        self.app = app
        self.db = db

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == "/healthz":
            await self.app(scope, receive, send)
            return
        raw = _bearer(scope)
        ctx = None
        if raw is not None:
            try:
                ctx = await to_thread.run_sync(verify_token, self.db, raw)
            except Exception as e:  # noqa: BLE001 - fail closed, log the category only
                log.error("token verification failed category=%s", type(e).__name__)
        if ctx is None:
            await _deny(send)
            return
        # Also on the scope: MCP handlers can run in a task whose context predates this request.
        scope.setdefault("state", {})[SCOPE_KEY] = ctx
        # The mcp session manager binds a session to scope["user"]; without it every session's
        # owner is None and any valid token could use or end another token's session.
        scope["user"] = AuthenticatedUser(
            AccessToken(token=ctx.token_id, client_id=ctx.token_id, scopes=sorted(ctx.actions))
        )
        reset = current_token.set(ctx)
        try:
            await self.app(scope, receive, send)
        finally:
            current_token.reset(reset)
