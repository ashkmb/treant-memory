"""Apply stage: turn a stored generation into graph items, reconcile, queue the general half.

``run`` works in two transactions:

1. One workspace transaction. It locks the session row first (``save_session`` takes the same
   lock), then resolves nodes, edges and facts and writes support rows. Saves are additive:
   every stored revision is applied, and what an older revision produced stays. The one
   exception is open threads: when the revision is the newest stored one of its session, the
   open threads only older revisions support and this one does not reproduce are superseded.
   A revision that is not the newest (distilled late, backfilled, or re-run when its concept
   is published) only adds: it changes no existing text, opens no thread and supersedes
   nothing. No revision re-creates a fact it produced before that is no longer current.
   It then enqueues an ``embed`` sweep.
2. One publisher transaction after that commit (workspace roles cannot touch the queue). It
   queues the general half with a ``gate`` job per new row. Rows of other revisions are left
   alone; a fact is not queued when its text is already published or waiting, or is a
   rephrasing (ratio >= 0.8) of a fact this session queued before. Every write is keyed, so
   a crash between the two transactions is repaired by the job's re-run.

Queue items carry ``source_text`` (concept: name, fact: text), the text the model generated. It
and ``item_key`` never change after insert; gate rewrites change ``item.text`` only. Every
"already published / queued" check compares against the ``source_text`` of queue rows, never
against the published (possibly rewritten) general text.

Rulings: entity resolution is an exact match of normalised name/alias of the same kind, else a
new node, with a ``merge_flags`` row for names of the same type at ratio >= 0.85. Fact identity
is same type and node and ratio >= 0.8 of the normalised text. Text is normalised in Python
only (``reconcile.normalise``), over candidate rows fetched by SQL. Nothing in ``general`` is
superseded here. ``control.general_session_head`` is no longer used.

Logs carry counts, stage names and ids only.
"""

import hashlib
import logging
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from treant import graph, jobs, names, reconcile
from treant.db import Database
from treant.reconcile import FACT_RATIO, MERGE_RATIO, normalise, similarity

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 12


def trusted(gen: dict[str, Any]) -> bool:
    """False when the model dropped more items than it kept (``stats``; older generations
    without stats count their items and ``dropped``). Reconcile only trusts a generation that
    is mostly valid: a botched reply must not supersede the open threads of older revisions."""
    stats = gen.get("stats")
    if isinstance(stats, dict):
        kept, dropped = stats.get("kept", 0), stats.get("dropped", 0)
    else:
        general = gen.get("general") or {}
        kept = sum(len(gen.get(k) or []) for k in ("nodes", "edges", "facts")) + sum(
            len(general.get(k) or []) for k in ("concepts", "facts")
        )
        dropped = gen.get("dropped", 0)
    return not dropped > kept


def item_key(kind: str, text: str) -> str:
    return f"{kind}:" + hashlib.sha256(normalise(text).encode()).hexdigest()


def _named(rows: list[tuple], keys: list[str]) -> str | None:
    """First id of (id, name, aliases) rows whose name, else an alias, normalises to a key."""
    wanted = {normalise(k) for k in keys}
    for row in rows:
        if normalise(row[1]) in wanted:
            return row[0]
    for row in rows:
        if any(normalise(a) in wanted for a in row[2]):
            return row[0]
    return None


def _concept_keys(conn: psycopg.Connection, general_id: str, name: str) -> set[str]:
    """Normalised name and aliases of a general concept, plus the name the model used."""
    row = conn.execute(
        "SELECT name, aliases FROM general.nodes WHERE id = %s", (general_id,)
    ).fetchone()
    keys = {normalise(name)}
    if row is not None:
        keys |= {normalise(row[0]), *(normalise(a) for a in row[1])}
    return keys


def general_concept(conn: psycopg.Connection, keys: list[str]) -> str | None:
    """Id of a current general concept whose name or an alias equals one of keys (normalised)."""
    rows = conn.execute(
        "SELECT id::text, name, aliases FROM general.nodes "
        "WHERE kind = 'concept' AND status = 'current' ORDER BY created_at, id"
    ).fetchall()
    return _named(rows, keys)


def resolve_node(
    conn: psycopg.Connection,
    ns: str,
    kind: str,
    type: str,
    name: str,
    aliases: list[str] | tuple[str, ...] = (),
    refines: str | None = None,
) -> tuple[str, bool]:
    """(id, created): an exact normalised name/alias match of the same kind, else a new node.
    A new workspace node with a near-identical name of the same type gets a merge flag."""
    schema = graph.schema_of(ns)
    rows = conn.execute(
        sql.SQL(
            "SELECT id::text, name, aliases FROM {s}.nodes WHERE kind = %s AND status = 'current' "
            "ORDER BY created_at, id"
        ).format(s=schema),
        (kind,),
    ).fetchall()
    found = _named(rows, [name, *aliases])
    if found:
        return found, False
    node_id = graph.add_node(conn, ns, kind, type, name, list(aliases), refines=refines)
    if ns != "general":
        _flag_near(conn, schema, node_id, type, name)
    return node_id, True


