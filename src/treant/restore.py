"""Owner action: restore what the replace-on-save rule hid.

Until saves became additive, a later save of a session superseded every item only that
session's older revisions supported and the new one did not repeat (reason ``reconcile`` in
``versions``). ``run`` sets those items current again, in one transaction per namespace:

1. Nodes, oldest first. A node whose name or alias a current node of the same kind already
   carries is not restored; it maps to that node. A local concept whose general concept is no
   longer current stays hidden.
2. Edges, with a mapped endpoint replaced by its current namesake, whose two endpoints are
   then current and distinct and that have no current twin (same src, dst, type).
3. Facts, newest first, so the newest wording of a fact saved several times wins. An open
   thread is never restored (a save's open threads still replace the session's older ones). A
   fact on a node that is neither current nor mapped stays hidden. A fact within FACT_RATIO of
   a current fact of the same type on the same node stays hidden as a duplicate. The others
   are set current, and moved to the mapped node when there is one.

Only items whose newest ``versions`` row has reason ``reconcile`` are candidates: anything the
owner retracted, closed or edited since, or that a newer fact superseded by reference, is left
alone. One gap: a fact that reconcile had already superseded when a newer fact then declared it
superseded has no snapshot of that second event, so it counts as a candidate. Every restore writes a ``versions`` snapshot with reason ``restore``, so a second run
finds nothing. Workspaces run as the admin role, ``general`` as the publisher. Output and logs
carry counts only.
"""

import logging

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from treant import graph, jobs, names
from treant.apply import _named
from treant.db import Database
from treant.errors import InvalidInput
from treant.reconcile import FACT_RATIO, normalise, similarity

log = logging.getLogger(__name__)

COUNTS = (
    "nodes_restored",
    "nodes_mapped",
    "nodes_orphan",
    "edges_restored",
    "edges_skipped",
    "facts_restored",
    "facts_duplicate",
    "facts_open_thread",
    "facts_orphan",
)


class _DryRun(Exception):
    pass


def _candidates(conn: psycopg.Connection, schema: sql.Identifier, kind: str, cols: str, order: str):
    """Superseded rows of ``kind`` whose newest version snapshot has reason ``reconcile``."""
    table = sql.Identifier({"node": "nodes", "edge": "edges", "fact": "facts"}[kind])
    return conn.execute(
        sql.SQL(
            "SELECT " + cols + " FROM {s}.{t} t WHERE t.status = 'superseded' AND "
            "(SELECT v.reason FROM {s}.versions v WHERE v.item_kind = %s AND v.item_id = t.id "
            " ORDER BY v.version DESC LIMIT 1) = 'reconcile' ORDER BY " + order + " FOR UPDATE OF t"
        ).format(s=schema, t=table),
        (kind,),
    ).fetchall()


