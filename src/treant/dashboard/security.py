"""Session, CSRF and recent-login helpers, and the ``page`` / ``action`` handler decorators.

Session (a signed, not encrypted, cookie ``treant_dash``) keys, and nothing else of ours:

- ``user``: the owner's username, set by a successful login callback.
- ``auth_time``: when the provider last authenticated the owner (epoch seconds, int).
- ``csrf``: the per-session CSRF token (rotated on every login).
- ``reauth_started``: set by ``/login?fresh=1``; the callback refuses an ``auth_time`` older.
- ``next``: where to go after the login callback (a local path).
- ``flash``: messages to show once, as ``[[kind, text], ...]``.
- ``token_nonce``: the one-shot nonce of the token creation form (not a token).
- ``dup_hidden``: keys (16 hex) of fact duplicate groups marked "not duplicates" (at most 50).

authlib adds ``_state_oidc_<state>`` keys (state, nonce, PKCE verifier) for the length of a
login redirect; ``OidcProvider.complete`` removes them. No provider token is ever stored.

Route handlers for later pages are plain sync functions wrapped by ``page`` (GET views) or
``action`` (POST forms); the wrapper does the login, CSRF and recent-login checks, runs the
handler in a threadpool (database calls are sync), and turns ``InvalidInput`` (including
``owner.Refused`` / ``owner.Flagged``) into a flash and a redirect, or an error page::

    @page()                                  # GET, needs a login
    def queue(request, deps, user) -> Response: ...

    @page(sensitive=True)                    # GET, needs a recent login; sent no-store
    def quarantined(request, deps, user) -> Response: ...

    @action(sensitive=True, back="/queue/{id}")   # POST: login + CSRF + recent login
    def approve(request, deps, user, form) -> Response:
        owner.general_approve(deps.db, actor(user), int(request.path_params["id"]), ...)
        flash(request, "ok", "approved")
        return redirect("/queue")

    def routes(deps) -> list[BaseRoute]:
        return [Route("/queue", queue), Route("/queue/{id}/approve", approve, methods=["POST"])]
"""

import functools
import hmac
import logging
import secrets
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode, urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from treant.errors import InvalidInput

log = logging.getLogger("treant.dashboard")

# Patched by tests (``treant.dashboard.security.now``); always call it as ``security.now()``.
now = time.time

# Absolute session lifetime: the cookie's max_age, and also the oldest ``auth_time`` accepted.
SESSION_MAX_AGE = 8 * 3600
MAX_NEXT = 2000
MAX_FLASHES = 5
MAX_FLASH_TEXT = 300


class NeedsReauth(Exception):
    """The login is older than ``reauth_s``; ``next`` is the local path to return to."""

    def __init__(self, next: str) -> None:
        super().__init__("recent login required")
        self.next = next


def actor(user: str) -> str:
    """The audit actor for a dashboard user."""
    return f"dashboard:{user}"


def safe_next(value: str | None) -> str:
    """``value`` if it is a local path (one leading ``/``, no backslash, no control character,
    no scheme or host), else ``/``. Use it on every redirect target that came from a request."""
    if not isinstance(value, str) or not value or len(value) > MAX_NEXT:
        return "/"
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return "/"
    return value


def current_path(request: Request) -> str:
    """The request's path and query string, as a ``next`` value."""
    query = request.url.query
    return request.url.path + (f"?{query}" if query else "")


def login_url(next: str, fresh: bool = False) -> str:
    params = {"fresh": "1", "next": safe_next(next)} if fresh else {"next": safe_next(next)}
    return "/login?" + urlencode(params)


def redirect(path: str) -> RedirectResponse:
    """A 303 to a local path (anything else goes to ``/``)."""
    return RedirectResponse(safe_next(path), status_code=303)


def _settings(request: Request) -> Any:
    return request.app.state.deps.settings


def drop_login(session: dict) -> None:
    """Forget the login (``user`` and ``auth_time``); the rest of the session stays."""
    session.pop("user", None)
    session.pop("auth_time", None)


def session_user(request: Request) -> str | None:
    """The logged-in owner, or None. Logged out (and the login dropped from the session) when the
    user is no longer in ``owners``, or ``auth_time`` is missing or older than
    ``SESSION_MAX_AGE``: an absolute lifetime that a re-sent cookie cannot extend."""
    settings = _settings(request)
    if settings.auth == "none":
        return settings.owner
    session = request.session
    user = session.get("user")
    if user is None:
        return None
    auth_time = session.get("auth_time")
    if (
        isinstance(user, str)
        and user in _settings(request).owners
        and type(auth_time) is int
        and now() - auth_time <= SESSION_MAX_AGE
    ):
        return user
    drop_login(session)
    return None


def require_owner(request: Request) -> str | RedirectResponse:
    """The owner's username, or a 303 to ``/login?next=<this page>``."""
    user = session_user(request)
    if user is None:
        return redirect(login_url(current_path(request)))
    return user


def require_recent(request: Request, next: str | None = None) -> None:
    """Raise ``NeedsReauth(next)`` unless the login is at most ``reauth_s`` seconds old.
    ``next`` defaults to the current path (for a POST, pass the GET page to return to)."""
    if _settings(request).auth == "none":
        return
    auth_time = request.session.get("auth_time")
    if type(auth_time) is not int or now() - auth_time > _settings(request).reauth_s:
        raise NeedsReauth(next or current_path(request))