def _flag_near(
    conn: psycopg.Connection, schema: sql.Identifier, node_id: str, type_: str, name: str
) -> None:
    rows = conn.execute(
        sql.SQL(
            "SELECT id::text, name FROM {s}.nodes WHERE type = %s AND status = 'current' "
            "AND id <> %s"
        ).format(s=schema),
        (type_, node_id),
    ).fetchall()
    target = normalise(name)
    for other, other_name in rows:
        score = similarity(target, normalise(other_name), MERGE_RATIO)
        if score >= MERGE_RATIO:
            conn.execute(
                sql.SQL(
                    "INSERT INTO {s}.merge_flags (node_a, node_b, score) VALUES (%s, %s, %s) "
                    "ON CONFLICT DO NOTHING"
                ).format(s=schema),
                (other, node_id, score),
            )


def _edge(conn: psycopg.Connection, ws: str, src: str, dst: str, type_: str) -> str:
    row = conn.execute(
        sql.SQL(
            "SELECT id::text FROM {s}.edges WHERE src = %s AND dst = %s AND type = %s "
            "AND status = 'current' ORDER BY created_at, id LIMIT 1"
        ).format(s=graph.schema_of(ws)),
        (src, dst, type_),
    ).fetchone()
    return row[0] if row else graph.add_edge(conn, ws, src, dst, type_)


def _match_fact(
    conn: psycopg.Connection,
    ws: str,
    fact: dict[str, Any],
    node_id: str | None,
    wait_keys: set[str] | None,
    produced: set[str],
    old: str | None,
) -> tuple[str, str, str | None] | None:
    """(id, text, node_id) of the existing fact this one is, per the fact-identity ruling.

    A fact on a local concept also matches a same-identity fact still waiting for that concept
    (``wait_keys``: the normalised names that concept goes by, its general name and aliases
    included), so a re-run attaches it instead of adding one.
    Facts this run already produced match only on identical normalised text (two identical
    facts are one fact; near-identical ones stay apart). ``old`` (the supersedes target) never
    matches.
    """
    if node_id is not None:
        where = "(node_id = %(node)s OR (node_id IS NULL AND wait_concept IS NOT NULL))"
    else:
        where = "node_id IS NULL"
    rows = conn.execute(
        sql.SQL(
            "SELECT id::text, text, node_id::text, wait_concept FROM {s}.facts "
            "WHERE type = %(type)s AND status = 'current' AND " + where + " "
            "ORDER BY created_at, id"
        ).format(s=graph.schema_of(ws)),
        {"type": fact["type"], "node": node_id},
    ).fetchall()
    target = normalise(fact["text"])
    best, best_score = None, 0.0
    for fid, text, fnode, wait in rows:
        if fnode is None:
            if wait is None:
                if wait_keys is not None:
                    continue
            elif wait_keys is None or normalise(wait) not in wait_keys:
                continue
        if fid == old:
            continue
        norm = normalise(text)
        if norm == target:
            score = 1.0
        elif fid in produced:
            continue
        else:
            score = similarity(target, norm, FACT_RATIO)
        if score >= FACT_RATIO and score > best_score:
            best, best_score = (fid, text, fnode), score
    return best


def _own_gone(conn: psycopg.Connection, ws: str, revision_id: str, fact: dict[str, Any]) -> bool:
    """True when this revision already produced this fact and it is no longer current (the owner
    retracted or closed it, or something superseded it). A re-run of the revision must not bring
    it back as a new fact."""
    rows = conn.execute(
        sql.SQL(
            "SELECT f.text FROM {s}.support s JOIN {s}.facts f ON f.id = s.item_id "
            "WHERE s.item_kind = 'fact' AND s.revision_id = %s AND f.type = %s "
            "AND f.status <> 'current'"
        ).format(s=graph.schema_of(ws)),
        (revision_id, fact["type"]),
    ).fetchall()
    target = normalise(fact["text"])
    return any(
        normalise(text) == target or similarity(target, normalise(text), FACT_RATIO) >= FACT_RATIO
        for (text,) in rows
    )


