"""save_session: one transaction writes the session, revision, journal entry and its jobs.

Everything stored passes the server scrubber first. A save the client marked unsure, or one the
scrubber is not confident about, is quarantined: stored for the owner to release or drop, with
no embed and no distill job, so nothing of it is searchable.
"""

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from psycopg import sql
from psycopg.types.json import Jsonb

from treant import jobs, names, settings
from treant.db import Database
from treant.errors import InvalidInput
from treant.scrub import scrub
from treant.tokens import TokenContext

SECTIONS: tuple[str, ...] = (
    "journal",
    "facts",
    "decisions",
    "lessons",
    "dead_ends",
    "procedures",
    "changes",
    "open_threads",
    "preferences",
    "warnings",
    "client_memory",
)
MAX_SESSION_ID = 200
MAX_TOTAL_TEXT = 200_000


@dataclass(frozen=True)
class SaveResult:
    session_id: str
    revision: int
    quarantined: bool = False


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _has_nul(value: Any) -> bool:
    """Postgres text and jsonb cannot hold NUL; find one in any string, key or leaf."""
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_has_nul(k) or _has_nul(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_has_nul(v) for v in value)
    return False


def _validate(client_session_id: str, sections: dict[str, Any]) -> None:
    if not isinstance(client_session_id, str) or not 0 < len(client_session_id) <= MAX_SESSION_ID:
        raise InvalidInput("client_session_id")
    if not isinstance(sections, dict) or not all(isinstance(k, str) for k in sections):
        raise InvalidInput("sections")
    # Keys count too: an unknown key becomes a journal heading.
    if sum(len(k) + len(_text(v)) for k, v in sections.items()) > MAX_TOTAL_TEXT:
        raise InvalidInput("too_large")
    if "\x00" in client_session_id or _has_nul(sections):
        raise InvalidInput("nul")


def _terms(db: Database, workspace: str) -> tuple[list[str], bool]:
    """The workspace's `rules.terms` and whether its stored settings parsed.

    A row that does not parse yields no terms and ``ok=False``: the caller quarantines the save,
    so a damaged row never weakens scrubbing silently. An unknown workspace stays an error.
    """
    try:
        return list(settings.load(db, workspace).terms), True
    except InvalidInput:
        with db.control_tx() as conn:
            known = conn.execute(
                "SELECT 1 FROM control.workspaces WHERE name = %s", (workspace,)
            ).fetchone()
        if known is None:
            raise
        return [], False


