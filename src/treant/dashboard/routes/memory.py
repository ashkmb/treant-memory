"""Memory pages: browse nodes, search, node detail, fact edit/retract/restore, fact duplicates,
merges, graph, conflicts, stale flags, sessions. A login and CSRF are enough, except saving or restoring
``general`` text over the rules (``force``), which needs a recent login."""

import re
from collections.abc import Callable
from functools import wraps
from typing import Any
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from treant import browse, owner, search
from treant.dashboard.graphview import diagram
from treant.dashboard.routes._common import PAGE, pager
from treant.dashboard.routes._common import offset as page_offset
from treant.dashboard.security import (
    NeedsReauth,
    action,
    actor,
    error_page,
    flash,
    login_url,
    page,
    redirect,
    require_recent,
)
from treant.errors import InvalidInput
from treant.tokens import TokenContext

NOT_FOUND = {"unknown namespace", "workspace not available"}
OVERVIEW_LIMIT = 60  # nodes on the unfiltered graph page


def _notfound(request: Request) -> Response:
    return error_page(request, 404, "not found")


def _mapped(fn: Callable[..., Response]) -> Callable[..., Response]:
    """Namespace refusals (unknown or absent) become a 404 page instead of a 400."""

    @wraps(fn)
    def inner(request: Request, deps: Any, user: str) -> Response:
        try:
            return fn(request, deps, user)
        except InvalidInput as e:
            if str(e) in NOT_FOUND:
                return _notfound(request)
            raise

    return inner


def _q(value: str) -> str:
    return quote(value, safe="")


def _render(request: Request, deps: Any, name: str, ctx: dict) -> Response:
    return deps.templates.TemplateResponse(request, name, ctx)


@page()
def index(request: Request, deps: Any, user: str) -> Response:
    return _render(request, deps, "memory_index.html", {"workspaces": browse.workspaces(deps.db)})


@page()
def sessions_index(request: Request, deps: Any, user: str) -> Response:
    """Every workspace, each linking to its sessions page."""
    return _render(request, deps, "sessions_index.html", {"workspaces": browse.workspaces(deps.db)})


@page()
@_mapped
def node_list(request: Request, deps: Any, user: str) -> Response:
    ns = request.path_params["ns"]
    q = (request.query_params.get("q") or "").strip()[:200]
    offset = page_offset(request)
    rows = browse.nodes(deps.db, ns, q or None, PAGE, offset)
    return _render(
        request,
        deps,
        "nodes.html",
        {
            "ns": ns,
            "q": q,
            "rows": rows,
            "offset": offset,
            **pager(offset, len(rows)),
        },
    )


def _search_context(deps: Any, ns: str) -> tuple[TokenContext | None, bool]:
    """The context to search ``ns`` with, and whether only general hits are kept."""
    if ns != "general":
        return TokenContext("dashboard", ns, frozenset({"read"})), False
    active = [w["name"] for w in browse.workspaces(deps.db) if w["status"] == "active"]
    if not active:
        return None, True
    return TokenContext("dashboard", active[0], frozenset({"read"})), True


@page()
@_mapped
def search_page(request: Request, deps: Any, user: str) -> Response:
    ns = request.path_params["ns"]
    browse.nodes(deps.db, ns, None, 1, 0)  # validates the namespace (404 when unknown)
    q = (request.query_params.get("q") or "").strip()[:500]
    ctx, only_general = _search_context(deps, ns)
    hits: list[dict] = []
    if q and ctx is not None:
        found = search.search(deps.db, deps.embedder, ctx, q, 20).hits
        # Hits say "workspace" or "general"; "link_ns" is the namespace name for the links.
        hits = [
            h | {"link_ns": "general" if h["namespace"] == "general" else ns}
            for h in found
            if not only_general or h["namespace"] == "general"
        ]
    return _render(
        request,
        deps,
        "search.html",
        {"ns": ns, "q": q, "hits": hits, "available": ctx is not None},
    )


