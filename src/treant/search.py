"""Hybrid search: cosine over workspace and general vectors, plus a one-hop graph expansion.

Everything runs in one ``ws_tx`` so the database role is what bounds the data; the final
tripwire re-checks every hit's source schema anyway. Each hit is carried with the schema it came
from until that check. Logs carry counts only.
"""

import logging
from dataclasses import dataclass
from typing import Any, Literal

import psycopg
from psycopg import sql

from treant import names, provenance
from treant.db import Database
from treant.embed import Embedder, EmbedderUnavailable
from treant.tokens import TokenContext
from treant.walk import NodeRef, walk

log = logging.getLogger(__name__)

MAX_TEXT = 1200
MAX_LIMIT = 20
# Facts reached by the graph walk score floor * (1 + WALK_SIM_WEIGHT * sim), where floor is
# WALK_FACTOR times the weakest direct hit and sim is the walk hit's own cosine similarity to
# the query clamped to [0, 1]. The band [floor, 0.75 * weakest] stays strictly below every
# direct hit when the weakest scores above zero; otherwise the band is
# [weakest - WALK_FACTOR, weakest - WALK_FACTOR * (1 - WALK_SIM_WEIGHT)], also below it.
# A walk hit with no vector yet scores the bottom of the band.
WALK_FACTOR = 0.5
WALK_SIM_WEIGHT = 0.5
KEYWORD_SCORE = 0.5

Hit = dict[str, Any]


@dataclass(frozen=True)
class SearchResult:
    mode: Literal["hybrid", "keyword"]
    hits: list[Hit]


@dataclass
class _Raw:
    schema: str
    hit: Hit
    node_id: str | None  # node the hit hangs off (fact hits) or is (node hits)
    walked: bool = False  # reached by graph expansion, not by similarity


def _literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(x) for x in vec) + "]"


def _schemas(workspace: str) -> list[tuple[str, str]]:
    return [(names.ws_schema(workspace), "workspace"), ("general", "general")]


def _mk(schema: str, ns: str, row: tuple, score: float) -> _Raw:
    kind, item_id, typ, text, node_name, node_id = row
    hit = {
        "namespace": ns,
        "kind": kind,
        "id": str(item_id),
        "type": typ,
        "text": text[:MAX_TEXT],
        "node": node_name,
        "score": score,
    }
    return _Raw(schema, hit, None if node_id is None else str(node_id))


_FACTS_NODES = (
    "SELECT 'fact' AS kind, f.id, f.type, f.text, n.name, f.node_id, e.vec <=> %(vec)s::vector AS d "
    "FROM {s}.embeddings e JOIN {s}.facts f ON e.item_kind = 'fact' AND e.item_id = f.id "
    "LEFT JOIN {s}.nodes n ON n.id = f.node_id WHERE f.status = 'current' "
    "UNION ALL "
    "SELECT 'node', n.id, n.type, n.name, n.name, n.id, e.vec <=> %(vec)s::vector "
    "FROM {s}.embeddings e JOIN {s}.nodes n ON e.item_kind = 'node' AND e.item_id = n.id "
    "WHERE n.status = 'current'"
)
# ``general`` has no journal; journal entries live in workspace schemas only.
_JOURNAL = (
    " UNION ALL SELECT 'journal', j.id, 'journal', j.text, NULL, NULL, e.vec <=> %(vec)s::vector "
    "FROM {s}.embeddings e JOIN {s}.journal_entries j "
    "ON e.item_kind = 'journal' AND e.item_id = j.id "
    "JOIN {s}.revisions r ON r.id = j.revision_id AND r.status = 'stored'"
)


def _vector_hits(conn: psycopg.Connection, schema: str, ns: str, vec: str, k: int) -> list[_Raw]:
    inner = _FACTS_NODES + (_JOURNAL if schema != "general" else "")
    query = sql.SQL("SELECT * FROM (" + inner + ") q ORDER BY d, id LIMIT %(k)s").format(
        s=sql.Identifier(schema)
    )
    rows = conn.execute(query, {"vec": vec, "k": k}).fetchall()
    return [_mk(schema, ns, row[:6], 1.0 - float(row[6])) for row in rows]


def _escape(query: str) -> str:
    return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _keyword_hits(
    conn: psycopg.Connection, schema: str, ns: str, pattern: str, k: int
) -> list[_Raw]:
    inner = (
        "SELECT 'fact' AS kind, f.id, f.type, f.text, n.name, f.node_id "
        "FROM {s}.facts f LEFT JOIN {s}.nodes n ON n.id = f.node_id "
        "WHERE f.status = 'current' AND f.text ILIKE %(p)s ESCAPE '\\' "
        "UNION ALL "
        "SELECT 'node', n.id, n.type, n.name, n.name, n.id FROM {s}.nodes n "
        "WHERE n.status = 'current' AND (n.name ILIKE %(p)s ESCAPE '\\' "
        "OR EXISTS (SELECT 1 FROM unnest(n.aliases) a WHERE a ILIKE %(p)s ESCAPE '\\'))"
    )
    if schema != "general":
        inner += (
            " UNION ALL SELECT 'journal', j.id, 'journal', j.text, NULL, NULL "
            "FROM {s}.journal_entries j "
            "JOIN {s}.revisions r ON r.id = j.revision_id AND r.status = 'stored' "
            "WHERE j.text ILIKE %(p)s ESCAPE '\\'"
        )
    query = sql.SQL("SELECT * FROM (" + inner + ") q ORDER BY id LIMIT %(k)s").format(
        s=sql.Identifier(schema)
    )
    rows = conn.execute(query, {"p": pattern, "k": k}).fetchall()
    return [_mk(schema, ns, row, KEYWORD_SCORE) for row in rows]


