"""Owner read models: plain dicts for the dashboard pages and the CLI list commands.

All reads run as the owner (``admin_tx``), which can read every schema, the general queue and the
control tables. ``ns`` is ``"general"`` or a workspace name; anything else is ``InvalidInput``
("unknown namespace") and a valid name that is not registered is ``Refused``. Rows never carry a
queue item's ``source_text`` except ``queue_item``, and a quarantined revision's text is not read
here at all (``sessions.quarantined_sections`` serves it on the sensitive page).

``limit`` defaults to 100 and is capped at 500; a negative ``offset`` counts as 0.
"""

import difflib
import hashlib
import re
import uuid
from typing import Any

from psycopg import sql

from treant import canary, names, settings
from treant.db import Database
from treant.errors import InvalidInput
from treant.owner import Refused

MAX_LIMIT = 500
DEFAULT_LIMIT = 100
SIMILAR_RATIO = 0.8
SIMILAR_CAP = 2000  # open queue items compared for similar_to: the newest 2000 only
_OPEN = ("pending", "waiting_review", "blocked")
_SPACES = re.compile(r"\s+")


def _page(limit: int, offset: int) -> tuple[int, int]:
    try:
        return max(1, min(int(limit), MAX_LIMIT)), max(0, int(offset))
    except (TypeError, ValueError):
        raise InvalidInput("bad paging") from None


def _ns(conn: Any, ns: str) -> sql.Identifier:
    """Validated schema identifier for ``ns``; the workspace must be registered."""
    if ns == "general":
        return sql.Identifier("general")
    try:
        schema = names.ws_schema(ns)
    except ValueError:
        raise InvalidInput("unknown namespace") from None
    _require_ws(conn, ns)
    return sql.Identifier(schema)


def _require_ws(conn: Any, ws: str) -> None:
    if conn.execute("SELECT 1 FROM control.workspaces WHERE name = %s", (ws,)).fetchone() is None:
        raise Refused("workspace not available")


def _ws_only(conn: Any, ws: str) -> sql.Identifier:
    if ws == "general":
        raise InvalidInput("unknown namespace")
    return _ns(conn, ws)


def _uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def _all_workspaces(conn: Any) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM control.workspaces ORDER BY name")]


# --- workspaces ------------------------------------------------------------------------------


def workspace(db: Database, name: str) -> dict | None:
    """One workspace as ``workspaces`` shows it, or None when it is not registered."""
    return next(iter(_workspace_rows(db, name)), None)


def workspaces(db: Database) -> list[dict]:
    return _workspace_rows(db, None)


def _workspace_rows(db: Database, only: str | None) -> list[dict]:
    with db.admin_tx() as conn:
        rows = conn.execute(
            "SELECT w.name, w.status, w.created_at, w.settings, "
            "(SELECT count(*) FROM control.tokens t WHERE t.workspace = w.name "
            " AND t.revoked_at IS NULL), "
            "(SELECT count(*) FROM control.tokens t WHERE t.workspace = w.name "
            " AND t.revoked_at IS NOT NULL), "
            "(SELECT count(*) FROM control.general_queue q WHERE q.workspace = w.name "
            " AND q.status = ANY(%s)), "
            "(SELECT count(*) FROM control.jobs j WHERE j.workspace = w.name "
            " AND j.status = 'dead') "
            "FROM control.workspaces w WHERE (%s::text IS NULL OR w.name = %s) ORDER BY w.name",
            (list(_OPEN), only, only),
        ).fetchall()
        out = []
        for name, status, created, raw, active, revoked, queue_open, dead in rows:
            try:
                dumped: dict | None = settings.dump(settings.parse(raw or {}))
            except InvalidInput:
                dumped = None
            schema = sql.Identifier(names.ws_schema(name))
            held = conn.execute(
                sql.SQL("SELECT count(*) FROM {}.revisions WHERE status = 'quarantined'").format(
                    schema
                )
            ).fetchone()[0]
            out.append(
                {
                    "name": name,
                    "status": status,
                    "created_at": created,
                    "settings": dumped,
                    "tokens_active": active,
                    "tokens_revoked": revoked,
                    "queue_open": queue_open,
                    "quarantined": held,
                    "jobs_dead": dead,
                }
            )
    return out


def targets(db: Database, ws: str) -> list[dict]:
    """A workspace's targets (kind, value), ordered by kind then value."""
    with db.admin_tx() as conn:
        schema = _ws_only(conn, ws)
        rows = conn.execute(
            sql.SQL("SELECT kind, value FROM {}.targets ORDER BY kind, value").format(schema)
        ).fetchall()
    return [{"kind": k, "value": v} for k, v in rows]


