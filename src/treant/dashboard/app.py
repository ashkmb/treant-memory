"""The dashboard Starlette app: login, logout, home, health; page modules plug in via
``ROUTE_MODULES`` (each a ``routes(deps) -> list[BaseRoute]``)."""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio.from_thread
import jinja2
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from treant import browse, owner
from treant.dashboard import security
from treant.dashboard.config import DashboardSettings
from treant.dashboard.identity import MAX_USERNAME, IdentityProvider, LoginFailed
from treant.dashboard.routes import insight, memory, ops, queue, workspaces
from treant.db import Database
from treant.embed import Embedder

log = logging.getLogger("treant.dashboard")

HERE = Path(__file__).parent
SESSION_COOKIE = "treant_dash"
SESSION_MAX_AGE = security.SESSION_MAX_AGE
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
}
UNGUARDED_PATHS = ("/healthz",)  # the container healthcheck calls 127.0.0.1


@dataclass(frozen=True)
class Deps:
    """What every route module gets: the database, the embedder, settings and templates."""

    db: Database
    embedder: Embedder
    settings: DashboardSettings
    templates: Jinja2Templates


RouteModule = Callable[[Deps], list[BaseRoute]]

# Page modules added by later tasks: ``from treant.dashboard.routes import queue`` and append
# ``queue.routes``. build_dashboard always includes them.
ROUTE_MODULES: list[RouteModule] = [
    memory.routes,
    queue.routes,
    ops.routes,
    workspaces.routes,
    insight.routes,
]


class SecurityHeadersMiddleware:
    """Adds the CSP, framing, referrer and nosniff headers to every HTTP response.

    It is the outermost user middleware, so it also turns an exception that escaped everything
    below into a plain 500 "internal error" carrying these headers (Starlette's own error
    middleware sits outside and would answer without them). Logged by type only."""

    def __init__(self, app: ASGIApp, issuer_origin: str) -> None:
        self.app = app
        # form-action also covers the redirects that follow a form POST: a sensitive action
        # with an old login answers 303 to /login, which redirects to the login provider.
        # Without its origin here the browser silently drops that navigation. Origin only: a
        # CSP source with a path (Authentik's issuer) would not match the authorize endpoint.
        sources = " ".join(filter(None, ("'self'", issuer_origin)))
        self.headers = {
            "Content-Security-Policy": (
                f"default-src 'self'; form-action {sources}; "
                "frame-ancestors 'none'; "
                "base-uri 'none'"
            ),
            **SECURITY_HEADERS,
        }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = MutableHeaders(scope=message)
                for name, value in self.headers.items():
                    headers[name] = value
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception as e:
            log.error("dashboard request failed: %s", type(e).__name__)
            if started:
                raise  # the response is under way: nothing to answer, let the server close it
            response = PlainTextResponse("internal error", status_code=500)
            await response(scope, receive, send_with_headers)


class HostCheckMiddleware:
    """400 unless the Host header (lower case, port stripped) is the host of ``public_url``.
    Blocks DNS rebinding and keeps the session cookie to one origin."""

    def __init__(self, app: ASGIApp, host: str) -> None:
        self.app = app
        self.host = host

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] not in UNGUARDED_PATHS:
            raw = Headers(scope=scope).get("host", "").strip().lower()
            name = raw.split("]")[0] + "]" if raw.startswith("[") else raw.split(":")[0]
            if name != self.host:
                await PlainTextResponse("bad request", status_code=400)(scope, receive, send)
                return
        await self.app(scope, receive, send)


SHORT = 200


def short(value: Any, n: int = SHORT) -> str:
    """Server-side truncation for lists; the result is still escaped when rendered."""
    text = str(value or "")
    return text if len(text) <= n else text[:n] + "..."


