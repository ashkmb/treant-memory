"""Graph store: nodes, edges and facts in ``general`` or one workspace schema.

``ns`` is ``"general"`` or a workspace name. The schema is always composed with
``sql.Identifier`` from a validated name; the caller's transaction role decides
what the database allows.
"""

import uuid

import psycopg
from psycopg import sql

from treant import names
from treant.errors import InvalidInput, NotFound

FACT_TYPES = frozenset(
    {
        "fact", "decision", "lesson", "dead_end", "procedure",
        "change", "open_thread", "preference", "warning",
    }
)  # fmt: skip
NODE_KINDS = frozenset({"concept", "local_concept", "instance"})
STATUSES = frozenset({"current", "superseded", "retracted", "closed"})
EDGE_TYPES = frozenset(
    {"uses", "depends_on", "deploys_to", "runs_on", "part_of", "replaced_by", "decided_in"}
)
_TABLES = {"fact": "facts", "node": "nodes", "edge": "edges"}


def schema_of(ns: str) -> sql.Identifier:
    if ns == "general":
        return sql.Identifier("general")
    try:
        return sql.Identifier(names.ws_schema(ns))
    except ValueError:
        raise InvalidInput("invalid namespace") from None


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise InvalidInput("invalid id") from None


def _exists(conn: psycopg.Connection, schema: sql.Identifier, table: str, item_id: str) -> bool:
    query = sql.SQL("SELECT 1 FROM {}.{} WHERE id = %s").format(schema, sql.Identifier(table))
    return conn.execute(query, (item_id,)).fetchone() is not None


def add_node(
    conn: psycopg.Connection,
    ns: str,
    kind: str,
    type: str,
    name: str,
    aliases: tuple[str, ...] | list[str] = (),
    refines: str | None = None,
) -> str:
    schema = schema_of(ns)
    if kind not in NODE_KINDS:
        raise InvalidInput("unknown node kind")
    if (kind == "concept") != (ns == "general"):
        raise InvalidInput("concept belongs in general only")
    if ns == "general":
        if refines is not None:
            raise InvalidInput("concepts do not refine")
        query = sql.SQL(
            "INSERT INTO {}.nodes (kind, type, name, aliases) VALUES (%s, %s, %s, %s) RETURNING id"
        ).format(schema)
        return str(conn.execute(query, (kind, type, name, list(aliases))).fetchone()[0])
    if kind == "local_concept" and refines is None:
        raise InvalidInput("local concept must refine a general concept")
    if refines is not None:
        refines = _uuid(refines)
        row = conn.execute(
            "SELECT kind, status FROM general.nodes WHERE id = %s", (refines,)
        ).fetchone()
        if row is None or row != ("concept", "current"):
            raise InvalidInput("refines target is not a general concept")
    query = sql.SQL(
        "INSERT INTO {}.nodes (kind, type, name, aliases, refines) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id"
    ).format(schema)
    return str(conn.execute(query, (kind, type, name, list(aliases), refines)).fetchone()[0])


def add_edge(conn: psycopg.Connection, ns: str, src: str, dst: str, type: str) -> str:
    schema = schema_of(ns)
    if type not in EDGE_TYPES:
        raise InvalidInput("unknown edge type")
    src, dst = _uuid(src), _uuid(dst)
    if src == dst:
        raise InvalidInput("self-loop edge")
    if not (_exists(conn, schema, "nodes", src) and _exists(conn, schema, "nodes", dst)):
        raise InvalidInput("edge endpoints must both be in the namespace")
    query = sql.SQL(
        "INSERT INTO {}.edges (src, dst, type) VALUES (%s, %s, %s) RETURNING id"
    ).format(schema)
    return str(conn.execute(query, (src, dst, type)).fetchone()[0])


def add_fact(
    conn: psycopg.Connection,
    ns: str,
    type: str,
    text: str,
    node_id: str | None = None,
    revision_id: str | None = None,
    wait_concept: str | None = None,
) -> str:
    """Insert a fact. ``wait_concept`` (workspace only, with no node) names the general concept
    an unattached fact waits for."""
    schema = schema_of(ns)
    if type not in FACT_TYPES:
        raise InvalidInput("unknown fact type")
    if node_id is not None:
        node_id = _uuid(node_id)
        if not _exists(conn, schema, "nodes", node_id):
            raise InvalidInput("node not in namespace")
    if revision_id is not None and ns == "general":
        raise InvalidInput("general facts have no revision")
    if wait_concept is not None and (ns == "general" or node_id is not None):
        raise InvalidInput("only an unattached workspace fact waits for a concept")
    if ns == "general":
        columns, values = ["type", "text", "node_id"], (type, text, node_id)
    else:
        revision_id = None if revision_id is None else _uuid(revision_id)
        if revision_id is not None and not _exists(conn, schema, "revisions", revision_id):
            raise InvalidInput("revision not in namespace")
        columns = ["type", "text", "node_id", "revision_id", "wait_concept"]
        values = (type, text, node_id, revision_id, wait_concept)
    marks = sql.SQL(", ").join(sql.Placeholder() for _ in columns)
    cols = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
    query = sql.SQL("INSERT INTO {}.facts ({}) VALUES ({}) RETURNING id").format(
        schema, cols, marks
    )
    return str(conn.execute(query, values).fetchone()[0])


