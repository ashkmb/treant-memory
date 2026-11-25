"""Origin-bound graph walk.

The walk starts in one workspace and may touch only that workspace's schema and
``general``. From a general node it goes down only into the origin workspace's
nodes that refine it, never into another workspace: no other ``ws_*`` schema is
queried at all.
"""

import uuid
from typing import Literal

import psycopg
from psycopg import sql

from treant import names
from treant.errors import InvalidInput

NodeRef = tuple[Literal["ws", "general"], str]
MAX_HOPS = 2


def _edge_neighbours(conn: psycopg.Connection, schema: str, ids: list[str]) -> list[str]:
    query = sql.SQL(
        "SELECT DISTINCT n.id::text FROM {s}.edges e "
        "JOIN {s}.nodes n ON n.id = CASE WHEN e.src = ANY(%(ids)s::uuid[]) THEN e.dst ELSE e.src END "
        "WHERE e.status = 'current' AND n.status = 'current' "
        "AND (e.src = ANY(%(ids)s::uuid[]) OR e.dst = ANY(%(ids)s::uuid[])) "
        "ORDER BY 1"
    ).format(s=sql.Identifier(schema))
    return [r[0] for r in conn.execute(query, {"ids": ids}).fetchall()]


def _column(conn: psycopg.Connection, query: sql.Composable, ids: list[str]) -> list[str]:
    return [r[0] for r in conn.execute(query, (ids,)).fetchall()]


def walk(
    conn: psycopg.Connection, workspace: str, start: list[NodeRef], hops: int = 2
) -> list[NodeRef]:
    try:
        ws = names.ws_schema(workspace)
    except ValueError:
        raise InvalidInput("invalid workspace") from None
    hops = max(0, min(int(hops), MAX_HOPS))
    seen: dict[NodeRef, None] = {}
    frontier: list[NodeRef] = []
    for space, node_id in start:
        if space not in ("ws", "general"):
            raise InvalidInput("invalid node reference")
        try:
            ref: NodeRef = (space, str(uuid.UUID(str(node_id))))
        except ValueError:
            raise InvalidInput("invalid node reference") from None
        if ref not in seen:
            seen[ref] = None
            frontier.append(ref)

    # Trust nothing in the caller's refs: keep only current nodes that really live in the
    # namespace their tag claims (origin workspace or general), in input order.
    live: set[NodeRef] = set()
    for space, query in (
        (
            "ws",
            sql.SQL(
                "SELECT id::text FROM {}.nodes WHERE id = ANY(%s::uuid[]) AND status = 'current'"
            ).format(sql.Identifier(ws)),
        ),
        (
            "general",
            sql.SQL(
                "SELECT id::text FROM general.nodes WHERE id = ANY(%s::uuid[]) AND status = 'current'"
            ),
        ),
    ):
        ids = [i for s, i in frontier if s == space]
        if ids:
            live.update((space, i) for i in _column(conn, query, ids))
    frontier = [ref for ref in frontier if ref in live]
    seen = dict.fromkeys(frontier)

    for _ in range(hops):
        ws_ids = [i for s, i in frontier if s == "ws"]
        gen_ids = [i for s, i in frontier if s == "general"]
        found: list[NodeRef] = []
        if ws_ids:
            found += [("ws", i) for i in _edge_neighbours(conn, ws, ws_ids)]
            up = sql.SQL(
                "SELECT DISTINCT g.id::text FROM {ws}.nodes n JOIN general.nodes g ON g.id = n.refines "
                "WHERE n.id = ANY(%s::uuid[]) AND g.status = 'current' ORDER BY 1"
            ).format(ws=sql.Identifier(ws))
            found += [("general", i) for i in _column(conn, up, ws_ids)]
        if gen_ids:
            found += [("general", i) for i in _edge_neighbours(conn, "general", gen_ids)]
            down = sql.SQL(
                "SELECT n.id::text FROM {ws}.nodes n "
                "WHERE n.refines = ANY(%s::uuid[]) AND n.status = 'current' ORDER BY 1"
            ).format(ws=sql.Identifier(ws))
            found += [("ws", i) for i in _column(conn, down, gen_ids)]
        frontier = []
        for ref in found:
            if ref not in seen:
                seen[ref] = None
                frontier.append(ref)
        if not frontier:
            break
    return list(seen)