def _put_fact(
    conn: psycopg.Connection,
    ws: str,
    revision_id: str,
    fact: dict[str, Any],
    node_id: str | None,
    concept: str | None,
    wait_keys: set[str] | None,
    produced: set[str],
    old: str | None,
    newest: bool = True,
) -> str | None:
    """Id of the fact this one is or became; None when nothing is written. A revision that is
    not the newest of its session (applied late, or re-run) only adds: it never changes the
    text of an existing fact and never opens a thread, since a newer save already said what is
    open. No revision re-creates a fact it produced before that is no longer current."""
    match = _match_fact(conn, ws, fact, node_id, wait_keys, produced, old)
    if match is None:
        if _own_gone(conn, ws, revision_id, fact):
            return None
        if not newest and fact["type"] == "open_thread":
            return None
        wait = concept if node_id is None else None
        return graph.add_fact(
            conn, ws, fact["type"], fact["text"], node_id, revision_id, wait_concept=wait
        )
    fid, text, current_node = match
    if node_id is not None and current_node is None:
        graph.attach_fact(conn, ws, fid, node_id)
    if newest and normalise(text) != normalise(fact["text"]):
        graph.update_fact(conn, ws, fid, text=fact["text"])
    return fid


def _apply_workspace(
    conn: psycopg.Connection,
    ws: str,
    revision_id: str,
    session_id: str,
    older: list[str],
    gen: dict[str, Any],
    reconcile_ok: bool = True,
    newest: bool = True,
) -> tuple[dict[str, set[str]], list[str], int]:
    """Write the workspace half: (produced ids by kind, names of the missing general concepts
    waiting local concepts need, count of open threads reconcile superseded). Without
    ``reconcile_ok`` (an untrusted generation, or not the session's newest revision) no open
    thread of older revisions is superseded. Without ``newest`` the revision only adds: no
    text change, no new open thread, no supersede by reference."""
    produced: dict[str, set[str]] = {k: set() for k in reconcile.KINDS}
    refs: dict[str, str] = {}
    waiting: dict[str, str] = {}  # ref of a local concept whose general concept is missing
    concept_of: dict[str, str] = {}  # ref of a created local concept -> its concept's name
    concept_keys: dict[str, set[str]] = {}  # ref of a created local concept -> names it goes by
    for node in gen.get("nodes") or []:
        ref, aliases = node["ref"], node.get("aliases") or []
        refines = None
        if node["kind"] == "local_concept":
            refines = general_concept(conn, [node["concept"]])
            if refines is None:
                waiting[ref] = node["concept"]
                continue
            concept_of[ref] = node["concept"]
            concept_keys[ref] = _concept_keys(conn, refines, node["concept"])
        node_id, _ = resolve_node(
            conn, ws, node["kind"], node["type"], node["name"], aliases, refines=refines
        )
        refs[ref] = node_id
        produced["node"].add(node_id)

    for edge in gen.get("edges") or []:
        src, dst = refs.get(edge["src"]), refs.get(edge["dst"])
        if src and dst and src != dst:  # two refs that resolved to one node make no edge
            produced["edge"].add(_edge(conn, ws, src, dst, edge["type"]))

    fact_refs = gen.get("fact_refs") or {}
    pairs: list[tuple[str, str]] = []
    for fact in gen.get("facts") or []:
        ref = fact.get("node")
        node_id = refs.get(ref) if ref else None
        concept = (concept_of.get(ref) or waiting.get(ref)) if ref else None
        keys = concept_keys.get(ref) if ref else None
        if keys is None and concept is not None:
            keys = {normalise(concept)}
        old = fact_refs.get(fact["supersedes"]) if fact.get("supersedes") else None
        fid = _put_fact(
            conn, ws, revision_id, fact, node_id, concept, keys, produced["fact"], old, newest
        )
        if fid is None:
            continue
        produced["fact"].add(fid)
        if newest and old and old != fid:
            pairs.append((fid, old))
    for new, old in pairs:
        if old not in produced["fact"]:  # another fact of this generation reproduced it
            reconcile.supersede_ref(conn, ws, new, old, session_id)

    for kind, ids in produced.items():
        for item_id in sorted(ids):
            graph.add_support(conn, ws, kind, item_id, revision_id)
    superseded = reconcile.workspace(conn, ws, session_id, older, produced) if reconcile_ok else 0
    missing = list({normalise(c): c for c in waiting.values()}.values())
    return produced, missing, superseded


def _queue(
    conn: psycopg.Connection,
    ws: str,
    session_id: str,
    revision_id: str,
    item: dict[str, Any],
    key: str,
) -> bool:
    row = conn.execute(
        "INSERT INTO control.general_queue (workspace, session_id, revision_id, item, item_key) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (workspace, revision_id, item_key) DO NOTHING RETURNING id",
        (ws, session_id, revision_id, Jsonb(item), key),
    ).fetchone()
    if row is None:
        return False
    jobs.enqueue(conn, ws, "gate", str(row[0]), {"queue_id": row[0]})
    return True