@page()
@_mapped
def node_graph(request: Request, deps: Any, user: str) -> Response:
    """The whole graph (filtered by name, type, unlinked) or, with ``focus``, one node's
    neighbourhood."""
    ns, qp = request.path_params["ns"], request.query_params
    try:
        depth = int(qp.get("depth") or 1)
    except ValueError:
        depth = 1
    focus = (qp.get("focus") or "").strip()
    q = (qp.get("q") or "").strip()[:200]
    types = [t for t in qp.getlist("type") if t][:50]
    unlinked = qp.get("unlinked") == "1"
    if focus:
        g = browse.neighbourhood(deps.db, ns, focus, depth)
        if g is None:
            return _notfound(request)
    else:
        # Unfiltered, only the hubs: a few hundred nodes on one canvas is unreadable.
        limit = browse.GRAPH_LIMIT if (q or types) else OVERVIEW_LIMIT
        g = browse.graph(deps.db, ns, limit, q=q or None, types=types, unlinked=unlinked)
    return _render(
        request,
        deps,
        "graph.html",
        {"ns": ns, "d": diagram(g), "g": g, "q": q, "types": types, "unlinked": unlinked},
    )


@page()
@_mapped
def node_page(request: Request, deps: Any, user: str) -> Response:
    ns, node_id = request.path_params["ns"], request.path_params["id"]
    node = browse.node(deps.db, ns, node_id)
    if node is None:
        return _notfound(request)
    return _render(request, deps, "node.html", {"ns": ns, "node": node})


@page()
@_mapped
def fact_history(request: Request, deps: Any, user: str) -> Response:
    ns, fact_id = request.path_params["ns"], request.path_params["id"]
    versions = browse.fact_history(deps.db, ns, fact_id)
    if not versions:
        return _notfound(request)
    current = versions[-1] if versions[-1]["current"] else None
    return _render(
        request,
        deps,
        "fact_history.html",
        {"ns": ns, "fact_id": fact_id, "versions": versions, "current": current},
    )


def _fact_target(deps: Any, ns: str, fact_id: str) -> str:
    """Where a fact action returns: its node page, else its history page."""
    history = f"/memory/{_q(ns)}/fact/{_q(fact_id)}/history"
    try:
        versions = browse.fact_history(deps.db, ns, fact_id)
    except InvalidInput:
        return f"/memory/{_q(ns)}"
    if not versions:
        return f"/memory/{_q(ns)}"
    node_id = (versions[-1]["data"] or {}).get("node_id") if versions[-1]["current"] else None
    return f"/memory/{_q(ns)}/node/{_q(str(node_id))}" if node_id else history


def _run_fact_action(
    request: Request, deps: Any, fn: Callable[[], str], ok: str, ns: str, fact_id: str
) -> Response:
    target = _fact_target(deps, ns, fact_id)
    try:
        fn()
    except owner.Flagged:
        flash(request, "error", "flagged: the rules matched this text; tick the box to save anyway")
    except InvalidInput as e:
        flash(request, "error", str(e))
    else:
        flash(request, "ok", ok)
    return redirect(target)


def _forced_general(request: Request, ns: str, force: bool) -> Response | None:
    """Saving flagged text into ``general`` over the rules is a sensitive action: a stale login
    goes back through a fresh login (nothing is written), as approving a queue item does."""
    if ns != "general" or not force:
        return None
    try:
        require_recent(request, f"/memory/general/fact/{_q(request.path_params['id'])}/history")
    except NeedsReauth as e:
        return redirect(login_url(e.next, fresh=True))
    return None


def _current(deps: Any, ns: str, fact_id: str) -> dict | None:
    """The fact's current row data (``text``, ``status``...), or None."""
    try:
        versions = browse.fact_history(deps.db, ns, fact_id)
    except InvalidInput:
        return None
    return versions[-1]["data"] if versions and versions[-1]["current"] else None


@action(back="/memory/{ns}/fact/{id}/history")
def fact_edit(request: Request, deps: Any, user: str, form: Any) -> Response:
    ns, fid = request.path_params["ns"], request.path_params["id"]
    force = bool(form.get("force"))
    reauth = _forced_general(request, ns, force)
    if reauth is not None:
        return reauth
    raw = form.get("text")
    text = None if raw is None else str(raw).replace("\r\n", "\n").replace("\r", "\n")
    status = form.get("status") or None
    current = _current(deps, ns, fid)
    if current is not None:
        # The form always sends the text back: unchanged text is a status-only change, which
        # must not touch the text nor run the scrubber or the rules again.
        if text == current.get("text"):
            text = None
        if text is None and status == current.get("status"):
            flash(request, "ok", "no changes")
            return redirect(_fact_target(deps, ns, fid))
    return _run_fact_action(
        request,
        deps,
        lambda: owner.edit_fact(deps.db, actor(user), ns, fid, text, status, force),
        "fact saved",
        ns,
        fid,
    )