def when(value: Any) -> str:
    """A timestamp to the minute, in UTC; anything that is not a datetime is shown as is."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(UTC)
        return value.strftime("%Y-%m-%d %H:%M UTC")
    return str(value or "")


def _templates() -> Jinja2Templates:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(HERE / "templates"),
        autoescape=True,  # every template, every variable; nothing is marked safe
        undefined=jinja2.StrictUndefined,
    )

    env.filters["short"] = short
    env.filters["when"] = when

    def context(request: Request) -> dict[str, Any]:
        return {
            "csrf": security.csrf_token(request),
            "can_sign_out": request.app.state.deps.settings.auth != "none",
            "user": security.session_user(request),
            "flashes": security.pop_flashes(request),
        }

    return Jinja2Templates(env=env, context_processors=[context])


def _audit(db: Database, actor: str, action: str, detail: dict[str, Any]) -> None:
    with db.admin_tx() as conn:
        owner.audit(conn, actor, action, detail)


_REFUSAL_TEXT = {
    "not_owner": "this account may not use the dashboard",
    "not_in_group": "this account may not use the dashboard",
    "reauth_not_confirmed": "re-authentication not confirmed",
}


def _auth_routes(deps: Deps, provider: IdentityProvider) -> list[BaseRoute]:
    settings = deps.settings

    async def login(request: Request) -> Response:
        nxt = security.safe_next(request.query_params.get("next"))
        if settings.auth == "none":
            return security.redirect(nxt)  # nobody logs in: go straight back
        fresh = request.query_params.get("fresh") == "1"
        request.session["next"] = nxt
        if fresh:
            request.session["reauth_started"] = int(security.now())
        else:
            request.session.pop("reauth_started", None)
        try:
            return await provider.login_redirect(request, settings.redirect_uri, fresh)
        except LoginFailed:
            return security.error_page(request, 502, "login provider unavailable")

    async def callback(request: Request) -> Response:
        if settings.auth == "none":
            return PlainTextResponse("not found", status_code=404)
        session = request.session
        if "next" not in session:
            # No login in flight from this browser (a forged or replayed callback): touch
            # neither the session nor the audit.
            return PlainTextResponse("no login in progress", status_code=400)
        nxt = security.safe_next(session.pop("next", None))
        reauth_started = session.pop("reauth_started", None)
        try:
            ident = await provider.complete(request)
            if not 0 < len(ident.username) <= MAX_USERNAME:
                raise LoginFailed("username length")
        except LoginFailed:
            # A failed provider round trip says nothing about who holds this browser: an
            # existing login stays (only a refused identity drops it, below).
            await run_in_threadpool(
                _audit, deps.db, "dashboard", "dashboard.login_refused", {"reason": "provider"}
            )
            return security.error_page(request, 403, "login failed")

        username = ident.username
        actor = security.actor(username)
        reason = None
        if username not in settings.owners:
            reason = "not_owner"
        elif settings.group not in ident.groups:
            reason = "not_in_group"
        elif reauth_started is not None and ident.auth_time < reauth_started:
            reason = "reauth_not_confirmed"
        if reason is not None:
            if reason != "reauth_not_confirmed":
                # Someone who is not an owner: drop any login this browser held.
                security.drop_login(session)
                security.rotate_csrf(request)
            await run_in_threadpool(
                _audit,
                deps.db,
                actor,
                "dashboard.login_refused",
                {"username": username, "reason": reason},
            )
            return security.error_page(request, 403, _REFUSAL_TEXT[reason])

        if (
            reauth_started is None
            and security.now() - ident.auth_time > SESSION_MAX_AGE - settings.reauth_s
        ):
            # The provider's SSO session would leave less than the recent-login window of our
            # absolute session lifetime (or none: logged out at once, and a loop), so ask for a
            # fresh authentication.
            return security.redirect(security.login_url(nxt, fresh=True))

        # Audit first: if the write fails, nobody is logged in.
        await run_in_threadpool(
            _audit,
            deps.db,
            actor,
            "dashboard.login",
            {"username": username, "fresh": reauth_started is not None},
        )
        flashes = session.get("flash")
        session.clear()
        session["user"] = username
        # A provider clock ahead of ours must not stretch the recent-login window.
        session["auth_time"] = min(ident.auth_time, int(security.now()))
        security.rotate_csrf(request)
        if flashes:
            session["flash"] = flashes
        return security.redirect(nxt)

    @security.action(back="/")
    def logout(request: Request, deps: Deps, user: str, form: Any) -> Response:
        if deps.settings.auth == "none":
            return security.redirect("/")  # there is no login to end
        _audit(deps.db, security.actor(user), "dashboard.logout", {})
        request.session.clear()
        # End the provider's session as well: otherwise the next click logs straight back in
        # through its SSO cookie and "Sign out" seems to do nothing.
        end = anyio.from_thread.run(provider.end_session_url)
        if end:
            return RedirectResponse(end, status_code=303)
        return deps.templates.TemplateResponse(
            request,
            "error.html",
            {"title": "Signed out", "message": "You are signed out of the dashboard."},
        )

    return [
        Route("/login", login),
        Route("/auth/callback", callback),
        Route("/logout", logout, methods=["POST"]),
    ]


@security.page()
def home(request: Request, deps: Deps, user: str) -> Response:
    rows = browse.workspaces(deps.db)
    totals = {
        key: sum(r[key] for r in rows)
        for key in ("queue_open", "quarantined", "jobs_dead", "tokens_active")
    }
    return deps.templates.TemplateResponse(
        request, "home.html", {"workspaces": rows, "totals": totals}
    )


async def healthz(request: Request) -> Response:
    return PlainTextResponse("ok")


def build_dashboard(
    db: Database,
    embedder: Embedder,
    settings: DashboardSettings,
    provider: IdentityProvider,
    extra_routes: Sequence[RouteModule] = (),
) -> Starlette:
    deps = Deps(db, embedder, settings, _templates())
    routes: list[BaseRoute] = [
        Route("/healthz", healthz),
        Route("/", home),
        *_auth_routes(deps, provider),
        Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
    ]
    for module in (*ROUTE_MODULES, *extra_routes):
        routes.extend(module(deps))
    app = Starlette(
        routes=routes,
        middleware=[
            Middleware(SecurityHeadersMiddleware, issuer_origin=settings.issuer_origin),
            Middleware(HostCheckMiddleware, host=settings.host),
            Middleware(
                SessionMiddleware,
                secret_key=settings.session_secret,
                session_cookie=SESSION_COOKIE,
                max_age=SESSION_MAX_AGE,
                same_site="lax",
                https_only=True,
            ),
        ],
    )
    app.state.deps = deps
    return app
