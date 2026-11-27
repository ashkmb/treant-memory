"""Embed worker: turns journal entries, facts and nodes into vectors.

Texts are read in one transaction, embedded with no transaction open (the embedder is a slow
network call), then written in a second transaction. Vectors are passed to Postgres as the text
literal ``'[...]'::vector``, so no per-connection type registration is needed on pooled
connections. Logs carry counts and exception type names only.

Because the embed call runs with no transaction open, a fact can be edited between the read
and the write. Fact items therefore carry the version they were read at, and the write stores
the vector only if the fact is still current at that version; the edit's own job embeds the
new text. Nodes have no text edit path in plan 1 (only status changes), so they carry no
version; a node is written only if still current, a journal entry only if its revision is
still stored.
"""

import logging

import psycopg
from psycopg import sql

from treant import jobs, names
from treant.db import Database
from treant.embed import Embedder, EmbedderUnavailable

log = logging.getLogger(__name__)

BATCH = 32
SWEEP_LIMIT = 256  # per kind and per sweep; a full sweep queues a follow-up job

Item = tuple[str, str, str, int | None]  # (item_kind, item_id, text, fact version or None)


def _literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(x) for x in vec) + "]"


def _pending(
    conn: psycopg.Connection, schema: sql.Identifier, journals: bool = False
) -> list[Item]:
    return _sweep(conn, schema, journals)[0]


def _sweep(
    conn: psycopg.Connection, schema: sql.Identifier, journals: bool = False
) -> tuple[list[Item], bool]:
    """Current facts and nodes of one schema that have no vector yet, and whether any kind
    hit ``SWEEP_LIMIT`` (so more may be waiting).

    With ``journals`` (workspace schemas only; ``general`` has no journal) it also sweeps
    journal entries of stored revisions that lack a vector, so an entry whose own job died
    during a long embedder outage is picked up by any later job.
    """
    items: list[Item] = []
    full = False
    if journals:
        rows = conn.execute(
            sql.SQL(
                "SELECT j.id, j.text FROM {s}.journal_entries j "
                "JOIN {s}.revisions r ON r.id = j.revision_id WHERE r.status = 'stored' "
                "AND NOT EXISTS (SELECT 1 FROM {s}.embeddings e "
                "WHERE e.item_kind = 'journal' AND e.item_id = j.id) "
                "ORDER BY j.created_at, j.id LIMIT {lim}"
            ).format(s=schema, lim=sql.Literal(SWEEP_LIMIT))
        ).fetchall()
        full = full or len(rows) >= SWEEP_LIMIT
        items += [("journal", str(i), t, None) for i, t in rows]
    rows = conn.execute(
        sql.SQL(
            "SELECT f.id, f.text, f.version FROM {s}.facts f WHERE f.status = 'current' "
            "AND NOT EXISTS ("
            "SELECT 1 FROM {s}.embeddings e WHERE e.item_kind = 'fact' AND e.item_id = f.id) "
            "ORDER BY f.created_at, f.id LIMIT {lim}"
        ).format(s=schema, lim=sql.Literal(SWEEP_LIMIT))
    ).fetchall()
    full = full or len(rows) >= SWEEP_LIMIT
    items += [("fact", str(i), t, v) for i, t, v in rows]
    rows = conn.execute(
        sql.SQL(
            "SELECT n.id, n.name, n.aliases, n.type FROM {s}.nodes n WHERE n.status = 'current' "
            "AND NOT EXISTS (SELECT 1 FROM {s}.embeddings e "
            "WHERE e.item_kind = 'node' AND e.item_id = n.id) "
            "ORDER BY n.created_at, n.id LIMIT {lim}"
        ).format(s=schema, lim=sql.Literal(SWEEP_LIMIT))
    ).fetchall()
    full = full or len(rows) >= SWEEP_LIMIT
    items += [
        ("node", str(i), " ".join([name, *aliases, typ]), None) for i, name, aliases, typ in rows
    ]
    return items, full


def _embed_all(
    embedder: Embedder, items: list[Item], vectors: list[list[float]] | None = None
) -> list[list[float]]:
    """Embed in batches; with ``vectors`` given, each finished batch is appended there, so a
    caller keeps what was made before a later batch failed."""
    vectors = [] if vectors is None else vectors
    for i in range(0, len(items), BATCH):
        vectors.extend(embedder.embed_documents([t for _, _, t, _ in items[i : i + BATCH]]))
    return vectors