def csrf_token(request: Request) -> str:
    """The session's CSRF token, created on first use. Exposed to templates as ``csrf``."""
    token = request.session.get("csrf")
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def rotate_csrf(request: Request) -> str:
    request.session["csrf"] = secrets.token_urlsafe(32)
    return request.session["csrf"]


def csrf_ok(request: Request, form: Mapping[str, Any]) -> bool:
    """True when the form's ``csrf`` field equals the session token (constant time)."""
    expected = request.session.get("csrf")
    given = form.get("csrf")
    if not isinstance(expected, str) or not expected or not isinstance(given, str):
        return False
    return hmac.compare_digest(expected.encode(), given.encode())


def flash(request: Request, kind: str, text: str) -> None:
    """Queue a message (``kind``: ``ok`` or ``error``) shown once by ``base.html``. The text is
    escaped when rendered; keep it a category or a short summary, never a secret."""
    items = list(request.session.get("flash") or [])
    items.append([kind, str(text)[:MAX_FLASH_TEXT]])
    request.session["flash"] = items[-MAX_FLASHES:]


def pop_flashes(request: Request) -> list[dict[str, str]]:
    items = request.session.pop("flash", None) or []
    return [{"kind": str(k), "text": str(t)} for k, t in items]


def error_page(request: Request, status: int, message: str, title: str = "Error") -> Response:
    """The error template with a category message (never an exception's text)."""
    templates = request.app.state.deps.templates
    return templates.TemplateResponse(
        request, "error.html", {"title": title, "message": message}, status_code=status
    )


def _crash(request: Request, e: Exception) -> Response:
    log.error("dashboard handler failed: %s", type(e).__name__)
    return error_page(request, 500, "internal error")


Page = Callable[[Request, Any, str], Response]
Action = Callable[[Request, Any, str, Any], Response]


def page(*, sensitive: bool = False) -> Callable[[Page], Callable[[Request], Any]]:
    """Wrap a sync GET handler ``(request, deps, user) -> Response``.

    Only GET and HEAD run the handler (405 otherwise: a page has no CSRF check). No login: 303
    to ``/login?next=<path>``. ``sensitive``: a login older than ``reauth_s`` gets a 303 to
    ``/login?fresh=1&next=<path>``. Every response is sent ``Cache-Control: no-store``.
    ``InvalidInput`` from the handler renders the error page (400) with its category; any other
    exception, the error page (500) "internal error".
    """

    def wrap(fn: Page) -> Callable[[Request], Any]:
        @functools.wraps(fn)
        async def endpoint(request: Request) -> Response:
            if request.method not in ("GET", "HEAD"):
                return PlainTextResponse(
                    "method not allowed", status_code=405, headers={"Allow": "GET, HEAD"}
                )
            user = require_owner(request)
            if isinstance(user, Response):
                return user
            if sensitive:
                try:
                    require_recent(request)
                except NeedsReauth as e:
                    return redirect(login_url(e.next, fresh=True))
            try:
                response = await run_in_threadpool(fn, request, request.app.state.deps, user)
            except InvalidInput as e:
                response = error_page(request, 400, str(e))
            except Exception as e:  # noqa: BLE001 - error page, logged by type only
                response = _crash(request, e)
            response.headers["Cache-Control"] = "no-store"
            return response

        return endpoint

    return wrap


def _back_path(back: str, path_params: Mapping[str, Any]) -> str:
    try:
        return safe_next(back.format(**path_params))
    except (KeyError, IndexError, ValueError):
        return "/"


def action(*, back: str, sensitive: bool = False) -> Callable[[Action], Callable[[Request], Any]]:
    """Wrap a sync POST handler ``(request, deps, user, form) -> Response``.

    ``back`` (required) is the GET page to return to, formatted with the path parameters (for
    example ``"/queue/{id}"``); a placeholder that is not a path parameter gives ``/``. It must
    be a GET page: after a re-auth or a refusal the browser GETs it. In order: no login → 303 to
    ``/login?next=<back>``; a missing or wrong ``csrf`` field → 403; ``sensitive`` and a login
    older than ``reauth_s`` → 303 to ``/login?fresh=1&next=<back>`` (the owner re-submits after
    the re-auth; nothing is done). ``InvalidInput`` from the handler (``Refused``, ``Flagged``)
    becomes ``flash("error", <category>)`` and a 303 to ``back``. Every response is sent
    ``Cache-Control: no-store``.
    """

    def wrap(fn: Action) -> Callable[[Request], Any]:
        @functools.wraps(fn)
        async def endpoint(request: Request) -> Response:
            response = await _run(request)
            response.headers["Cache-Control"] = "no-store"
            return response

        async def _run(request: Request) -> Response:
            target = _back_path(back, request.path_params)
            user = session_user(request)
            if user is None:
                return redirect(login_url(target))
            form = await request.form()
            if not csrf_ok(request, form):
                return error_page(request, 403, "invalid form token; reload the page")
            if sensitive:
                try:
                    require_recent(request, target)
                except NeedsReauth as e:
                    return redirect(login_url(e.next, fresh=True))
            try:
                return await run_in_threadpool(fn, request, request.app.state.deps, user, form)
            except InvalidInput as e:
                flash(request, "error", str(e))
                return redirect(target)
            except Exception as e:  # noqa: BLE001 - error page, logged by type only
                return _crash(request, e)

        return endpoint

    return wrap