# --- the general queue -----------------------------------------------------------------------


def _label(item: dict) -> str:
    return str((item.get("name") if item.get("kind") == "concept" else item.get("text")) or "")


def _norm(text: str) -> str:
    return _SPACES.sub(" ", text.casefold()).strip()


def _similar_hints(conn: Any, wanted: list[tuple[int, str, str]]) -> dict[int, int]:
    """For each (id, kind, text) in ``wanted``, the earliest earlier open item of the same kind
    that is at least ``SIMILAR_RATIO`` similar. Compared against the newest ``SIMILAR_CAP`` open
    items, whatever the page, so paging does not change a hint."""
    rows = conn.execute(
        "SELECT id, item FROM control.general_queue WHERE status = ANY(%s) "
        "ORDER BY id DESC LIMIT %s",
        (list(_OPEN), SIMILAR_CAP),
    ).fetchall()
    by_kind: dict[Any, list[tuple[int, str]]] = {}
    for qid, item in sorted(rows, key=lambda r: r[0]):
        by_kind.setdefault(item.get("kind"), []).append((qid, _norm(_label(item))))
    found: dict[int, int] = {}
    matcher = difflib.SequenceMatcher(None)
    for qid, kind, text in wanted:
        # seq2 is the side SequenceMatcher caches its index for: set it once per wanted item.
        matcher.set_seq2(_norm(text))
        for other_id, other in by_kind.get(kind, ()):
            if other_id >= qid:
                break
            matcher.set_seq1(other)
            if (
                matcher.real_quick_ratio() >= SIMILAR_RATIO
                and matcher.quick_ratio() >= SIMILAR_RATIO
                and matcher.ratio() >= SIMILAR_RATIO
            ):
                found[qid] = other_id
                break
    return found


def queue(
    db: Database,
    status: str | None,
    ws: str | None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    hints: bool = True,
) -> list[dict]:
    """Queue rows in id order; ``hints`` adds ``similar_to`` for open rows (the CLI, which does
    not print it, passes False to skip the comparison)."""
    limit, offset = _page(limit, offset)
    with db.admin_tx() as conn:
        rows = conn.execute(
            "SELECT id, workspace, status, item, rewrites, reasons, created_at "
            "FROM control.general_queue "
            "WHERE (%(s)s::text IS NULL OR status = %(s)s) "
            "AND (%(w)s::text IS NULL OR workspace = %(w)s) ORDER BY id LIMIT %(l)s OFFSET %(o)s",
            {"s": status, "w": ws, "l": limit, "o": offset},
        ).fetchall()
        wanted = [
            (qid, item.get("kind"), _label(item)) for qid, _, st, item, *_ in rows if st in _OPEN
        ]
        similar = _similar_hints(conn, wanted) if wanted and hints else {}
    return [
        {
            "id": qid,
            "workspace": w,
            "status": st,
            "kind": item.get("kind"),
            "text": _label(item),
            "name": item.get("name") if item.get("kind") == "concept" else None,
            "type": item.get("type"),
            "rewrites": rewrites,
            "categories": sorted({r["category"] for r in reasons}),
            "created_at": created,
            "similar_to": similar.get(qid),
        }
        for qid, w, st, item, rewrites, reasons, created in rows
    ]


def queue_item(db: Database, queue_id: int) -> dict | None:
    with db.admin_tx() as conn:
        row = conn.execute(
            "SELECT workspace, item, status, reasons, rewrites, created_at, decided_at, "
            "session_id::text, revision_id::text, published_id::text "
            "FROM control.general_queue WHERE id = %s",
            (queue_id,),
        ).fetchone()
    if row is None:
        return None
    ws, item, status, reasons, rewrites, created, decided, session_id, revision_id, pub = row
    return {
        "id": queue_id,
        "workspace": ws,
        "status": status,
        "kind": item.get("kind"),
        "type": item.get("type"),
        "name": item.get("name"),
        "aliases": list(item.get("aliases") or []),
        "text": item.get("text"),
        "concept": item.get("concept"),
        "source_text": item.get("source_text"),
        "rewrites": rewrites,
        "reasons": [{k: r.get(k) for k in ("category", "start", "end")} for r in reasons],
        "created_at": created,
        "decided_at": decided,
        "session_id": session_id,
        "revision_id": revision_id,
        "published_id": pub,
    }


# --- jobs, quarantine, sessions --------------------------------------------------------------