@action(back="/memory/{ns}/fact/{id}/history")
def fact_retract(request: Request, deps: Any, user: str, form: Any) -> Response:
    ns, fid = request.path_params["ns"], request.path_params["id"]
    return _run_fact_action(
        request,
        deps,
        lambda: owner.retract_fact(deps.db, actor(user), ns, fid),
        "fact retracted",
        ns,
        fid,
    )


@action(back="/memory/{ns}/fact/{id}/history")
def fact_restore(request: Request, deps: Any, user: str, form: Any) -> Response:
    ns, fid = request.path_params["ns"], request.path_params["id"]
    force = bool(form.get("force"))
    reauth = _forced_general(request, ns, force)
    if reauth is not None:
        return reauth
    try:
        version = int(str(form.get("version", "")))
    except ValueError:
        version = 0  # refused by owner.restore_fact as "version"
    return _run_fact_action(
        request,
        deps,
        lambda: owner.restore_fact(deps.db, actor(user), ns, fid, version, force),
        "fact restored",
        ns,
        fid,
    )


@page()
@_mapped
def merges(request: Request, deps: Any, user: str) -> Response:
    ws = request.path_params["ws"]
    return _render(request, deps, "merges.html", {"ws": ws, "rows": browse.merges(deps.db, ws)})


HIDDEN_KEY = "dup_hidden"  # session: duplicate groups the owner marked "not duplicates"
MAX_HIDDEN = 50
_GROUP_KEY = re.compile(r"[0-9a-f]{16}")


def _hidden(request: Request) -> list[str]:
    keys = request.session.get(HIDDEN_KEY)
    return [k for k in keys if isinstance(k, str)] if isinstance(keys, list) else []


@page()
@_mapped
def duplicates(request: Request, deps: Any, user: str) -> Response:
    """Groups of near-identical current facts; "Not duplicates" hides one for the session."""
    ns = request.path_params["ns"]
    hidden = set(_hidden(request))
    groups = browse.duplicates(deps.db, ns)
    shown = [g for g in groups if g["key"] not in hidden]
    return _render(
        request,
        deps,
        "duplicates.html",
        {"ns": ns, "groups": shown, "hidden": len(groups) - len(shown)},
    )


@action(back="/memory/{ns}/duplicates")
def duplicates_merge(request: Request, deps: Any, user: str, form: Any) -> Response:
    ns = request.path_params["ns"]
    back = f"/memory/{_q(ns)}/duplicates"
    ids = [str(i) for i in form.getlist("ids")][:200]
    keep = str(form.get("keep") or "")
    if not keep or keep not in ids:
        flash(request, "error", "pick the wording to keep")
        return redirect(back)
    raw = str(form.get("text") or "").replace("\r\n", "\n").replace("\r", "\n")
    text = raw if raw.strip() else None
    drops = [i for i in ids if i != keep]
    try:
        owner.merge_facts(deps.db, actor(user), ns, keep, drops, text)
    except owner.Flagged:
        # Forcing general text over the rules stays on the fact page (recent login needed).
        flash(request, "error", "flagged: the rules matched this text; edit it on the fact page")
        return redirect(back)
    n = len(set(drops))
    flash(request, "ok", f"merged {n} fact{'s' if n != 1 else ''}")
    return redirect(back)


@action(back="/memory/{ns}/duplicates")
def duplicates_dismiss(request: Request, deps: Any, user: str, form: Any) -> Response:
    ns = request.path_params["ns"]
    key = str(form.get("key") or "")
    if not _GROUP_KEY.fullmatch(key):
        flash(request, "error", "invalid group")
    else:
        keys = [k for k in _hidden(request) if k != key] + [key]
        request.session[HIDDEN_KEY] = keys[-MAX_HIDDEN:]
        flash(request, "ok", "hidden for this session")
    return redirect(f"/memory/{_q(ns)}/duplicates")


@page()
@_mapped
def conflicts(request: Request, deps: Any, user: str) -> Response:
    ws = request.path_params["ws"]
    return _render(
        request, deps, "conflicts.html", {"ws": ws, "rows": browse.conflicts(deps.db, ws)}
    )