def _upsert(
    conn: psycopg.Connection, schema: sql.Identifier, items: list[Item], vectors: list[list[float]]
) -> None:
    # Each vector is stored only if its item may still have one: a fact still current at the
    # version that was read, a node still current, a journal entry whose revision is still
    # stored. Every status change locks the row (FOR UPDATE) before changing it and deletes the
    # vector after, so FOR SHARE here orders the two: either the change waits for this commit
    # and then deletes the vector, or this insert waits for the change and then finds the
    # item no longer qualifies. A fact edit likewise cannot leave a vector of the old text.
    fact_query = sql.SQL(
        "INSERT INTO {s}.embeddings (item_kind, item_id, vec) "
        "SELECT 'fact', f.id, %s::vector FROM {s}.facts f "
        "WHERE f.id = %s AND f.version = %s AND f.status = 'current' FOR SHARE "
        "ON CONFLICT (item_kind, item_id) DO UPDATE SET vec = excluded.vec"
    ).format(s=schema)
    node_query = sql.SQL(
        "INSERT INTO {s}.embeddings (item_kind, item_id, vec) "
        "SELECT 'node', n.id, %s::vector FROM {s}.nodes n "
        "WHERE n.id = %s AND n.status = 'current' FOR SHARE "
        "ON CONFLICT (item_kind, item_id) DO UPDATE SET vec = excluded.vec"
    ).format(s=schema)
    journal_query = sql.SQL(
        "INSERT INTO {s}.embeddings (item_kind, item_id, vec) "
        "SELECT 'journal', j.id, %s::vector FROM {s}.journal_entries j "
        "JOIN {s}.revisions r ON r.id = j.revision_id "
        "WHERE j.id = %s AND r.status = 'stored' FOR SHARE OF j, r "
        "ON CONFLICT (item_kind, item_id) DO UPDATE SET vec = excluded.vec"
    ).format(s=schema)
    for (kind, item_id, _, version), vec in zip(items, vectors, strict=True):
        if kind == "fact":
            conn.execute(fact_query, (_literal(vec), item_id, version))
        elif kind == "node":
            conn.execute(node_query, (_literal(vec), item_id))
        elif kind == "journal":
            conn.execute(journal_query, (_literal(vec), item_id))
        else:
            raise ValueError("unknown item kind")


def _process(
    db: Database, embedder: Embedder, workspace: str, revision_id: str | None
) -> tuple[int, bool]:
    schema = sql.Identifier(names.ws_schema(workspace))
    with db.ws_tx(workspace) as conn:
        items: list[Item] = []
        row = conn.execute(
            sql.SQL("SELECT id, text FROM {s}.journal_entries WHERE revision_id = %s").format(
                s=schema
            ),
            (revision_id,),
        ).fetchone()
        own = str(row[0]) if row else None
        if row:
            items.append(("journal", own, row[1], None))
        pending, full = _sweep(conn, schema, journals=True)
        items += [i for i in pending if i[1] != own]
    vectors: list[list[float]] = []
    try:
        _embed_all(embedder, items, vectors)
    except EmbedderUnavailable:
        if vectors:  # keep the finished batches; the retry sweeps only the rest
            with db.ws_tx(workspace) as conn:
                _upsert(conn, schema, items[: len(vectors)], vectors)
        raise
    with db.ws_tx(workspace) as conn:
        _upsert(conn, schema, items, vectors)
    return len(items), full


def run_once(db: Database, embedder: Embedder) -> bool:
    """Process one embed job; True if a job was claimed (even if it failed), False when idle."""
    job = jobs.claim(db, {"embed"})
    if job is None:
        return False
    try:
        n, full = _process(db, embedder, job.workspace, job.payload["revision_id"])
        if full:  # more unembedded items may remain: continue in a fresh job
            with db.control_tx() as conn:
                jobs.enqueue(
                    conn,
                    job.workspace,
                    "embed",
                    f"sweep:{job.workspace}:{job.id}",
                    {"revision_id": None},
                )
    except EmbedderUnavailable:
        log.warning("embed job failed category=embedder")
        jobs.fail(db, job, "embedder")
    except Exception as e:  # noqa: BLE001 - recorded by category, never by message
        log.error("embed job failed category=internal error=%s", type(e).__name__)
        jobs.fail(db, job, "internal")
    else:
        if jobs.complete(db, job):
            log.info("embed job done items=%d", n)
        else:
            log.warning("embed job lease lost items=%d", n)
    return True


def embed_general(db: Database, embedder: Embedder) -> int:
    """Embed current general facts and nodes lacking a vector, as the publisher role."""
    schema = sql.Identifier("general")
    with db.publisher_tx() as conn:
        items = _pending(conn, schema)  # bounded; the next call takes the rest
    if not items:
        return 0
    vectors = _embed_all(embedder, items)
    with db.publisher_tx() as conn:
        _upsert(conn, schema, items, vectors)
    log.info("embed general items=%d", len(items))
    return len(items)
