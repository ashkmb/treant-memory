"""Workspace-bound bearer tokens.

Format ``trt_<id>_<secret>``: ``id`` is 12 lowercase hex chars, ``secret`` is a 43-char
urlsafe string. Only sha256(secret) is stored. Every token belongs to one workspace.
"""

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from typing import Any

from treant.db import Database

ACTIONS = frozenset({"create", "read", "update", "delete"})
_ID_RE = re.compile(r"[0-9a-f]{12}")
_SECRET_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_DUMMY_HASH = hashlib.sha256(b"treant-dummy").hexdigest()


@dataclass(frozen=True)
class TokenContext:
    token_id: str
    workspace: str
    actions: frozenset[str]


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def create_token_in(conn: Any, workspace: str, label: str, actions: set[str]) -> str:
    """``create_token`` inside the caller's control transaction (so an audit row can share it)."""
    if not actions or not set(actions) <= ACTIONS:
        raise ValueError("unknown action")
    token_id = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    row = conn.execute(
        "INSERT INTO control.tokens (id, workspace, label, secret_sha256, actions) "
        "SELECT %s, w.name, %s, %s, %s FROM control.workspaces w "
        "WHERE w.name = %s AND w.status = 'active' RETURNING id",
        (token_id, label, _hash(secret), sorted(actions), workspace),
    ).fetchone()
    if row is None:
        raise ValueError("workspace not available")
    return f"trt_{token_id}_{secret}"


def create_token(db: Database, workspace: str, label: str, actions: set[str]) -> str:
    """Create a token; the full string is returned once and never stored."""
    with db.control_tx() as conn:
        return create_token_in(conn, workspace, label, actions)


def _parse(raw: str) -> tuple[str, str] | None:
    if not isinstance(raw, str):
        return None
    parts = raw.split("_", 2)
    if len(parts) != 3 or parts[0] != "trt":
        return None
    token_id, secret = parts[1], parts[2]
    if not _ID_RE.fullmatch(token_id) or not _SECRET_RE.fullmatch(secret):
        return None
    return token_id, secret


def verify_token(db: Database, raw: str) -> TokenContext | None:
    parsed = _parse(raw)
    if parsed is None:
        return None
    token_id, secret = parsed
    with db.control_tx() as conn:
        row = conn.execute(
            "SELECT t.workspace, t.actions, t.secret_sha256 "
            "FROM control.tokens t JOIN control.workspaces w ON w.name = t.workspace "
            "WHERE t.id = %s AND t.revoked_at IS NULL AND w.status = 'active'",
            (token_id,),
        ).fetchone()
        stored = row[2] if row is not None else _DUMMY_HASH
        ok = hmac.compare_digest(stored, _hash(secret))
        if row is None or not ok:
            return None
        conn.execute(
            "UPDATE control.tokens SET last_used_at = now() WHERE id = %s "
            "AND (last_used_at IS NULL OR last_used_at < now() - interval '60 seconds')",
            (token_id,),
        )
    return TokenContext(token_id, row[0], frozenset(row[1]))


def list_tokens(db: Database, workspace: str) -> list[dict[str, Any]]:
    with db.control_tx() as conn:
        rows = conn.execute(
            "SELECT id, workspace, label, actions, created_at, revoked_at, last_used_at "
            "FROM control.tokens WHERE workspace = %s ORDER BY created_at, id",
            (workspace,),
        ).fetchall()
    return [
        {
            "id": r[0],
            "workspace": r[1],
            "label": r[2],
            "actions": sorted(r[3]),
            "created_at": r[4],
            "revoked_at": r[5],
            "last_used_at": r[6],
        }
        for r in rows
    ]
