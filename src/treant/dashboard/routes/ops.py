"""Quarantined saves and background jobs. Viewing the held text and releasing are sensitive
(recent login); drop and retry need a login and CSRF only."""

import json
from typing import Any
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from treant import browse, owner, sessions
from treant.dashboard.routes._common import PAGE, UNKNOWN_WS, pager, parse_id, ws_filter
from treant.dashboard.routes._common import offset as page_offset
from treant.dashboard.security import action, actor, error_page, flash, page, redirect
from treant.errors import InvalidInput

JOB_STATUSES = ("queued", "running", "done", "dead")


def _q(value: str) -> str:
    return quote(value, safe="")


def _rendered(value: Any) -> str:
    """A section value as plain text (escaped when rendered): strings as is, others as JSON."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


@page()
def quarantine_list(request: Request, deps: Any, user: str) -> Response:
    ws, notice = ws_filter(request.query_params.get("ws"))
    try:
        rows = browse.quarantined(deps.db, ws or None)
    except InvalidInput:  # a valid name that is not registered
        ws, notice = "", UNKNOWN_WS
        rows = browse.quarantined(deps.db, None)
    return deps.templates.TemplateResponse(
        request, "quarantine.html", {"rows": rows, "ws": ws, "notice": notice}
    )


@page(sensitive=True)
def quarantine_item(request: Request, deps: Any, user: str) -> Response:
    ws, rev = request.path_params["ws"], request.path_params["rev"]
    try:
        sections = sessions.quarantined_sections(deps.db, ws, rev)
    except InvalidInput:
        return error_page(request, 404, "not found")
    entries = [(str(k), _rendered(v)) for k, v in sections.items()]
    return deps.templates.TemplateResponse(
        request, "quarantine_item.html", {"ws": ws, "rev": rev, "entries": entries}
    )


@action(sensitive=True, back="/quarantine/{ws}/{rev}")
def release(request: Request, deps: Any, user: str, form: Any) -> Response:
    ws, rev = request.path_params["ws"], request.path_params["rev"]
    owner.release_quarantine(deps.db, actor(user), ws, rev)
    flash(request, "ok", "released")
    return redirect("/quarantine")


@action(back="/quarantine/{ws}/{rev}")
def drop(request: Request, deps: Any, user: str, form: Any) -> Response:
    ws, rev = request.path_params["ws"], request.path_params["rev"]
    owner.drop_quarantine(deps.db, actor(user), ws, rev)
    flash(request, "ok", "dropped")
    return redirect("/quarantine")


@page()
def jobs_list(request: Request, deps: Any, user: str) -> Response:
    params = request.query_params
    status = params.get("status", "dead").strip()
    if status == "all":
        status_arg = None
    elif status in JOB_STATUSES:
        status_arg = status
    else:
        raise InvalidInput("status")
    ws, notice = ws_filter(params.get("ws"))
    kind = (params.get("kind") or "").strip()[:100] or None
    start = page_offset(request)
    rows = browse.jobs(deps.db, status_arg, ws or None, kind, PAGE, start)
    return deps.templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "rows": rows,
            "status": status,
            "ws": ws,
            "notice": notice,
            "kind": kind or "",
            "statuses": JOB_STATUSES,
            **pager(start, len(rows)),
        },
    )


@action(back="/jobs")
def retry(request: Request, deps: Any, user: str, form: Any) -> Response:
    jid = parse_id(request.path_params["id"])
    if jid is None:
        flash(request, "error", "invalid id")
        return redirect("/jobs")
    owner.retry_jobs(deps.db, actor(user), job_id=jid)
    flash(request, "ok", f"re-queued #{jid}")
    return redirect("/jobs")


@action(back="/jobs")
def retry_dead(request: Request, deps: Any, user: str, form: Any) -> Response:
    ws = str(form.get("ws") or "").strip()[:100] or None
    kind = str(form.get("kind") or "").strip()[:100] or None
    ids = owner.retry_jobs(deps.db, actor(user), status="dead", kind=kind, ws=ws)
    flash(request, "ok", f"re-queued {len(ids)} dead job(s)")
    return redirect("/jobs")


def routes(deps: Any) -> list[BaseRoute]:
    post = ["POST"]
    return [
        Route("/quarantine", quarantine_list),
        Route("/quarantine/{ws}/{rev}", quarantine_item),
        Route("/quarantine/{ws}/{rev}/release", release, methods=post),
        Route("/quarantine/{ws}/{rev}/drop", drop, methods=post),
        Route("/jobs", jobs_list),
        Route("/jobs/retry-dead", retry_dead, methods=post),
        Route("/jobs/{id}/retry", retry, methods=post),
    ]
