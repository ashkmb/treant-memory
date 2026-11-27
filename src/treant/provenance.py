"""Where a memory item came from: the day it was saved, and for workspace items the session
that first produced it and the day a session last confirmed it.

A workspace item is linked to revisions through ``facts.revision_id``,
``journal_entries.revision_id`` and its ``support`` rows; only stored revisions count.
``general`` items carry their own creation date and nothing else: nothing here reads what
links a general item to a workspace or a session.
"""

import uuid
from datetime import UTC, datetime

import psycopg
from psycopg import sql

MAX_SESSION = 80

Item = tuple[str, str, str]  # (schema, kind, id)

_KINDS = ("fact", "node", "journal")

# One row per (item, linked stored revision); an item with no such revision gets one row with
# NULL revision columns. A revision linked twice (revision_id and a support row) repeats.
_WORKSPACE = (
    "WITH items AS ("
    "SELECT * FROM unnest(%(kinds)s::text[], %(ids)s::uuid[]) AS i(kind, id)), "
    "base AS ("
    "SELECT i.kind, i.id, f.created_at AS saved FROM items i "
    "JOIN {s}.facts f ON i.kind = 'fact' AND f.id = i.id "
    "UNION ALL SELECT i.kind, i.id, n.created_at FROM items i "
    "JOIN {s}.nodes n ON i.kind = 'node' AND n.id = i.id "
    "UNION ALL SELECT i.kind, i.id, j.created_at FROM items i "
    "JOIN {s}.journal_entries j ON i.kind = 'journal' AND j.id = i.id), "
    "links AS ("
    "SELECT i.kind, i.id, f.revision_id FROM items i "
    "JOIN {s}.facts f ON i.kind = 'fact' AND f.id = i.id WHERE f.revision_id IS NOT NULL "
    "UNION ALL SELECT i.kind, i.id, j.revision_id FROM items i "
    "JOIN {s}.journal_entries j ON i.kind = 'journal' AND j.id = i.id "
    "UNION ALL SELECT i.kind, i.id, su.revision_id FROM items i "
    "JOIN {s}.support su ON su.item_kind = i.kind AND su.item_id = i.id), "
    "stored AS ("
    "SELECT l.kind, l.id, r.created_at, se.client_session_id FROM links l "
    "JOIN {s}.revisions r ON r.id = l.revision_id AND r.status = 'stored' "
    "JOIN {s}.sessions se ON se.id = r.session_id) "
    "SELECT b.kind, b.id::text, b.saved, st.created_at, st.client_session_id FROM base b "
    "LEFT JOIN stored st ON st.kind = b.kind AND st.id = b.id"
)

# ``general`` has no journal and no revisions: the creation date is all there is.
_GENERAL = (
    "WITH items AS ("
    "SELECT * FROM unnest(%(kinds)s::text[], %(ids)s::uuid[]) AS i(kind, id)) "
    "SELECT i.kind, i.id::text, f.created_at FROM items i "
    "JOIN {s}.facts f ON i.kind = 'fact' AND f.id = i.id "
    "UNION ALL SELECT i.kind, i.id::text, n.created_at FROM items i "
    "JOIN {s}.nodes n ON i.kind = 'node' AND n.id = i.id"
)


def _day(value: datetime) -> str:
    return value.astimezone(UTC).date().isoformat()


def format_source(
    saved: datetime, revisions: list[tuple[datetime, str]], general: bool
) -> dict[str, str]:
    """``revisions`` are ``(created_at, client_session_id)`` of the item's stored revisions in
    any order. The session is the earliest revision's; the confirmed date the newest's."""
    out = {"saved": _day(saved)}
    if general or not revisions:
        return out
    ordered = sorted(revisions)
    out["confirmed"] = _day(ordered[-1][0])
    out["session"] = ordered[0][1][:MAX_SESSION]
    return out


def _canonical(item_id: str) -> str | None:
    try:
        return str(uuid.UUID(str(item_id)))
    except ValueError:
        return None


def lookup(
    conn: psycopg.Connection, ws_schema: str, items: list[Item]
) -> dict[Item, dict[str, str]]:
    """Source fields for each found item. Only ``ws_schema`` and ``general`` are read, with at
    most one query each; items of any other schema, unknown kind or not found are absent."""
    wanted: dict[str, dict[tuple[str, str], list[Item]]] = {ws_schema: {}, "general": {}}
    for item in items:
        schema, kind, item_id = item
        canon = _canonical(item_id)
        if schema not in wanted or kind not in _KINDS or canon is None:
            continue
        if schema == "general" and kind == "journal":
            continue
        wanted[schema].setdefault((kind, canon), []).append(item)

    out: dict[Item, dict[str, str]] = {}
    for schema, keys in wanted.items():
        if not keys:
            continue
        general = schema == "general"
        params = {"kinds": [k for k, _ in keys], "ids": [i for _, i in keys]}
        query = sql.SQL(_GENERAL if general else _WORKSPACE).format(s=sql.Identifier(schema))
        saved: dict[tuple[str, str], datetime] = {}
        revs: dict[tuple[str, str], list[tuple[datetime, str]]] = {}
        for row in conn.execute(query, params).fetchall():
            key = (row[0], row[1])
            saved[key] = row[2]
            found = revs.setdefault(key, [])
            if not general and row[3] is not None:
                found.append((row[3], row[4]))
        for key, created in saved.items():
            source = format_source(created, revs.get(key, []), general)
            for item in keys.get(key, []):
                out[item] = dict(source)
    return out