def _snapshot(
    conn: psycopg.Connection, schema: sql.Identifier, kind: str, item_id: str, reason: str
) -> int:
    """Copy the current row into versions; return the version it was stored under."""
    table = sql.Identifier(_TABLES[kind])
    if kind == "fact":
        version_expr = sql.SQL("t.version")
    else:
        version_expr = sql.SQL(
            "(SELECT coalesce(max(v.version), 0) + 1 FROM {}.versions v "
            "WHERE v.item_kind = %s AND v.item_id = t.id)"
        ).format(schema)
    query = sql.SQL(
        "INSERT INTO {}.versions (item_kind, item_id, version, data, reason) "
        "SELECT %s, t.id, {}, to_jsonb(t), %s FROM {}.{} t WHERE t.id = %s "
        "RETURNING version"
    ).format(schema, version_expr, schema, table)
    params: tuple = (kind, kind, reason, item_id) if kind != "fact" else (kind, reason, item_id)
    row = conn.execute(query, params).fetchone()
    if row is None:
        raise NotFound("item not found")
    return row[0]


def fact_state(conn: psycopg.Connection, ns: str, fact_id: str) -> tuple[str, str, int]:
    """Lock a fact row for the rest of the transaction; return (status, text, version)."""
    schema = schema_of(ns)
    fact_id = _uuid(fact_id)
    row = conn.execute(
        sql.SQL("SELECT status, text, version FROM {}.facts WHERE id = %s FOR UPDATE").format(
            schema
        ),
        (fact_id,),
    ).fetchone()
    if row is None:
        raise NotFound("fact not found")
    return row[0], row[1], row[2]


def fact_status(conn: psycopg.Connection, ns: str, fact_id: str) -> str:
    """Lock a fact row for the rest of the transaction and return its status."""
    return fact_state(conn, ns, fact_id)[0]


def update_fact(
    conn: psycopg.Connection,
    ns: str,
    fact_id: str,
    text: str | None = None,
    status: str | None = None,
) -> int:
    schema = schema_of(ns)
    fact_id = _uuid(fact_id)
    if text is None and status is None:
        raise InvalidInput("nothing to update")
    if status is not None and status not in STATUSES:
        raise InvalidInput("unknown status")
    row = conn.execute(
        sql.SQL("SELECT type, text FROM {}.facts WHERE id = %s FOR UPDATE").format(schema),
        (fact_id,),
    ).fetchone()
    if row is None:
        raise NotFound("fact not found")
    if status == "closed" and row[0] != "open_thread":
        raise InvalidInput("only open threads can be closed")
    _snapshot(conn, schema, "fact", fact_id, "update")
    new_text = row[1] if text is None else text
    new_status = status
    version = conn.execute(
        sql.SQL(
            "UPDATE {}.facts SET text = %s, status = coalesce(%s, status), "
            "version = version + 1 WHERE id = %s RETURNING version"
        ).format(schema),
        (new_text, new_status, fact_id),
    ).fetchone()[0]
    if new_text != row[1]:
        # Stale vector: delete it so the embed worker re-embeds the new text.
        conn.execute(
            sql.SQL("DELETE FROM {}.embeddings WHERE item_kind = 'fact' AND item_id = %s").format(
                schema
            ),
            (fact_id,),
        )
    return version


def retract(conn: psycopg.Connection, ns: str, item_kind: str, item_id: str) -> None:
    schema = schema_of(ns)
    if item_kind not in _TABLES:
        raise InvalidInput("unknown item kind")
    item_id = _uuid(item_id)
    row = conn.execute(
        sql.SQL("SELECT status FROM {}.{} WHERE id = %s FOR UPDATE").format(
            schema, sql.Identifier(_TABLES[item_kind])
        ),
        (item_id,),
    ).fetchone()
    if row is None:
        raise NotFound("item not found")
    if row[0] == "retracted":  # already retracted: no second snapshot or version
        return
    _snapshot(conn, schema, item_kind, item_id, "retract")
    bump = sql.SQL(", version = version + 1") if item_kind == "fact" else sql.SQL("")
    conn.execute(
        sql.SQL("UPDATE {}.{} SET status = 'retracted'{} WHERE id = %s").format(
            schema, sql.Identifier(_TABLES[item_kind]), bump
        ),
        (item_id,),
    )