def jobs(
    db: Database,
    status: str | None,
    ws: str | None,
    kind: str | None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> list[dict]:
    limit, offset = _page(limit, offset)
    with db.admin_tx() as conn:
        rows = conn.execute(
            "SELECT id, workspace, kind, key, status, attempts, error_category, run_after, "
            "created_at FROM control.jobs "
            "WHERE (%(s)s::text IS NULL OR status = %(s)s) "
            "AND (%(w)s::text IS NULL OR workspace = %(w)s) "
            "AND (%(k)s::text IS NULL OR kind = %(k)s) ORDER BY id LIMIT %(l)s OFFSET %(o)s",
            {"s": status, "w": ws, "k": kind, "l": limit, "o": offset},
        ).fetchall()
    keys = ("id", "workspace", "kind", "key", "status", "attempts", "error_category")
    return [dict(zip(keys, r[:7])) | {"run_after": r[7], "created_at": r[8]} for r in rows]


def quarantined(db: Database, ws: str | None = None) -> list[dict]:
    """Metadata of quarantined revisions, oldest first per workspace; never the text."""
    with db.admin_tx() as conn:
        if ws is not None:
            _ws_only(conn, ws)
        targets = [ws] if ws is not None else _all_workspaces(conn)
        out = []
        for w in targets:
            schema = sql.Identifier(names.ws_schema(w))
            for rid, created, removed in conn.execute(
                sql.SQL(
                    "SELECT id, created_at, removed FROM {}.revisions "
                    "WHERE status = 'quarantined' ORDER BY created_at"
                ).format(schema)
            ):
                out.append(
                    {
                        "workspace": w,
                        "revision_id": str(rid),
                        "created_at": created,
                        "removed": removed,
                    }
                )
    return out


def sessions(db: Database, ws: str, limit: int = DEFAULT_LIMIT, offset: int = 0) -> list[dict]:
    limit, offset = _page(limit, offset)
    with db.admin_tx() as conn:
        schema = _ws_only(conn, ws)
        rows = conn.execute(
            sql.SQL(
                "SELECT s.id, s.client_session_id, s.token_id, s.created_at "
                "FROM {}.sessions s ORDER BY s.created_at DESC, s.id LIMIT %s OFFSET %s"
            ).format(schema),
            (limit, offset),
        ).fetchall()
        ids = [r[0] for r in rows]
        labels = dict(
            conn.execute(
                "SELECT id, label FROM control.tokens WHERE id = ANY(%s)", ([r[2] for r in rows],)
            ).fetchall()
        )
        revs = conn.execute(
            sql.SQL(
                "SELECT r.id, r.session_id, r.n, r.status, r.created_at, "
                "EXISTS (SELECT 1 FROM {}.generations g WHERE g.revision_id = r.id) "
                "FROM {}.revisions r WHERE r.session_id = ANY(%s) ORDER BY r.n"
            ).format(schema, schema),
            (ids,),
        ).fetchall()
        distill = dict(
            conn.execute(
                "SELECT key, status FROM control.jobs "
                "WHERE workspace = %s AND kind = 'distill' AND key = ANY(%s)",
                (ws, [str(r[0]) for r in revs]),
            ).fetchall()
        )
    by_session: dict[Any, list[dict]] = {}
    for rid, sid, n, status, created, has_gen in revs:
        by_session.setdefault(sid, []).append(
            {
                "id": str(rid),
                "n": n,
                "status": status,
                "created_at": created,
                "has_generation": has_gen,
                "distill_status": distill.get(str(rid)),
            }
        )
    return [
        {
            "id": str(sid),
            "client_session_id": client,
            "token_id": token,
            "token_label": labels.get(token),
            "created_at": created,
            "revisions": by_session.get(sid, []),
        }
        for sid, client, token, created in rows
    ]


# --- graph -----------------------------------------------------------------------------------


def _like_prefix(q: str) -> str:
    return q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def nodes(
    db: Database, ns: str, q: str | None, limit: int = DEFAULT_LIMIT, offset: int = 0
) -> list[dict]:
    limit, offset = _page(limit, offset)
    pattern = _like_prefix(q) if q else None
    with db.admin_tx() as conn:
        schema = _ns(conn, ns)
        rows = conn.execute(
            sql.SQL(
                "SELECT id, kind, type, name, aliases, status, confidence, created_at "
                "FROM {}.nodes WHERE (%(p)s::text IS NULL OR lower(name) LIKE %(p)s "
                "OR EXISTS (SELECT 1 FROM unnest(aliases) a WHERE lower(a) LIKE %(p)s)) "
                "ORDER BY lower(name), id LIMIT %(l)s OFFSET %(o)s"
            ).format(schema),
            {"p": pattern, "l": limit, "o": offset},
        ).fetchall()
    keys = ("id", "kind", "type", "name", "aliases", "status", "confidence", "created_at")
    return [dict(zip(keys, r)) | {"id": str(r[0]), "aliases": list(r[4])} for r in rows]


def node(db: Database, ns: str, node_id: str) -> dict | None:
    """The node, its facts (all statuses), edges both ways with the other end's name, and up to
    20 journal entries of revisions supporting the node or its facts (newest first; none for
    ``general``)."""
    with db.admin_tx() as conn:
        schema = _ns(conn, ns)
        nid = _uuid(node_id)
        if nid is None:
            return None
        row = conn.execute(
            sql.SQL(
                "SELECT id, kind, type, name, aliases, status, confidence, created_at, "
                "valid_from, valid_to FROM {}.nodes WHERE id = %s"
            ).format(schema),
            (nid,),
        ).fetchone()
        if row is None:
            return None
        facts = conn.execute(
            sql.SQL(
                "SELECT id, type, text, status, version, created_at FROM {}.facts "
                "WHERE node_id = %s ORDER BY created_at, id"
            ).format(schema),
            (nid,),
        ).fetchall()
        edges = conn.execute(
            sql.SQL(
                "SELECT e.id, e.type, e.status, 'out', o.id, o.name FROM {s}.edges e "
                "JOIN {s}.nodes o ON o.id = e.dst WHERE e.src = %(n)s "
                "UNION ALL "
                "SELECT e.id, e.type, e.status, 'in', o.id, o.name FROM {s}.edges e "
                "JOIN {s}.nodes o ON o.id = e.src WHERE e.dst = %(n)s ORDER BY 4 DESC, 6, 1"
            ).format(s=schema),
            {"n": nid},
        ).fetchall()
        journal: list[dict] = []
        if ns != "general":
            fact_ids = [f[0] for f in facts]
            for rid, text, created in conn.execute(
                sql.SQL(
                    "SELECT j.revision_id, j.text, j.created_at FROM {s}.journal_entries j "
                    "WHERE j.revision_id IN (SELECT revision_id FROM {s}.support WHERE "
                    "(item_kind = 'node' AND item_id = %(n)s) "
                    "OR (item_kind = 'fact' AND item_id = ANY(%(f)s))) "
                    "ORDER BY j.created_at DESC, j.id LIMIT 20"
                ).format(s=schema),
                {"n": nid, "f": fact_ids},
            ):
                journal.append({"revision_id": str(rid), "text": text, "created_at": created})
    keys = ("id", "kind", "type", "name", "aliases", "status", "confidence", "created_at")
    out = dict(zip(keys, row[:8])) | {"valid_from": row[8], "valid_to": row[9]}
    out["id"], out["aliases"] = str(row[0]), list(row[4])
    out["facts"] = [
        {
            "id": str(i),
            "type": t,
            "text": x,
            "status": s,
            "version": v,
            "created_at": c,
        }
        for i, t, x, s, v, c in facts
    ]
    out["edges"] = [
        {
            "id": str(i),
            "type": t,
            "status": s,
            "direction": d,
            "other_id": str(oid),
            "other_name": oname,
        }
        for i, t, s, d, oid, oname in edges
    ]
    out["journal"] = journal
    return out


GRAPH_LIMIT = 150
GRAPH_DEPTH = 3

# Current-edge count per node, joined to ``{s}.nodes n`` as ``d``.
_DEGREE = (
    "LEFT JOIN (SELECT x, count(*) c FROM (SELECT src x FROM {s}.edges WHERE status = 'current' "
    "UNION ALL SELECT dst FROM {s}.edges WHERE status = 'current') q GROUP BY x) d ON d.x = n.id "
)


def _refines(ns: str) -> sql.Composable:
    return sql.SQL("NULL::uuid") if ns == "general" else sql.SQL("n.refines")


def graph(
    db: Database,
    ns: str,
    limit: int = GRAPH_LIMIT,
    *,
    q: str | None = None,
    types: list[str] | None = None,
    unlinked: bool = True,
) -> dict:
    """Current nodes and the current edges between them, best-connected first, at most ``limit``
    nodes. ``types`` keeps only those node types; ``q`` keeps nodes whose name or an alias contains
    it, plus their direct neighbours; ``unlinked=False`` drops nodes with no edge (a local concept
    refining a general one counts as linked). See ``_graph_result`` for the shape; ``types`` in the
    result lists every current node type with its count, ``hidden_unlinked`` how many were
    dropped by ``unlinked=False``."""
    limit = max(1, min(int(limit), GRAPH_LIMIT))
    pattern = "%" + _like_prefix(q)[:-1] + "%" if q else None
    linked = sql.SQL("(coalesce(d.c, 0) > 0 OR {r} IS NOT NULL)").format(r=_refines(ns))
    with db.admin_tx() as conn:
        schema = _ns(conn, ns)
        rows = conn.execute(
            sql.SQL(
                "WITH hit AS (SELECT id FROM {s}.nodes WHERE status = 'current' AND "
                "(lower(name) LIKE %(p)s OR EXISTS (SELECT 1 FROM unnest(aliases) a "
                "WHERE lower(a) LIKE %(p)s))), "
                "near AS (SELECT id FROM hit UNION SELECT CASE WHEN e.src = h.id THEN e.dst "
                "ELSE e.src END FROM {s}.edges e JOIN hit h ON h.id IN (e.src, e.dst) "
                "WHERE e.status = 'current') "
                "SELECT n.id, n.name, n.type, n.kind, {r}, NULL::int, count(*) OVER () "
                "FROM {s}.nodes n " + _DEGREE + "WHERE n.status = 'current' "
                "AND (%(t)s::text[] IS NULL OR n.type = ANY(%(t)s)) "
                "AND (%(p)s::text IS NULL OR n.id IN (SELECT id FROM near)) "
                "AND (%(u)s OR {linked}) "
                "ORDER BY coalesce(d.c, 0) DESC, lower(n.name), n.id LIMIT %(l)s"
            ).format(s=schema, r=_refines(ns), linked=linked),
            {"p": pattern, "t": types or None, "u": unlinked, "l": limit},
        ).fetchall()
        total = rows[0][6] if rows else 0
        type_counts = conn.execute(
            sql.SQL(
                "SELECT type, count(*) FROM {}.nodes WHERE status = 'current' "
                "GROUP BY type ORDER BY 2 DESC, 1"
            ).format(schema)
        ).fetchall()
        hidden = 0
        if not unlinked:
            hidden = conn.execute(
                sql.SQL(
                    "SELECT count(*) FROM {s}.nodes n " + _DEGREE + "WHERE n.status = 'current' "
                    "AND (%(t)s::text[] IS NULL OR n.type = ANY(%(t)s)) AND NOT {linked}"
                ).format(s=schema, linked=linked),
                {"t": types or None},
            ).fetchone()[0]
        out = _graph_result(conn, schema, ns, [r[:6] for r in rows], total)
    return out | {"types": [(t, c) for t, c in type_counts], "hidden_unlinked": hidden}


def neighbourhood(
    db: Database, ns: str, focus: str, depth: int = 1, limit: int = GRAPH_LIMIT
) -> dict | None:
    """The current nodes within ``depth`` (1 to 3) current edges of ``focus``, edges followed
    both ways, nearest first, at most ``limit`` nodes; None when ``focus`` is not a current node.
    Each node carries ``depth`` (0 for the focus); ``focus`` in the result is the focus node."""
    depth = max(1, min(int(depth), GRAPH_DEPTH))
    limit = max(1, min(int(limit), GRAPH_LIMIT))
    with db.admin_tx() as conn:
        schema = _ns(conn, ns)
        fid = _uuid(focus)
        if fid is None:
            return None
        rows = conn.execute(
            sql.SQL(
                "WITH RECURSIVE walk (id, depth) AS (SELECT id, 0 FROM {s}.nodes "
                "WHERE id = %(f)s AND status = 'current' "
                "UNION SELECT CASE WHEN e.src = w.id THEN e.dst ELSE e.src END, w.depth + 1 "
                "FROM walk w JOIN {s}.edges e ON e.status = 'current' AND w.id IN (e.src, e.dst) "
                "WHERE w.depth < %(d)s), "
                "near AS (SELECT id, min(depth) depth FROM walk GROUP BY id) "
                "SELECT n.id, n.name, n.type, n.kind, {r}, w.depth, count(*) OVER () "
                "FROM near w JOIN {s}.nodes n ON n.id = w.id "
                + _DEGREE
                + "WHERE n.status = 'current' "
                "ORDER BY w.depth, coalesce(d.c, 0) DESC, lower(n.name), n.id LIMIT %(l)s"
            ).format(s=schema, r=_refines(ns)),
            {"f": fid, "d": depth, "l": limit},
        ).fetchall()
        if not rows:
            return None
        out = _graph_result(conn, schema, ns, [r[:6] for r in rows], rows[0][6])
    return out | {"focus": out["nodes"][0], "depth": depth}


def _graph_result(conn: Any, schema: sql.Identifier, ns: str, rows: list, total: int) -> dict:
    """Nodes for ``rows`` (id, name, type, kind, refines, depth), the current edges among them,
    and the general concepts they refine (extra nodes, joined by ``refines`` edges). Each node
    has ``general``: whether it lives in the general namespace."""
    ids = [r[0] for r in rows]
    edges = conn.execute(
        sql.SQL(
            "SELECT src, dst, type FROM {}.edges WHERE status = 'current' "
            "AND src = ANY(%(i)s) AND dst = ANY(%(i)s) ORDER BY created_at, id"
        ).format(schema),
        {"i": ids},
    ).fetchall()
    concepts = conn.execute(
        "SELECT id, name, type, kind FROM general.nodes "
        "WHERE status = 'current' AND id = ANY(%s) ORDER BY lower(name), id",
        ([r[4] for r in rows if r[4] is not None],),
    ).fetchall()
    general = ns == "general"
    nodes = [
        {"id": str(i), "name": n, "type": t, "kind": k, "general": general, "depth": dp}
        for i, n, t, k, _, dp in rows
    ] + [
        {"id": str(i), "name": n, "type": t, "kind": k, "general": True, "depth": None}
        for i, n, t, k in concepts
    ]
    have = {c[0] for c in concepts}
    links = [{"src": str(s), "dst": str(d), "type": t} for s, d, t in edges] + [
        {"src": str(r[0]), "dst": str(r[4]), "type": "refines"} for r in rows if r[4] in have
    ]
    return {
        "nodes": nodes,
        "edges": links,
        "total": total,
        "shown": len(rows),
        "truncated": total > len(rows),
    }


def fact_history(db: Database, ns: str, fact_id: str) -> list[dict]:
    """Stored versions (oldest first, with reason and time), then the current row last."""
    with db.admin_tx() as conn:
        schema = _ns(conn, ns)
        fid = _uuid(fact_id)
        if fid is None:
            return []
        versions = conn.execute(
            sql.SQL(
                "SELECT version, data, reason, changed_at FROM {}.versions "
                "WHERE item_kind = 'fact' AND item_id = %s ORDER BY version"
            ).format(schema),
            (fid,),
        ).fetchall()
        current = conn.execute(
            sql.SQL(
                "SELECT version, jsonb_build_object('id', id, 'type', type, 'text', text, "
                "'node_id', node_id, 'status', status, 'version', version, "
                "'created_at', created_at), created_at FROM {}.facts WHERE id = %s"
            ).format(schema),
            (fid,),
        ).fetchone()
    out = [
        {"version": v, "data": d, "reason": r, "changed_at": at, "current": False}
        for v, d, r, at in versions
    ]
    if current is not None:
        out.append(
            {
                "version": current[0],
                "data": current[1],
                "reason": None,
                "changed_at": None,
                "current": True,
            }
        )
    return out


# --- fact duplicates -------------------------------------------------------------------------

DUPLICATE_THRESHOLD = 0.88


def _threshold(value: Any) -> float:
    try:
        t = float(value)
    except (TypeError, ValueError):
        raise InvalidInput("threshold") from None
    if not 0 < t <= 1:  # also refuses NaN
        raise InvalidInput("threshold")
    return t


def _group_key(facts: list[dict]) -> str:
    """16 hex characters naming a group by its fact ids (a changed membership is a new key)."""
    return hashlib.sha256(",".join(sorted(f["id"] for f in facts)).encode()).hexdigest()[:16]


def duplicates(db: Database, ns: str, threshold: float = DUPLICATE_THRESHOLD) -> list[dict]:
    """Groups of current facts of the same type whose stored vectors are at least ``threshold``
    cosine-similar: the connected components of the pair graph, largest first. Each group has
    ``key`` (stable for the same ids), ``type`` and ``facts`` (id, type, text, node_id,
    node_name, created_at), newest first. Facts without a vector yet are left out. Computed on
    request; nothing is stored."""
    threshold = _threshold(threshold)
    with db.admin_tx() as conn:
        s = _ns(conn, ns)
        pairs = conn.execute(
            sql.SQL(
                "SELECT a.id::text, b.id::text FROM {s}.facts a "
                "JOIN {s}.embeddings ea ON ea.item_kind = 'fact' AND ea.item_id = a.id "
                "JOIN {s}.facts b ON b.id > a.id AND b.status = 'current' AND b.type = a.type "
                "JOIN {s}.embeddings eb ON eb.item_kind = 'fact' AND eb.item_id = b.id "
                "WHERE a.status = 'current' AND 1 - (ea.vec <=> eb.vec) >= %s"
            ).format(s=s),
            (threshold,),
        ).fetchall()
        parent: dict[str, str] = {}

        def root(x: str) -> str:
            while parent.setdefault(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for a, b in pairs:
            ra, rb = root(a), root(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
        if not parent:
            return []
        rows = conn.execute(
            sql.SQL(
                "SELECT f.id::text, f.type, f.text, f.node_id::text, n.name, f.created_at "
                "FROM {s}.facts f LEFT JOIN {s}.nodes n ON n.id = f.node_id "
                "WHERE f.id = ANY(%s::uuid[]) ORDER BY f.created_at DESC, f.id"
            ).format(s=s),
            (list(parent),),
        ).fetchall()
    keys = ("id", "type", "text", "node_id", "node_name", "created_at")
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(root(r[0]), []).append(dict(zip(keys, r)))
    out = [
        {"key": _group_key(facts), "type": facts[0]["type"], "facts": facts}
        for facts in groups.values()
    ]
    out.sort(key=lambda g: (-len(g["facts"]), -g["facts"][0]["created_at"].timestamp(), g["key"]))
    return out


# --- merges, conflicts, mistakes, audit ------------------------------------------------------


def merges(db: Database, ws: str, open_only: bool = True) -> list[dict]:
    with db.admin_tx() as conn:
        s = _ws_only(conn, ws)
        rows = conn.execute(
            sql.SQL(
                "SELECT m.id, m.node_a::text, m.node_b::text, m.score, a.name, b.name, "
                "m.created_at, m.resolved_at, m.resolution FROM {s}.merge_flags m "
                "LEFT JOIN {s}.nodes a ON a.id = m.node_a LEFT JOIN {s}.nodes b ON b.id = m.node_b "
                "WHERE (NOT %(o)s OR m.resolved_at IS NULL) ORDER BY m.id"
            ).format(s=s),
            {"o": open_only},
        ).fetchall()
    out = []
    for i, a, b, score, name_a, name_b, created, resolved, resolution in rows:
        row = {
            "id": i,
            "node_a": a,
            "node_b": b,
            "score": score,
            "name_a": name_a,
            "name_b": name_b,
            "created_at": created,
        }
        if not open_only:
            row |= {"resolved_at": resolved, "resolution": resolution}
        out.append(row)
    return out


def conflicts(db: Database, ws: str, open_only: bool = True) -> list[dict]:
    with db.admin_tx() as conn:
        s = _ws_only(conn, ws)
        rows = conn.execute(
            sql.SQL(
                "SELECT c.id, c.new_fact::text, c.old_fact::text, n.text, o.text, c.created_at, "
                "c.resolved_at, c.resolution FROM {s}.conflicts c "
                "LEFT JOIN {s}.facts n ON n.id = c.new_fact "
                "LEFT JOIN {s}.facts o ON o.id = c.old_fact "
                "WHERE (NOT %(o)s OR c.resolved_at IS NULL) ORDER BY c.id"
            ).format(s=s),
            {"o": open_only},
        ).fetchall()
    out = []
    for i, new, old, new_text, old_text, created, resolved, resolution in rows:
        row = {
            "id": i,
            "new_fact": new,
            "old_fact": old,
            "new_text": new_text,
            "old_text": old_text,
            "created_at": created,
        }
        if not open_only:
            row |= {"resolved_at": resolved, "resolution": resolution}
        out.append(row)
    return out


_STALE_COLS = (
    "sf.id, sf.fact_id::text, f.text, sf.repo, sf.commit, sf.missing, sf.created_at, "
    "sf.resolved_at, sf.resolution"
)


def _stale_row(row: tuple, with_resolution: bool) -> dict:
    i, fact_id, text, repo, commit, missing, created, resolved, resolution = row
    out = {
        "id": i,
        "fact_id": fact_id,
        "text": text,
        "repo": repo,
        "commit": commit,
        "missing": missing,
        "created_at": created,
    }
    if with_resolution:
        out |= {"resolved_at": resolved, "resolution": resolution}
    return out


def stale_flags(db: Database, ws: str, open_only: bool = True) -> list[dict]:
    """Stale flags of a workspace. Open ones only where the fact is still current and unedited."""
    with db.admin_tx() as conn:
        s = _ws_only(conn, ws)
        rows = conn.execute(
            sql.SQL(
                "SELECT " + _STALE_COLS + " FROM {s}.stale_flags sf "
                "JOIN {s}.facts f ON f.id = sf.fact_id "
                "WHERE (NOT %(o)s OR (sf.resolved_at IS NULL AND f.status = 'current' "
                "AND f.version = sf.fact_version)) ORDER BY sf.id"
            ).format(s=s),
            {"o": open_only},
        ).fetchall()
    return [_stale_row(r, not open_only) for r in rows]


def stale_flag(db: Database, ws: str, flag_id: int) -> dict | None:
    """One stale flag, open or resolved, with its fact text."""
    with db.admin_tx() as conn:
        s = _ws_only(conn, ws)
        row = conn.execute(
            sql.SQL(
                "SELECT " + _STALE_COLS + " FROM {s}.stale_flags sf "
                "JOIN {s}.facts f ON f.id = sf.fact_id WHERE sf.id = %s"
            ).format(s=s),
            (flag_id,),
        ).fetchone()
    return None if row is None else _stale_row(row, True)


def mistakes(
    db: Database, ws: str | None, limit: int = DEFAULT_LIMIT, offset: int = 0
) -> list[dict]:
    limit, offset = _page(limit, offset)
    with db.admin_tx() as conn:
        rows = conn.execute(
            "SELECT id, workspace, at, text FROM control.mistakes "
            "WHERE (%(w)s::text IS NULL OR workspace = %(w)s) "
            "ORDER BY id DESC LIMIT %(l)s OFFSET %(o)s",
            {"w": ws, "l": limit, "o": offset},
        ).fetchall()
    return [{"id": i, "workspace": w, "at": at, "text": t} for i, w, at, t in rows]


def audit(
    db: Database, limit: int = DEFAULT_LIMIT, offset: int = 0, action_prefix: str | None = None
) -> list[dict]:
    limit, offset = _page(limit, offset)
    with db.admin_tx() as conn:
        rows = conn.execute(
            "SELECT id, at, actor, action, detail FROM control.audit "
            "WHERE (%(p)s::text IS NULL OR starts_with(action, %(p)s)) "
            "ORDER BY id DESC LIMIT %(l)s OFFSET %(o)s",
            {"p": action_prefix, "l": limit, "o": offset},
        ).fetchall()
    return [
        {"id": i, "at": at, "actor": actor, "action": action, "detail": detail}
        for i, at, actor, action, detail in rows
    ]


# --- measurements ----------------------------------------------------------------------------


def measurements(db: Database) -> dict:
    """Counts for the owner's measurements page. ``gate_blocks`` counts the reason categories of
    queue items currently ``blocked``. ``searches_7d`` is None: search writes no per-search metric
    (only ``isolation_fault``), so there is nothing to total."""
    with db.admin_tx() as conn:
        active = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM control.workspaces WHERE status = 'active' ORDER BY name"
            )
        ]
        out: dict[str, dict] = {
            ws: {
                "queue": {},
                "jobs": {},
                "gate_blocks": {},
                "searches_7d": None,
                "mistakes_30d": 0,
            }
            for ws in active
        }
        for ws, status, n in conn.execute(
            "SELECT workspace, status, count(*) FROM control.general_queue GROUP BY 1, 2"
        ):
            if ws in out:
                out[ws]["queue"][status] = n
        for ws, status, n in conn.execute(
            "SELECT workspace, status, count(*) FROM control.jobs GROUP BY 1, 2"
        ):
            if ws in out:
                out[ws]["jobs"][status] = n
        for ws, category, n in conn.execute(
            "SELECT q.workspace, r->>'category', count(*) FROM control.general_queue q, "
            "jsonb_array_elements(q.reasons) r WHERE q.status = 'blocked' GROUP BY 1, 2"
        ):
            if ws in out:
                out[ws]["gate_blocks"][category] = n
        for ws, n in conn.execute(
            "SELECT workspace, count(*) FROM control.mistakes "
            "WHERE at > now() - interval '30 days' GROUP BY 1"
        ):
            if ws in out:
                out[ws]["mistakes_30d"] = n
        latest = {
            name: (at, value)
            for name, at, value in conn.execute(
                "SELECT DISTINCT ON (name) name, at, value FROM control.metrics "
                "WHERE name IN ('isolation.faults', 'isolation.checks') AND workspace IS NULL "
                "ORDER BY name, at DESC"
            )
        }
    faults = latest.get("isolation.faults")
    checks = latest.get("isolation.checks")
    isolation = None
    if faults or checks:
        isolation = {
            "at": max(m[0] for m in (faults, checks) if m),
            "faults": int(faults[1]) if faults else None,
            "checks": int(checks[1]) if checks else None,
        }
    return {"workspaces": out, "isolation": isolation, "canaries": canary.counts(db)}


def waiting_on_concept(db: Database, ws: str, name: str) -> int:
    """Current unattached facts in ``ws`` waiting for the general concept ``name`` (any case)."""
    with db.admin_tx() as conn:
        schema = _ws_only(conn, ws)
        row = conn.execute(
            sql.SQL(
                "SELECT count(*) FROM {}.facts WHERE status = 'current' "
                "AND node_id IS NULL AND lower(wait_concept) = lower(%s)"
            ).format(schema),
            (name,),
        ).fetchone()
    return int(row[0])