def _walk_score(weakest: float, distance: float | None) -> float:
    sim = 0.0 if distance is None else min(max(1.0 - float(distance), 0.0), 1.0)
    if weakest > 0:
        return WALK_FACTOR * weakest * (1.0 + WALK_SIM_WEIGHT * sim)
    return weakest - WALK_FACTOR * (1.0 - WALK_SIM_WEIGHT * sim)


def _expand(
    conn: psycopg.Connection, workspace: str, top: list[_Raw], vec: str, weakest: float
) -> list[_Raw]:
    ws = names.ws_schema(workspace)
    starts: list[NodeRef] = []
    for raw in top:
        if raw.node_id is not None:
            starts.append(("ws" if raw.schema == ws else "general", raw.node_id))
    if not starts:
        return []
    out: list[_Raw] = []
    reached = walk(conn, workspace, starts, hops=1)
    for space, schema, ns in (("ws", ws, "workspace"), ("general", "general", "general")):
        ids = [i for s, i in reached if s == space]
        if not ids:
            continue
        rows = conn.execute(
            sql.SQL(
                "SELECT 'fact', f.id, f.type, f.text, n.name, f.node_id, "
                "e.vec <=> %(vec)s::vector FROM {s}.facts f "
                "JOIN {s}.nodes n ON n.id = f.node_id "
                "LEFT JOIN {s}.embeddings e ON e.item_kind = 'fact' AND e.item_id = f.id "
                "WHERE f.status = 'current' AND f.node_id = ANY(%(ids)s::uuid[])"
            ).format(s=sql.Identifier(schema)),
            {"vec": vec, "ids": ids},
        ).fetchall()
        for row in rows:
            raw = _mk(schema, ns, row[:6], _walk_score(weakest, row[6]))
            raw.walked = True
            out.append(raw)
    return out


def _search_tx(
    conn: psycopg.Connection, ctx: TokenContext, query: str, limit: int, embedder: Embedder
) -> tuple[Literal["hybrid", "keyword"], list[_Raw]]:
    schemas = _schemas(ctx.workspace)
    try:
        vec = _literal(embedder.embed_query(query))
    except EmbedderUnavailable:
        pattern = f"%{_escape(query)}%"
        raws = [r for s, ns in schemas for r in _keyword_hits(conn, s, ns, pattern, limit * 2)]
        raws.sort(key=lambda r: r.hit["id"])
        return "keyword", raws[: limit * 2]
    direct = [r for s, ns in schemas for r in _vector_hits(conn, s, ns, vec, limit * 2)]
    direct.sort(key=lambda r: (-r.hit["score"], r.hit["id"]))
    direct = direct[: limit * 2]
    if direct:
        weakest = min(r.hit["score"] for r in direct)
        direct += _expand(conn, ctx.workspace, direct, vec, weakest)
    return "hybrid", direct


def search(
    db: Database, embedder: Embedder, ctx: TokenContext, query: str, limit: int = 8
) -> SearchResult:
    limit = max(1, min(int(limit), MAX_LIMIT))
    with db.ws_tx(ctx.workspace) as conn:
        mode, raws = _search_tx(conn, ctx, query, limit, embedder)
        found = provenance.lookup(
            conn,
            names.ws_schema(ctx.workspace),
            [(r.schema, r.hit["kind"], r.hit["id"]) for r in raws],
        )
        for r in raws:
            if (r.schema, r.hit["kind"], r.hit["id"]) in found:
                r.hit["source"] = found[(r.schema, r.hit["kind"], r.hit["id"])]
    allowed = {names.ws_schema(ctx.workspace), "general"}
    clean = [r for r in raws if r.schema in allowed]
    faults = len(raws) - len(clean)
    if faults:
        # The workspace role has no rights on control, so this is a separate transaction.
        with db.control_tx() as conn:
            conn.execute(
                "INSERT INTO control.metrics (name, workspace, value) VALUES (%s, %s, %s)",
                ("isolation_fault", ctx.workspace, faults),
            )
        log.error("isolation tripwire fired hits=%d", faults)
    best: dict[tuple[str, str, str], _Raw] = {}
    for r in clean:
        key = (r.hit["namespace"], r.hit["kind"], r.hit["id"])
        if key not in best or r.hit["score"] > best[key].hit["score"]:
            best[key] = r
    order = lambda r: (-r.hit["score"], r.hit["id"])
    direct = sorted((r for r in best.values() if not r.walked), key=order)
    walked = sorted((r for r in best.values() if r.walked), key=order)
    # Walk hits always score below direct ones, so without reserved slots a full set of direct
    # hits would crowd them out. A quarter of the limit is kept for them (none when limit < 4);
    # slots the direct hits leave empty go to further walk hits.
    reserve = min(len(walked), limit // 4)
    take = min(len(direct), limit - reserve)
    chosen = direct[:take] + walked[: limit - take]
    hits = [r.hit for r in sorted(chosen, key=order)]
    log.info("search mode=%s hits=%d", mode, len(hits))
    return SearchResult(mode, hits)