def set_status(
    conn: psycopg.Connection, ns: str, kind: str, item_id: str, status: str, reason: str
) -> None:
    """Set an item's status with a snapshot in ``versions``; a fact's version is bumped."""
    schema = schema_of(ns)
    if kind not in _TABLES:
        raise InvalidInput("unknown item kind")
    if status not in STATUSES:
        raise InvalidInput("unknown status")
    item_id = _uuid(item_id)
    table = sql.Identifier(_TABLES[kind])
    locked = conn.execute(
        sql.SQL("SELECT 1 FROM {}.{} WHERE id = %s FOR UPDATE").format(schema, table), (item_id,)
    ).fetchone()
    if locked is None:
        raise NotFound("item not found")
    _snapshot(conn, schema, kind, item_id, reason)
    bump = sql.SQL(", version = version + 1") if kind == "fact" else sql.SQL("")
    conn.execute(
        sql.SQL("UPDATE {}.{} SET status = %s{} WHERE id = %s").format(schema, table, bump),
        (status, item_id),
    )


def attach_fact(conn: psycopg.Connection, ns: str, fact_id: str, node_id: str) -> None:
    """Attach a waiting workspace fact to a node: snapshot, set the node, clear the wait."""
    if ns == "general":
        raise InvalidInput("general facts do not wait")
    schema = schema_of(ns)
    fact_id, node_id = _uuid(fact_id), _uuid(node_id)
    if not _exists(conn, schema, "nodes", node_id):
        raise InvalidInput("node not in namespace")
    locked = conn.execute(
        sql.SQL("SELECT 1 FROM {}.facts WHERE id = %s FOR UPDATE").format(schema), (fact_id,)
    ).fetchone()
    if locked is None:
        raise NotFound("fact not found")
    _snapshot(conn, schema, "fact", fact_id, "attach")
    conn.execute(
        sql.SQL(
            "UPDATE {}.facts SET node_id = %s, wait_concept = NULL, version = version + 1 "
            "WHERE id = %s"
        ).format(schema),
        (node_id, fact_id),
    )


def move_fact(conn: psycopg.Connection, ns: str, fact_id: str, node_id: str) -> None:
    """Move a fact to another node of the same namespace: snapshot (reason ``restore``), set the
    node, bump the version. Used when a restored fact's own node maps to a current namesake."""
    schema = schema_of(ns)
    fact_id, node_id = _uuid(fact_id), _uuid(node_id)
    if not _exists(conn, schema, "nodes", node_id):
        raise InvalidInput("node not in namespace")
    locked = conn.execute(
        sql.SQL("SELECT 1 FROM {}.facts WHERE id = %s FOR UPDATE").format(schema), (fact_id,)
    ).fetchone()
    if locked is None:
        raise NotFound("fact not found")
    _snapshot(conn, schema, "fact", fact_id, "restore")
    conn.execute(
        sql.SQL("UPDATE {}.facts SET node_id = %s, version = version + 1 WHERE id = %s").format(
            schema
        ),
        (node_id, fact_id),
    )


def move_edge(conn: psycopg.Connection, ns: str, edge_id: str, src: str, dst: str) -> None:
    """Re-point an edge to other nodes of the same namespace: snapshot (reason ``restore``), set
    the endpoints. Used when a restored edge's endpoint maps to a current namesake."""
    schema = schema_of(ns)
    edge_id, src, dst = _uuid(edge_id), _uuid(src), _uuid(dst)
    if src == dst:
        raise InvalidInput("edge would point at itself")
    if not (_exists(conn, schema, "nodes", src) and _exists(conn, schema, "nodes", dst)):
        raise InvalidInput("node not in namespace")
    locked = conn.execute(
        sql.SQL("SELECT 1 FROM {}.edges WHERE id = %s FOR UPDATE").format(schema), (edge_id,)
    ).fetchone()
    if locked is None:
        raise NotFound("edge not found")
    _snapshot(conn, schema, "edge", edge_id, "restore")
    conn.execute(
        sql.SQL("UPDATE {}.edges SET src = %s, dst = %s WHERE id = %s").format(schema),
        (src, dst, edge_id),
    )


def add_support(
    conn: psycopg.Connection, ns: str, kind: str, item_id: str, revision_id: str
) -> None:
    """Record that a revision produced (or reproduced) an item; a repeat is a no-op."""
    if ns == "general":
        raise InvalidInput("general items have no support rows")
    schema = schema_of(ns)
    if kind not in _TABLES:
        raise InvalidInput("unknown item kind")
    conn.execute(
        sql.SQL(
            "INSERT INTO {}.support (item_kind, item_id, revision_id) VALUES (%s, %s, %s) "
            "ON CONFLICT DO NOTHING"
        ).format(schema),
        (kind, _uuid(item_id), _uuid(revision_id)),
    )


