"""Mistakes, measurements and the audit log."""

import json
from datetime import datetime
from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from treant import browse, owner
from treant.dashboard import security
from treant.dashboard.routes._common import PAGE, offset, pager, ws_filter
from treant.dashboard.security import action, actor, flash, page, redirect

STALE_SECONDS = 36 * 3600
MAX_PREFIX = 64


def _iso(at: Any) -> str:
    return at.isoformat(timespec="minutes") if isinstance(at, datetime) else str(at)


@page()
def mistakes_page(request: Request, deps: Any, user: str) -> Response:
    ws, notice = ws_filter(request.query_params.get("ws"))
    start = offset(request)
    found = browse.mistakes(deps.db, ws or None, PAGE, start)
    rows = [{**r, "at": _iso(r["at"])} for r in found]
    active = [w["name"] for w in browse.workspaces(deps.db) if w["status"] == "active"]
    return deps.templates.TemplateResponse(
        request,
        "mistakes.html",
        {"rows": rows, "ws": ws, "notice": notice, "active": active, **pager(start, len(rows))},
    )


@action(back="/mistakes")
def mistake_log(request: Request, deps: Any, user: str, form: Any) -> Response:
    text = str(form.get("text") or "").replace("\r\n", "\n").replace("\r", "\n")
    owner.log_mistake(deps.db, actor(user), str(form.get("workspace") or ""), text)
    flash(request, "ok", "mistake logged")
    return redirect("/mistakes")


def _isolation(data: dict | None) -> tuple[dict | None, bool]:
    """The isolation record and whether it is bad: missing, older than 36 h, or with faults."""
    if data is None:
        return None, True
    age = security.now() - data["at"].timestamp()
    bad = age > STALE_SECONDS or bool(data["faults"])
    return {**data, "at_text": _iso(data["at"])}, bad


@page()
def measurements_page(request: Request, deps: Any, user: str) -> Response:
    data = browse.measurements(deps.db)
    isolation, bad = _isolation(data["isolation"])
    return deps.templates.TemplateResponse(
        request,
        "measurements.html",
        {
            "workspaces": data["workspaces"],
            "isolation": isolation,
            "isolation_bad": bad,
            "canaries": data["canaries"],
        },
    )


@page()
def audit_page(request: Request, deps: Any, user: str) -> Response:
    prefix = request.query_params.get("action", "").strip()[:MAX_PREFIX]
    start = offset(request)
    rows = browse.audit(deps.db, PAGE, start, prefix or None)
    shown = [
        {
            "id": r["id"],
            "at": _iso(r["at"]),
            "actor": r["actor"],
            "action": r["action"],
            "detail": json.dumps(r["detail"], ensure_ascii=False, sort_keys=True),
        }
        for r in rows
    ]
    return deps.templates.TemplateResponse(
        request,
        "audit.html",
        {"rows": shown, "prefix": prefix, **pager(start, len(rows))},
    )


def routes(deps: Any) -> list[BaseRoute]:
    return [
        Route("/mistakes", mistakes_page),
        Route("/mistakes", mistake_log, methods=["POST"]),
        Route("/measurements", measurements_page),
        Route("/audit", audit_page),
    ]
