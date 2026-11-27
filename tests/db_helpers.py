"""Database setup helpers shared by several test modules."""

from datetime import UTC, datetime

from psycopg.types.json import Jsonb

from treant import names

DAY1 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def _revision(db, ws, client_session, when, status="stored"):
    """A revision of ``client_session`` (created if new) dated ``when``."""
    s = names.ws_schema(ws)
    with db.admin_tx() as c:
        sid = c.execute(
            f"INSERT INTO {s}.sessions (token_id, client_session_id) VALUES ('t', %s) "
            "ON CONFLICT (token_id, client_session_id) DO UPDATE SET token_id = EXCLUDED.token_id "
            "RETURNING id::text",
            (client_session,),
        ).fetchone()[0]
        return c.execute(
            f"INSERT INTO {s}.revisions (session_id, n, sections, status, created_at) "
            f"VALUES (%s, (SELECT coalesce(max(n), 0) + 1 FROM {s}.revisions "
            "WHERE session_id = %s), %s, %s, %s) RETURNING id::text",
            (sid, sid, Jsonb({}), status, when),
        ).fetchone()[0]


def _journal(db, ws, revision, text="journal text"):
    with db.admin_tx() as c:
        return c.execute(
            f"INSERT INTO {names.ws_schema(ws)}.journal_entries (revision_id, text) "
            "VALUES (%s, %s) RETURNING id::text",
            (revision, text),
        ).fetchone()[0]
