"""Review queue pages: list, item with rule findings, approve (single and bulk), drop.

Approving is a sensitive action (recent login); dropping is login + CSRF only. Untrusted text is
shown as escaped segments, never as HTML built here."""

from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from treant import browse, gate, owner
from treant.dashboard.routes._common import PAGE, pager, parse_id, ws_filter
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

MAX_BULK = 200
STATUSES = ("pending", "waiting_review", "blocked", "published", "dropped")
FLAGGED_EDIT = "flagged: the rules matched the edited text; tick the box to publish anyway"


def segments(text: str, spans: list[tuple[int, int]]) -> list[tuple[str, bool]]:
    """Split ``text`` into (part, flagged) pieces at the (merged) spans, for escaped rendering."""
    n = len(text)
    merged: list[list[int]] = []
    clamped = ((max(0, s), min(n, e)) for s, e in spans)
    for start, end in sorted(c for c in clamped if c[0] < c[1]):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    parts: list[tuple[str, bool]] = []
    pos = 0
    for start, end in merged:
        if start > pos:
            parts.append((text[pos:start], False))
        parts.append((text[start:end], True))
        pos = end
    if pos < n:
        parts.append((text[pos:], False))
    return parts


def _checked_text(item: dict) -> str:
    """The text the rules ran on (as the gate builds it)."""
    if item["kind"] == "concept":
        return "\n".join([item["name"] or "", *item["aliases"], item["type"] or ""])
    return item["text"] or ""


@page()
def queue_list(request: Request, deps: Any, user: str) -> Response:
    params = request.query_params
    status = params.get("status", "waiting_review").strip()
    if status == "all":
        status_arg = None
    elif status in STATUSES:
        status_arg = status
    else:
        raise InvalidInput("status")
    ws, notice = ws_filter(params.get("ws"))
    start = page_offset(request)
    rows = browse.queue(deps.db, status_arg, ws or None, PAGE, start)
    return deps.templates.TemplateResponse(
        request,
        "queue.html",
        {
            "rows": rows,
            "status": status,
            "ws": ws,
            "notice": notice,
            "statuses": STATUSES,
            "open": gate.OPEN,
            **pager(start, len(rows)),
        },
    )


@page()
def queue_item(request: Request, deps: Any, user: str) -> Response:
    qid = parse_id(request.path_params["id"])
    item = browse.queue_item(deps.db, qid) if qid is not None else None
    if item is None:
        return error_page(request, 404, "not found")
    is_open = item["status"] in gate.OPEN
    findings: list = []
    if is_open:
        try:
            findings = owner.check_approval(deps.db, qid, None)
        except InvalidInput:
            findings = []
    text = _checked_text(item)
    waiting = None
    if item["kind"] == "concept" and item["name"]:
        waiting = browse.waiting_on_concept(deps.db, item["workspace"], item["name"])
    return deps.templates.TemplateResponse(
        request,
        "queue_item.html",
        {
            "item": item,
            "is_open": is_open,
            "findings": findings,
            "parts": segments(text, [(f.start, f.end) for f in findings]),
            "source_parts": segments(item["source_text"] or "", []),
            "max_text": gate.MAX_TEXT,
            "waiting": waiting,
        },
    )


@action(sensitive=True, back="/queue/{id}")
def approve(request: Request, deps: Any, user: str, form: Any) -> Response:
    qid = parse_id(request.path_params["id"])
    if qid is None:
        flash(request, "error", "invalid id")
        return redirect("/queue")
    item = browse.queue_item(deps.db, qid)
    text: str | None = None
    if item is not None and item["kind"] != "concept" and "text" in form:
        given = str(form.get("text") or "").replace("\r\n", "\n").replace("\r", "\n")
        if not given.strip():
            # A cleared textarea is not "publish the original": say so instead.
            raise InvalidInput("text is empty")
        if given != (item["text"] or ""):
            text = given
    try:
        owner.general_approve(deps.db, actor(user), qid, text, bool(form.get("force")))
    except owner.Flagged:
        flash(
            request,
            "error",
            FLAGGED_EDIT if text is not None else FLAGGED_EDIT.replace("edited ", ""),
        )
        return redirect(f"/queue/{qid}")
    flash(request, "ok", f"published #{qid}")
    return redirect("/queue")


@action(back="/queue/{id}")
def drop(request: Request, deps: Any, user: str, form: Any) -> Response:
    qid = parse_id(request.path_params["id"])
    if qid is None:
        flash(request, "error", "invalid id")
        return redirect("/queue")
    owner.general_drop(deps.db, actor(user), qid)
    flash(request, "ok", f"dropped #{qid}")
    return redirect("/queue")


def _bulk_ids(form: Any) -> list[int]:
    raw = form.getlist("ids")
    if len(raw) > MAX_BULK:
        raise InvalidInput("ids")
    ids: list[int] = []
    for value in raw:
        qid = parse_id(value)
        if qid is None:
            raise InvalidInput("ids")
        if qid not in ids:
            ids.append(qid)
    return ids


def _why(e: InvalidInput) -> str:
    """The refusal category for a skipped item; the common one is shortened to "not open"."""
    text = str(e)
    return "not open" if "not open" in text else text


def _bulk_approve(deps: Any, user: str, ids: list[int]) -> str:
    done = 0
    skipped: list[str] = []
    for qid in ids:
        try:
            owner.general_approve(deps.db, actor(user), qid, None, False)
            done += 1
        except owner.Flagged:
            skipped.append(f"#{qid} (flagged)")
        except InvalidInput as e:
            skipped.append(f"#{qid} ({_why(e)})")
    out = f"published {done}"
    return out + (f"; skipped {', '.join(skipped)}" if skipped else "")


@action(back="/queue")
def bulk(request: Request, deps: Any, user: str, form: Any) -> Response:
    op = form.get("op")
    if op not in ("approve", "drop"):
        raise InvalidInput("op")
    if op == "approve":  # approving is sensitive; dropping is not
        try:
            require_recent(request, "/queue")
        except NeedsReauth as e:
            return redirect(login_url(e.next, fresh=True))
    ids = _bulk_ids(form)
    if not ids:
        raise InvalidInput("no items selected")
    if op == "approve":
        flash(request, "ok", _bulk_approve(deps, user, ids))
        return redirect("/queue")
    done = 0
    skipped: list[str] = []
    for qid in ids:
        try:
            owner.general_drop(deps.db, actor(user), qid)
            done += 1
        except InvalidInput:
            skipped.append(f"#{qid} (not open)")
    flash(request, "ok", f"dropped {done}" + (f"; skipped {', '.join(skipped)}" if skipped else ""))
    return redirect("/queue")


def routes(deps: Any) -> list[BaseRoute]:
    post = ["POST"]
    return [
        Route("/queue", queue_list),
        Route("/queue/bulk", bulk, methods=post),
        Route("/queue/{id}", queue_item),
        Route("/queue/{id}/approve", approve, methods=post),
        Route("/queue/{id}/drop", drop, methods=post),
    ]