class _Tally:
    def __init__(self, terms: Sequence[str]) -> None:
        self.terms = terms
        self.removed = 0
        self.confident = True

    def text(self, value: str) -> str:
        res = scrub(value, self.terms)
        self.removed += res.removed
        self.confident = self.confident and res.confident
        return res.text

    def leaves(self, value: Any) -> Any:
        """Scrub every string leaf, keeping the structure."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {
                self.text(k) if isinstance(k, str) else k: self.leaves(v) for k, v in value.items()
            }
        if isinstance(value, list):
            return [self.leaves(v) for v in value]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            text = self.text(str(value))
            return text if text != str(value) else value
        return value

    def sections(self, sections: dict[str, Any]) -> dict[str, Any]:
        """Scrub keys and values; keys that collide after scrubbing get a numeric suffix."""
        out: dict[str, Any] = {}
        for key, value in sections.items():
            new = self.text(key)
            n = 1
            while new in out:
                n += 1
                new = f"{self.text(key)}_{n}"
            out[new] = self.leaves(value)
        return out


def _journal_text(sections: dict[str, Any]) -> str:
    text = _text(sections.get("journal", ""))
    for key, value in sections.items():
        if key not in SECTIONS:
            text += f"\n\n## {key}\n{_text(value)}"
    return text


def save_session(
    db: Database,
    ctx: TokenContext,
    client_session_id: str,
    sections: dict[str, Any],
    unsure: bool = False,
) -> SaveResult:
    _validate(client_session_id, sections)
    terms, settings_ok = _terms(db, ctx.workspace)
    tally = _Tally(terms)
    sections = tally.sections(sections)
    journal = _journal_text(sections)
    quarantined = bool(unsure) or not tally.confident or not settings_ok
    status = "quarantined" if quarantined else "stored"
    schema = sql.Identifier(names.ws_schema(ctx.workspace))
    with db.ws_tx(ctx.workspace) as conn:
        # The no-op update takes the row lock, serialising concurrent saves of one session.
        session_id = conn.execute(
            sql.SQL(
                "INSERT INTO {s}.sessions (token_id, client_session_id) VALUES (%s, %s) "
                "ON CONFLICT (token_id, client_session_id) "
                "DO UPDATE SET client_session_id = excluded.client_session_id RETURNING id"
            ).format(s=schema),
            (ctx.token_id, client_session_id),
        ).fetchone()[0]
        revision_id, n = conn.execute(
            sql.SQL(
                "INSERT INTO {s}.revisions (session_id, n, sections, status, removed) "
                "SELECT %(sid)s, coalesce(max(n), 0) + 1, %(sections)s, %(status)s, %(removed)s "
                "FROM {s}.revisions WHERE session_id = %(sid)s RETURNING id, n"
            ).format(s=schema),
            {
                "sid": session_id,
                "sections": Jsonb(sections),
                "status": status,
                "removed": tally.removed,
            },
        ).fetchone()
        conn.execute(
            sql.SQL("INSERT INTO {s}.journal_entries (revision_id, text) VALUES (%s, %s)").format(
                s=schema
            ),
            (revision_id, journal),
        )
        if not quarantined:
            _enqueue_stored(conn, ctx.workspace, str(revision_id))
    return SaveResult(session_id=str(session_id), revision=n, quarantined=quarantined)


def _enqueue_stored(conn: Any, workspace: str, revision_id: str) -> None:
    payload = {"revision_id": revision_id}
    jobs.enqueue(conn, workspace, "embed", revision_id, payload)
    jobs.enqueue(conn, workspace, "distill", revision_id, payload)


def _require_workspace(db: Database, workspace: str) -> None:
    try:
        names.validate_workspace(workspace)
    except ValueError:
        raise InvalidInput("workspace") from None  # a category, like an unknown name
    with db.admin_tx() as conn:
        found = conn.execute(
            "SELECT 1 FROM control.workspaces WHERE name = %s", (workspace,)
        ).fetchone()
    if found is None:
        raise InvalidInput("workspace")


def _revision_uuid(revision_id: str) -> None:
    try:
        uuid.UUID(revision_id)
    except (ValueError, AttributeError, TypeError):
        raise InvalidInput("revision") from None


def quarantined_sections(admin_db: Database, workspace: str, revision_id: str) -> dict[str, Any]:
    """Owner terminal only: the stored (scrubbed) sections of a quarantined revision."""
    _require_workspace(admin_db, workspace)
    _revision_uuid(revision_id)
    schema = sql.Identifier(names.ws_schema(workspace))
    with admin_db.admin_tx() as conn:
        row = conn.execute(
            sql.SQL(
                "SELECT sections FROM {s}.revisions WHERE id = %s AND status = 'quarantined'"
            ).format(s=schema),
            (revision_id,),
        ).fetchone()
    if row is None:
        raise InvalidInput("revision is not quarantined")
    return row[0]


def _owner_change(db: Database, actor: str, workspace: str, revision_id: str, to: str) -> None:
    _require_workspace(db, workspace)
    _revision_uuid(revision_id)
    schema = sql.Identifier(names.ws_schema(workspace))
    with db.admin_tx() as conn:
        row = conn.execute(
            sql.SQL(
                "UPDATE {s}.revisions SET status = %s WHERE id = %s AND status = 'quarantined' "
                "RETURNING id"
            ).format(s=schema),
            (to, revision_id),
        ).fetchone()
        if row is None:
            raise InvalidInput("revision is not quarantined")
        if to == "stored":
            _enqueue_stored(conn, workspace, revision_id)
        else:
            conn.execute(
                sql.SQL("UPDATE {s}.revisions SET sections = '{{}}' WHERE id = %s").format(
                    s=schema
                ),
                (revision_id,),
            )
            conn.execute(
                sql.SQL(
                    "DELETE FROM {s}.embeddings WHERE item_kind = 'journal' AND item_id IN "
                    "(SELECT id FROM {s}.journal_entries WHERE revision_id = %s)"
                ).format(s=schema),
                (revision_id,),
            )
            conn.execute(
                sql.SQL("UPDATE {s}.journal_entries SET text = '' WHERE revision_id = %s").format(
                    s=schema
                ),
                (revision_id,),
            )
        action = "quarantine.release" if to == "stored" else "quarantine.drop"
        conn.execute(
            "INSERT INTO control.audit (actor, action, detail) VALUES (%s, %s, %s)",
            (actor, action, Jsonb({"workspace": workspace, "revision_id": revision_id})),
        )


def release(admin_db: Database, workspace: str, revision_id: str, *, actor: str) -> None:
    """Owner action: a quarantined revision becomes stored and gets its embed and distill jobs."""
    _owner_change(admin_db, actor, workspace, revision_id, "stored")


def drop(admin_db: Database, workspace: str, revision_id: str, *, actor: str) -> None:
    """Owner action: drop a quarantined revision for good. The suspect text is blanked (sections
    emptied, journal text cleared, journal embeddings deleted); the rows stay as a record."""
    _owner_change(admin_db, actor, workspace, revision_id, "dropped")
