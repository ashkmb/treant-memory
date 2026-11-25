"""Helpers shared by the dashboard route modules."""

from typing import Any

from starlette.requests import Request

from treant import names

PAGE = 100
MAX_ID = 2**63 - 1
MAX_OFFSET = 10**9  # far past any real list, and well inside a Postgres bigint
MAX_FILTER = 100
UNKNOWN_WS = "unknown workspace filter ignored"


def offset(request: Request) -> int:
    """The ``offset`` query parameter, 0 when missing, negative or not a number, and at most
    ``MAX_OFFSET`` (a huge number would overflow the database's bigint)."""
    try:
        return min(MAX_OFFSET, max(0, int(request.query_params.get("offset", "0"))))
    except ValueError:
        return 0


def ws_filter(raw: Any) -> tuple[str, str]:
    """A ``?ws=`` list filter as ``(name, notice)``: an invalid name is ignored (``""``) with the
    notice ``UNKNOWN_WS`` for the page to show, so every list treats a bad filter alike."""
    ws = str(raw or "").strip()[:MAX_FILTER]
    if not ws:
        return "", ""
    try:
        names.validate_workspace(ws)
    except ValueError:
        return "", UNKNOWN_WS
    return ws, ""


def parse_id(raw: Any) -> int | None:
    """A database id from a path or form value; None when it is not a plain id in range."""
    try:
        value = int(str(raw))
    except ValueError:
        return None
    return value if 0 <= value <= MAX_ID else None


def pager(start: int, count: int) -> dict[str, int | None]:
    """The prev/next offsets for a page of ``count`` rows read at ``start``."""
    return {
        "prev": max(0, start - PAGE) if start > 0 else None,
        "next": start + PAGE if count == PAGE else None,
    }
