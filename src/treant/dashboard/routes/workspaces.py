"""Workspace, target and token pages.

Creating and purging a workspace stay CLI-only (shown as text). A token is shown once, on the
response of its creation, and is never put in a flash, the session, a log line or the audit."""

import hmac
import secrets
from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from treant import browse, names, owner, settings, tokens
from treant.dashboard.security import action, actor, error_page, flash, page, redirect
from treant.errors import InvalidInput
from treant.names import TARGET_KINDS

MAX_LABEL = 100
MAX_TARGET = 500
RULES = ("domains", "ticket_prefixes", "terms")
TOKEN_ACTIONS = ("create", "read", "update", "delete")
NONCE_KEY = "token_nonce"  # one-shot: set when /tokens renders the form, used up by a create
USED_FORM = "form already used; create again"


def _workspace(deps: Any, name: str) -> dict | None:
    try:
        names.validate_workspace(name)
    except ValueError:
        return None
    return browse.workspace(deps.db, name)


def _lines(raw: Any) -> list[str]:
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    return [line.strip() for line in text.split("\n") if line.strip()]


@page()
def workspace_list(request: Request, deps: Any, user: str) -> Response:
    return deps.templates.TemplateResponse(
        request, "workspaces.html", {"workspaces": browse.workspaces(deps.db)}
    )


@page()
def workspace_detail(request: Request, deps: Any, user: str) -> Response:
    ws = _workspace(deps, request.path_params["ws"])
    if ws is None:
        return error_page(request, 404, "not found")
    return deps.templates.TemplateResponse(
        request,
        "workspace.html",
        {
            "ws": ws,
            "s": ws["settings"] or settings.dump(settings.WorkspaceSettings()),
            "invalid_settings": ws["settings"] is None,
            "targets": browse.targets(deps.db, ws["name"]),
            "kinds": TARGET_KINDS,
            "backends": settings.BACKENDS,
            "modes": settings.PUBLISH_MODES,
            "active": ws["status"] == "active",
        },
    )


@action(sensitive=True, back="/workspaces/{ws}")
def settings_save(request: Request, deps: Any, user: str, form: Any) -> Response:
    ws = request.path_params["ws"]
    back = f"/workspaces/{ws}"
    info = _workspace(deps, ws)
    if info is None or info["status"] != "active":
        raise owner.Refused("workspace not available")
    reset = bool(form.get("reset"))
    wanted: dict[str, Any] = {
        key: str(form.get(key) or "").strip() for key in ("backend", "alias", "judge_alias")
    }
    wanted["publish"] = str(form.get("publish") or "").strip()
    for rule in RULES:
        wanted[f"rules.{rule}"] = _lines(form.get(rule))
    if reset:
        changes: dict[str, Any] = {}
    else:
        try:
            current = settings.dump(settings.load(deps.db, ws))
        except InvalidInput:
            flash(request, "error", "stored settings are invalid; tick reset to defaults")
            return redirect(back)
        flat = {k: current[k] for k in ("backend", "alias", "judge_alias", "publish")}
        flat.update({f"rules.{r}": current["rules"][r] for r in RULES})
        changes = {k: v for k, v in wanted.items() if v != flat[k]}
        if not changes:
            flash(request, "ok", "no changes")
            return redirect(back)
    done = owner.set_workspace_settings(deps.db, actor(user), ws, changes, reset=reset)
    flash(request, "ok", "reset to defaults" if reset else "saved: " + ", ".join(done))
    return redirect(back)


def _target(request: Request, deps: Any, form: Any) -> tuple[str, str, str]:
    ws = request.path_params["ws"]
    info = _workspace(deps, ws)
    if info is None or info["status"] != "active":
        raise owner.Refused("workspace not available")
    kind = str(form.get("kind") or "")
    value = str(form.get("value") or "").strip()
    if kind not in TARGET_KINDS:
        raise InvalidInput("unknown target kind")
    if not value or len(value) > MAX_TARGET:
        raise InvalidInput("target value length")
    return ws, kind, value


