"""Reconciliation helpers for the apply stage.

Text identity (``normalise``, ``similarity``), model-declared supersedes with conflict records,
the open-thread reconcile within a session, and the source texts the general queue
de-duplicates against. Saves are additive: apart from open threads, nothing a session produced
is superseded because a later save does not repeat it. Nothing is ever deleted except this
session's older support rows on an open thread another session still supports.
"""

from difflib import SequenceMatcher

import psycopg
from psycopg import sql

from treant import graph

MERGE_RATIO = 0.85
FACT_RATIO = 0.8
KINDS = ("node", "edge", "fact")


def normalise(text: str) -> str:
    """The one text normalisation: casefold, whitespace runs (NBSP included) to one space."""
    return " ".join(text.casefold().split())


def similarity(a: str, b: str, floor: float) -> float:
    """``difflib`` ratio of a and b, or 0.0 when the cheap upper bounds are already below floor."""
    m = SequenceMatcher(None, a, b)
    if m.real_quick_ratio() < floor or m.quick_ratio() < floor:
        return 0.0
    return m.ratio()


def supersede_ref(conn: psycopg.Connection, ws: str, new: str, old: str, session_id: str) -> None:
    schema = graph.schema_of(ws)
    row = conn.execute(
        sql.SQL(
            "SELECT f.status, r.session_id::text FROM {s}.facts f "
            "LEFT JOIN {s}.revisions r ON r.id = f.revision_id WHERE f.id = %s FOR UPDATE OF f"
        ).format(s=schema),
        (old,),
    ).fetchone()
    if row is None or row[0] not in ("current", "superseded"):
        return
    if row[0] == "current":
        graph.set_status(conn, ws, "fact", old, "superseded", "superseded")
    if row[1] != session_id:  # another session's fact (or one with no revision): a conflict
        conn.execute(
            sql.SQL(
                "INSERT INTO {s}.conflicts (new_fact, old_fact) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING"
            ).format(s=schema),
            (new, old),
        )


def _status(conn: psycopg.Connection, ws: str, kind: str, item_id: str) -> str | None:
    table = sql.Identifier({"node": "nodes", "edge": "edges", "fact": "facts"}[kind])
    row = conn.execute(
        sql.SQL("SELECT status FROM {}.{} WHERE id = %s FOR UPDATE").format(
            graph.schema_of(ws), table
        ),
        (item_id,),
    ).fetchone()
    return row[0] if row else None


def workspace(
    conn: psycopg.Connection,
    ws: str,
    session_id: str,
    older: list[str],
    produced: dict[str, set[str]],
) -> int:
    """Supersede the open threads only this session's older revisions supported and this one
    did not reproduce; an open thread another session also supports just loses this session's
    older support. Saves are additive for everything else: nodes, edges and facts of other
    types stay as they are, with their support."""
    if not older:
        return 0
    schema = graph.schema_of(ws)
    rows = conn.execute(
        sql.SQL(
            "SELECT s.item_id::text, bool_or(r.session_id <> %(sid)s) "
            "FROM {s}.support s JOIN {s}.revisions r ON r.id = s.revision_id "
            "JOIN {s}.facts f ON f.id = s.item_id "
            "WHERE s.item_kind = 'fact' AND f.type = 'open_thread' AND s.item_id IN ("
            " SELECT item_id FROM {s}.support WHERE item_kind = 'fact' "
            " AND revision_id = ANY(%(older)s)) "
            "GROUP BY 1 ORDER BY 1"
        ).format(s=schema),
        {"sid": session_id, "older": older},
    ).fetchall()
    superseded = 0
    for item_id, other in rows:
        if item_id in produced["fact"]:
            continue
        if other:
            conn.execute(
                sql.SQL(
                    "DELETE FROM {s}.support WHERE item_kind = 'fact' AND item_id = %s "
                    "AND revision_id = ANY(%s)"
                ).format(s=schema),
                (item_id, older),
            )
        elif _status(conn, ws, "fact", item_id) == "current":
            graph.set_status(conn, ws, "fact", item_id, "superseded", "reconcile")
            superseded += 1
    return superseded


def published_sources(conn: psycopg.Connection, kind: str) -> set[str]:
    """Normalised ``source_text`` of the queue rows behind every current published general item
    of ``kind`` ("fact" or "concept")."""
    table = sql.Identifier("facts" if kind == "fact" else "nodes")
    rows = conn.execute(
        sql.SQL(
            "SELECT q.item->>'source_text' FROM control.general_origin o "
            "JOIN control.general_queue q ON q.id = o.queue_id "
            "JOIN general.{} x ON x.id = o.item_id "
            "WHERE o.item_kind = %s AND x.status = 'current'"
        ).format(table),
        (kind,),
    ).fetchall()
    return {normalise(r[0]) for r in rows if r[0]}


LIVE_QUEUE = ["pending", "waiting_review", "blocked"]


def live_sources(conn: psycopg.Connection, ws: str, kind: str) -> set[str]:
    """Normalised ``source_text`` of the workspace's queue rows of ``kind`` ("fact" or
    "concept") that still wait for the gate or the owner."""
    rows = conn.execute(
        "SELECT item->>'source_text' FROM control.general_queue "
        "WHERE workspace = %s AND item->>'kind' = %s AND status = ANY(%s)",
        (ws, kind, LIVE_QUEUE),
    ).fetchall()
    return {normalise(r[0]) for r in rows if r[0]}


def session_sources(conn: psycopg.Connection, ws: str, session_id: str) -> list[str]:
    """Normalised ``source_text`` of every fact this session has queued, whatever became of it
    (waiting, published, dropped), except rows staled by the earlier replace-on-save rule. A
    later save that rephrases one of these does not queue it again."""
    rows = conn.execute(
        "SELECT item->>'source_text' FROM control.general_queue "
        "WHERE workspace = %s AND session_id = %s AND item->>'kind' = 'fact' "
        "AND status <> 'stale' ORDER BY id",
        (ws, session_id),
    ).fetchall()
    return [normalise(r[0]) for r in rows if r[0]]