def restore_fact(conn: psycopg.Connection, ns: str, fact_id: str, version: int) -> int:
    """Set a fact's text and status back to those of ``version`` (from ``versions``). The current
    row is snapshotted first (reason ``restore``), so restoring the current version works and the
    version number keeps increasing. Returns the new version; a changed text drops the vector."""
    schema = schema_of(ns)
    fact_id = _uuid(fact_id)
    row = conn.execute(
        sql.SQL("SELECT text FROM {}.facts WHERE id = %s FOR UPDATE").format(schema), (fact_id,)
    ).fetchone()
    if row is None:
        raise NotFound("fact not found")
    _snapshot(conn, schema, "fact", fact_id, "restore")
    old = conn.execute(
        sql.SQL(
            "SELECT data->>'text', data->>'status' FROM {}.versions "
            "WHERE item_kind = 'fact' AND item_id = %s AND version = %s"
        ).format(schema),
        (fact_id, version),
    ).fetchone()
    if old is None:
        raise NotFound("version not found")
    new_version = conn.execute(
        sql.SQL(
            "UPDATE {}.facts SET text = %s, status = %s, version = version + 1 "
            "WHERE id = %s RETURNING version"
        ).format(schema),
        (old[0], old[1], fact_id),
    ).fetchone()[0]
    if old[0] != row[0]:
        conn.execute(
            sql.SQL("DELETE FROM {}.embeddings WHERE item_kind = 'fact' AND item_id = %s").format(
                schema
            ),
            (fact_id,),
        )
    return new_version


def merge_nodes(conn: psycopg.Connection, ns: str, keep: str, gone: str) -> None:
    """Fold node ``gone`` into ``keep`` (workspace namespaces only). Facts and edges move over,
    ``gone``'s name and aliases become aliases of ``keep``, ``gone`` is marked superseded and a
    ``replaced_by`` edge points to ``keep``. Everything touched is snapshotted (reason ``merge``);
    ``keep``'s kind is kept."""
    if ns == "general":
        raise InvalidInput("unknown namespace")
    schema = schema_of(ns)
    keep, gone = _uuid(keep), _uuid(gone)
    if keep == gone:
        raise InvalidInput("same node")
    nodes = {
        str(r[0]): r
        for r in conn.execute(
            sql.SQL(
                "SELECT id, name, aliases, status FROM {}.nodes WHERE id IN (%s, %s) ORDER BY id FOR UPDATE"
            ).format(schema),
            (keep, gone),
        )
    }
    if len(nodes) != 2:
        raise NotFound("node not found")
    if nodes[keep][3] != "current" or nodes[gone][3] != "current":
        raise InvalidInput("node not current")
    for node_id in (keep, gone):
        _snapshot(conn, schema, "node", node_id, "merge")
    for (fact_id,) in conn.execute(
        sql.SQL("SELECT id FROM {}.facts WHERE node_id = %s ORDER BY id FOR UPDATE").format(schema),
        (gone,),
    ).fetchall():
        _snapshot(conn, schema, "fact", str(fact_id), "merge")
        conn.execute(
            sql.SQL("UPDATE {}.facts SET node_id = %s, version = version + 1 WHERE id = %s").format(
                schema
            ),
            (keep, fact_id),
        )
    for edge_id, src, dst in conn.execute(
        sql.SQL(
            "SELECT id, src, dst FROM {}.edges WHERE src = %s OR dst = %s ORDER BY id FOR UPDATE"
        ).format(schema),
        (gone, gone),
    ).fetchall():
        _snapshot(conn, schema, "edge", str(edge_id), "merge")
        new_src = keep if str(src) == gone else str(src)
        new_dst = keep if str(dst) == gone else str(dst)
        loop = new_src == new_dst
        conn.execute(
            sql.SQL(
                "UPDATE {}.edges SET src = %s, dst = %s, status = CASE WHEN %s THEN 'retracted' "
                "ELSE status END WHERE id = %s"
            ).format(schema),
            (new_src, new_dst, loop, edge_id),
        )
    keep_name, keep_aliases = nodes[keep][1], nodes[keep][2]
    seen = {keep_name.lower()}
    merged: list[str] = []
    for alias in [*keep_aliases, nodes[gone][1], *nodes[gone][2]]:
        if alias.lower() not in seen:
            seen.add(alias.lower())
            merged.append(alias)
    conn.execute(
        sql.SQL("UPDATE {}.nodes SET aliases = %s WHERE id = %s").format(schema), (merged, keep)
    )
    conn.execute(
        sql.SQL("UPDATE {}.nodes SET status = 'superseded' WHERE id = %s").format(schema), (gone,)
    )
    add_edge(conn, ns, gone, keep, "replaced_by")