def _apply_general(
    db: Database,
    ws: str,
    session_id: str,
    revision_id: str,
    general: dict[str, Any],
    waiting: list[str],
) -> int:
    """Publisher half: queue what is new, return the number of queued rows. Additive: rows of
    other revisions are left alone. A concept is skipped when its name is published, already
    waiting in this workspace, or a current general concept. A fact is skipped when its text
    is published or already waiting in this workspace, or is within FACT_RATIO of a fact this
    session queued before."""
    concepts = list(general.get("concepts") or [])
    listed = {normalise(c["name"]) for c in concepts}
    for name in waiting:  # a waiting local concept always goes to the gate
        if normalise(name) not in listed:
            concepts.append({"name": name, "type": "concept", "aliases": []})
            listed.add(normalise(name))
    queued = 0
    with db.publisher_tx() as conn:
        known = reconcile.published_sources(conn, "concept") | reconcile.live_sources(
            conn, ws, "concept"
        )
        for c in concepts:
            aliases = list(c.get("aliases") or [])
            key = item_key("concept", c["name"])
            if normalise(c["name"]) in known:
                continue
            if general_concept(conn, [c["name"], *aliases]) is not None:
                continue
            item = {"kind": "concept", "name": c["name"], "type": c["type"], "aliases": aliases}
            item["source_text"] = c["name"]
            queued += _queue(conn, ws, session_id, revision_id, item, key)
            known.add(normalise(c["name"]))
        exact = reconcile.published_sources(conn, "fact") | reconcile.live_sources(conn, ws, "fact")
        near = reconcile.session_sources(conn, ws, session_id)
        for f in general.get("facts") or []:
            target = normalise(f["text"])
            key = item_key("fact", f["text"])
            if target in exact or any(
                similarity(target, src, FACT_RATIO) >= FACT_RATIO for src in near
            ):
                continue
            item = {"kind": "fact", "type": f["type"], "text": f["text"], "concept": f["concept"]}
            item["source_text"] = f["text"]
            queued += _queue(conn, ws, session_id, revision_id, item, key)
            exact.add(target)
            near.append(target)
    return queued


def run(db: Database, job: jobs.Job) -> str:
    """Apply one stored generation: "done" or "skipped". Does not complete/fail the job."""
    ws = job.workspace
    revision_id = str(job.payload.get("revision_id") or job.key)
    schema = sql.Identifier(names.ws_schema(ws))
    with db.ws_tx(ws) as conn:
        row = conn.execute(
            sql.SQL("SELECT session_id::text FROM {s}.revisions WHERE id = %s").format(s=schema),
            (revision_id,),
        ).fetchone()
        if row is None:
            return "skipped"
        session_id = row[0]
        conn.execute(
            sql.SQL("SELECT 1 FROM {s}.sessions WHERE id = %s FOR UPDATE").format(s=schema),
            (session_id,),
        )
        row = conn.execute(
            sql.SQL(
                "SELECT r.status, r.n, (SELECT max(n) FROM {s}.revisions x "
                " WHERE x.session_id = r.session_id AND x.status = 'stored'), g.output "
                "FROM {s}.revisions r LEFT JOIN {s}.generations g ON g.revision_id = r.id "
                "WHERE r.id = %s"
            ).format(s=schema),
            (revision_id,),
        ).fetchone()
        if row[0] != "stored" or row[3] is None:
            log.info("apply skipped revision=%s", revision_id)
            return "skipped"
        n, gen = row[1], row[3]
        newest = row[1] == row[2]
        older = [
            r[0]
            for r in conn.execute(
                sql.SQL(
                    "SELECT id::text FROM {s}.revisions WHERE session_id = %s AND n < %s ORDER BY n"
                ).format(s=schema),
                (session_id, n),
            )
        ]
        ok = trusted(gen)
        if not ok:
            log.warning("apply untrusted_generation revision=%s reconcile=skipped", revision_id)
        produced, waiting, superseded = _apply_workspace(
            conn, ws, revision_id, session_id, older, gen, ok and newest, newest
        )
        jobs.enqueue(conn, ws, "embed", f"apply:{job.key}", {"revision_id": None})
    queued = _apply_general(db, ws, session_id, revision_id, gen.get("general") or {}, waiting)
    log.info(
        "apply done revision=%s nodes=%d edges=%d facts=%d waiting=%d superseded=%d queued=%d",
        revision_id,
        len(produced["node"]),
        len(produced["edge"]),
        len(produced["fact"]),
        len(waiting),
        superseded,
        queued,
    )
    return "done"


def handle(db: Database, job: jobs.Job) -> str:
    """Run and complete the job (``skipped`` when run skipped); returns the run status.
    Exceptions propagate to the caller, which fails the job with ``MAX_ATTEMPTS``."""
    status = run(db, job)
    jobs.complete(db, job, status="skipped" if status == "skipped" else "done")
    return status