def _restore(conn: psycopg.Connection, ns: str, counts: dict[str, int]) -> None:
    schema = graph.schema_of(ns)

    mapped: dict[str, str] = {}
    for node_id, kind, name, aliases, refines in _candidates(
        conn,
        schema,
        "node",
        "t.id::text, t.kind, t.name, t.aliases, "
        + ("NULL" if ns == "general" else "t.refines::text"),  # general nodes refine nothing
        "t.created_at, t.id",
    ):
        if (
            refines is not None
            and not conn.execute(
                "SELECT 1 FROM general.nodes WHERE id = %s AND status = 'current'", (refines,)
            ).fetchone()
        ):
            counts["nodes_orphan"] += 1  # a local concept whose general concept is gone
            continue
        current = conn.execute(
            sql.SQL(
                "SELECT id::text, name, aliases FROM {s}.nodes WHERE kind = %s "
                "AND status = 'current' ORDER BY created_at, id"
            ).format(s=schema),
            (kind,),
        ).fetchall()
        twin = _named(current, [name, *aliases])
        if twin:
            mapped[node_id] = twin
            counts["nodes_mapped"] += 1
        else:
            graph.set_status(conn, ns, "node", node_id, "current", "restore")
            counts["nodes_restored"] += 1

    for edge_id, src, dst, type_ in _candidates(
        conn, schema, "edge", "t.id::text, t.src::text, t.dst::text, t.type", "t.created_at, t.id"
    ):
        to_src, to_dst = mapped.get(src, src), mapped.get(dst, dst)  # follow mapped endpoints
        ok = (
            to_src != to_dst
            and conn.execute(
                sql.SQL(
                    "SELECT (SELECT count(*) FROM {s}.nodes WHERE id IN (%(src)s, %(dst)s) "
                    " AND status = 'current') = 2 AND NOT EXISTS (SELECT 1 FROM {s}.edges "
                    " WHERE src = %(src)s AND dst = %(dst)s AND type = %(type)s AND status = 'current')"
                ).format(s=schema),
                {"src": to_src, "dst": to_dst, "type": type_},
            ).fetchone()[0]
        )
        if ok:
            if (to_src, to_dst) != (src, dst):
                graph.move_edge(conn, ns, edge_id, to_src, to_dst)
            graph.set_status(conn, ns, "edge", edge_id, "current", "restore")
            counts["edges_restored"] += 1
        else:
            counts["edges_skipped"] += 1

    live_nodes = {
        r[0]
        for r in conn.execute(
            sql.SQL("SELECT id::text FROM {s}.nodes WHERE status = 'current'").format(s=schema)
        )
    }
    current: dict[tuple[str, str | None], list[str]] = {}
    for type_, node_id, text in conn.execute(
        sql.SQL("SELECT type, node_id::text, text FROM {s}.facts WHERE status = 'current'").format(
            s=schema
        )
    ):
        current.setdefault((type_, node_id), []).append(normalise(text))

    for fact_id, type_, text, node_id in _candidates(
        conn,
        schema,
        "fact",
        "t.id::text, t.type, t.text, t.node_id::text",
        "t.created_at DESC, t.id",
    ):
        if type_ == "open_thread":
            counts["facts_open_thread"] += 1
            continue
        target = node_id
        if node_id is not None and node_id not in live_nodes:
            target = mapped.get(node_id)
            if target is None:
                counts["facts_orphan"] += 1
                continue
        norm = normalise(text)
        bucket = current.setdefault((type_, target), [])
        if any(norm == t or similarity(norm, t, FACT_RATIO) >= FACT_RATIO for t in bucket):
            counts["facts_duplicate"] += 1
            continue
        graph.set_status(conn, ns, "fact", fact_id, "current", "restore")
        if target != node_id:
            graph.move_fact(conn, ns, fact_id, target)
        bucket.append(norm)
        counts["facts_restored"] += 1


def run(db: Database, ns: str, dry_run: bool = False) -> dict[str, int]:
    """Restore the items of ``ns`` (a workspace name or ``"general"``) superseded by reconcile.
    Returns counts only. ``dry_run`` makes the same decisions and rolls everything back."""
    if ns != "general":
        names.validate_workspace(ns)
        with db.control_tx() as conn:
            known = conn.execute(
                "SELECT 1 FROM control.workspaces WHERE name = %s", (ns,)
            ).fetchone()
        if known is None:
            raise InvalidInput("unknown workspace")
    counts = dict.fromkeys(COUNTS, 0)
    tx = db.publisher_tx if ns == "general" else db.admin_tx
    try:
        with tx() as conn:
            _restore(conn, ns, counts)
            if dry_run:
                raise _DryRun
            changed = counts["nodes_restored"] + counts["edges_restored"] + counts["facts_restored"]
            if changed and ns != "general":  # general vectors: worker.embed_general sweeps them
                jobs.enqueue(conn, ns, "embed", f"restore:{ns}", {"revision_id": None})
    except _DryRun:
        pass
    else:
        with db.admin_tx() as conn:
            conn.execute(
                "INSERT INTO control.audit (actor, action, detail) VALUES ('cli', %s, %s)",
                ("restore", Jsonb({"namespace": ns, **counts})),
            )
    log.info(
        "restore namespace_kind=%s %s dry_run=%s",
        "general" if ns == "general" else "workspace",
        " ".join(f"{k}={v}" for k, v in counts.items()),
        dry_run,
    )
    return counts