@page()
@_mapped
def stale(request: Request, deps: Any, user: str) -> Response:
    ws = request.path_params["ws"]
    return _render(request, deps, "stale.html", {"ws": ws, "rows": browse.stale_flags(deps.db, ws)})


@page()
@_mapped
def sessions(request: Request, deps: Any, user: str) -> Response:
    ws = request.path_params["ws"]
    offset = page_offset(request)
    rows = browse.sessions(deps.db, ws, PAGE, offset)
    return _render(
        request,
        deps,
        "sessions.html",
        {
            "ws": ws,
            "rows": rows,
            "offset": offset,
            **pager(offset, len(rows)),
        },
    )


def _resolve(request: Request, fn: Callable[[str, int], None], ok: str, back: str) -> Response:
    ws = request.path_params["ws"]
    try:
        fn(ws, int(request.path_params["flag"]))
    except ValueError:
        flash(request, "error", "invalid id")
    except InvalidInput as e:
        flash(request, "error", str(e))
    else:
        flash(request, "ok", ok)
    return redirect(back.format(ws=_q(ws)))


@action(back="/memory/{ws}/merges")
def merge_do(request: Request, deps: Any, user: str, form: Any) -> Response:
    return _resolve(
        request,
        lambda ws, i: owner.merge(deps.db, actor(user), ws, i),
        "merged",
        "/memory/{ws}/merges",
    )


@action(back="/memory/{ws}/merges")
def merge_dismiss(request: Request, deps: Any, user: str, form: Any) -> Response:
    return _resolve(
        request,
        lambda ws, i: owner.dismiss_merge(deps.db, actor(user), ws, i),
        "dismissed",
        "/memory/{ws}/merges",
    )


@action(back="/memory/{ws}/conflicts")
def conflict_dismiss(request: Request, deps: Any, user: str, form: Any) -> Response:
    return _resolve(
        request,
        lambda ws, i: owner.dismiss_conflict(deps.db, actor(user), ws, i),
        "dismissed",
        "/memory/{ws}/conflicts",
    )


@action(back="/memory/{ws}/stale")
def stale_keep(request: Request, deps: Any, user: str, form: Any) -> Response:
    return _resolve(
        request,
        lambda ws, i: owner.keep_stale(deps.db, actor(user), ws, i),
        "kept",
        "/memory/{ws}/stale",
    )


@action(back="/memory/{ws}/stale")
def stale_retract(request: Request, deps: Any, user: str, form: Any) -> Response:
    return _resolve(
        request,
        lambda ws, i: owner.retract_stale(deps.db, actor(user), ws, i),
        "retracted",
        "/memory/{ws}/stale",
    )


def routes(deps: Any) -> list[BaseRoute]:
    post = ["POST"]
    return [
        Route("/memory", index),
        Route("/sessions", sessions_index),
        Route("/memory/{ns}", node_list),
        Route("/memory/{ns}/search", search_page),
        Route("/memory/{ns}/graph", node_graph),
        Route("/memory/{ns}/node/{id}", node_page),
        Route("/memory/{ns}/fact/{id}/history", fact_history),
        Route("/memory/{ns}/fact/{id}/edit", fact_edit, methods=post),
        Route("/memory/{ns}/fact/{id}/retract", fact_retract, methods=post),
        Route("/memory/{ns}/fact/{id}/restore", fact_restore, methods=post),
        Route("/memory/{ns}/duplicates", duplicates),
        Route("/memory/{ns}/duplicates/merge", duplicates_merge, methods=post),
        Route("/memory/{ns}/duplicates/dismiss", duplicates_dismiss, methods=post),
        Route("/memory/{ws}/merges", merges),
        Route("/memory/{ws}/merges/{flag}/merge", merge_do, methods=post),
        Route("/memory/{ws}/merges/{flag}/dismiss", merge_dismiss, methods=post),
        Route("/memory/{ws}/conflicts", conflicts),
        Route("/memory/{ws}/conflicts/{flag}/dismiss", conflict_dismiss, methods=post),
        Route("/memory/{ws}/stale", stale),
        Route("/memory/{ws}/stale/{flag}/keep", stale_keep, methods=post),
        Route("/memory/{ws}/stale/{flag}/retract", stale_retract, methods=post),
        Route("/memory/{ws}/sessions", sessions),
    ]