@action(sensitive=True, back="/workspaces/{ws}")
def target_add(request: Request, deps: Any, user: str, form: Any) -> Response:
    ws, kind, value = _target(request, deps, form)
    owner.add_target(deps.db, actor(user), ws, kind, value)
    flash(request, "ok", "target added")
    return redirect(f"/workspaces/{ws}")


@action(sensitive=True, back="/workspaces/{ws}")
def target_remove(request: Request, deps: Any, user: str, form: Any) -> Response:
    ws, kind, value = _target(request, deps, form)
    owner.remove_target(deps.db, actor(user), ws, kind, value)
    flash(request, "ok", "target removed")
    return redirect(f"/workspaces/{ws}")


@action(sensitive=True, back="/workspaces/{ws}")
def archive(request: Request, deps: Any, user: str, form: Any) -> Response:
    ws = request.path_params["ws"]
    if str(form.get("confirm") or "") != ws:
        flash(request, "error", "type the workspace name to confirm")
        return redirect(f"/workspaces/{ws}")
    revoked = owner.archive_workspace(deps.db, actor(user), ws)
    flash(request, "ok", f"archived; {revoked} token(s) revoked")
    return redirect(f"/workspaces/{ws}")


@page()
def token_list(request: Request, deps: Any, user: str) -> Response:
    all_ws = browse.workspaces(deps.db)
    rows = [t for w in all_ws for t in tokens.list_tokens(deps.db, w["name"])]
    rows.sort(key=lambda t: (t["created_at"], t["id"]), reverse=True)
    nonce = secrets.token_urlsafe(16)  # not the token: it only makes the form one-shot
    request.session[NONCE_KEY] = nonce
    return deps.templates.TemplateResponse(
        request,
        "tokens.html",
        {
            "tokens": rows,
            "active": [w["name"] for w in all_ws if w["status"] == "active"],
            "token_actions": TOKEN_ACTIONS,
            "nonce": nonce,
        },
    )


def _nonce_ok(request: Request, form: Any) -> bool:
    """Use up the session's form nonce; True only when the form carried that same nonce, so a
    re-sent POST (refresh, back button) cannot mint a second live token."""
    expected = request.session.pop(NONCE_KEY, None)
    given = form.get("nonce")
    if not isinstance(expected, str) or not expected or not isinstance(given, str):
        return False
    return hmac.compare_digest(expected.encode(), given.encode())


@action(sensitive=True, back="/tokens")
def token_create(request: Request, deps: Any, user: str, form: Any) -> Response:
    if not _nonce_ok(request, form):
        flash(request, "error", USED_FORM)
        return redirect("/tokens")
    ws = str(form.get("workspace") or "")
    label = str(form.get("label") or "").strip()
    if not 1 <= len(label) <= MAX_LABEL:
        raise InvalidInput("label length")
    actions = {str(a) for a in form.getlist("actions")}
    token = owner.create_token(deps.db, actor(user), ws, label, actions)
    response = deps.templates.TemplateResponse(
        request,
        "token_created.html",
        {"token": token, "ws": ws, "label": label, "actions": sorted(actions)},
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


@action(sensitive=True, back="/tokens")
def token_revoke(request: Request, deps: Any, user: str, form: Any) -> Response:
    owner.revoke_token(deps.db, actor(user), str(request.path_params["id"]))
    flash(request, "ok", "token revoked")
    return redirect("/tokens")


def routes(deps: Any) -> list[BaseRoute]:
    post = ["POST"]
    return [
        Route("/workspaces", workspace_list),
        Route("/workspaces/{ws}", workspace_detail),
        Route("/workspaces/{ws}/settings", settings_save, methods=post),
        Route("/workspaces/{ws}/targets/add", target_add, methods=post),
        Route("/workspaces/{ws}/targets/remove", target_remove, methods=post),
        Route("/workspaces/{ws}/archive", archive, methods=post),
        Route("/tokens", token_list),
        Route("/tokens", token_create, methods=post),
        Route("/tokens/{id}/revoke", token_revoke, methods=post),
    ]
