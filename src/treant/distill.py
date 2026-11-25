"""Distill stage: one model call per newest stored revision, validated and stored.

``run`` reads the revision (one workspace transaction), calls the model with no transaction
open, then stores the validated generation and enqueues the ``apply`` job in ONE workspace
transaction. ``run`` returns a status string and never completes or fails the job itself;
``BackendUnavailable`` (and its subclasses) propagate. A reply that is not a JSON object stores
nothing and raises ``BackendBadReply``. The stored output carries ``stats`` (items kept and
dropped) so apply can refuse to reconcile on a mostly broken reply. ``handle`` wraps ``run``
with the job bookkeeping: it maps those exceptions to the categories ``model_bad_reply``,
``model_unavailable`` and ``model_rejected``, and returns the status, or the category when the
job was failed.

Logs carry counts and ids only, never note text, prompts, replies or names.
"""

import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from importlib import resources
from typing import Any

from psycopg import sql
from psycopg.types.json import Jsonb

from treant import backends, graph, jobs, names, sessions, settings
from treant.backends import BackendBadReply, BackendRejected, BackendUnavailable
from treant.db import Database
from treant.scrub import scrub

log = logging.getLogger(__name__)

Generation = dict[str, Any]

MAX_NODES, MAX_EDGES, MAX_FACTS = 100, 200, 200
MAX_GENERAL_CONCEPTS, MAX_GENERAL_FACTS = 50, 100
MAX_TEXT, MAX_NAME = 2000, 200
MAX_ALIASES = 20
MAX_REPLY = 200_000
MAX_MATCHES = 40
MIN_MATCH_LEN = 3
MAX_ATTEMPTS = 12
BAD_REPLY_ATTEMPTS = 3

_FENCE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL | re.IGNORECASE)
_WS_KINDS = ("instance", "local_concept")


def _prompt() -> str:
    return (resources.files("treant") / "prompts" / "distill.md").read_text(encoding="utf-8")


def _str(value: Any, limit: int) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= limit or not value.strip():
        return None
    if "\x00" in value:
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return value


def _aliases(value: Any) -> list[str] | None:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ALIASES:
        return None
    out = [_str(a, MAX_NAME) for a in value]
    return None if None in out else out  # type: ignore[return-value]


def _items(value: Any, cap: int) -> tuple[list[Any], int]:
    """The dict items of a list within ``cap``, and how many entries were dropped."""
    if value is None:
        return [], 0
    if not isinstance(value, list):
        return [], 1
    kept = value[:cap]
    return kept, len(value) - len(kept)


def _node(item: Any, seen: set[str]) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    ref, kind = _str(item.get("ref"), MAX_NAME), item.get("kind")
    if not isinstance(kind, str):
        return None
    type_, name = _str(item.get("type"), MAX_NAME), _str(item.get("name"), MAX_NAME)
    aliases = _aliases(item.get("aliases"))
    if not (ref and type_ and name) or kind not in _WS_KINDS or aliases is None or ref in seen:
        return None
    node = {"ref": ref, "kind": kind, "type": type_, "name": name, "aliases": aliases}
    if kind == "local_concept":
        concept = _str(item.get("concept"), MAX_NAME)
        if concept is None:
            return None
        node["concept"] = concept
    return node


def _edge(item: Any, refs: set[str]) -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None
    src, dst, type_ = item.get("src"), item.get("dst"), item.get("type")
    if not all(isinstance(v, str) for v in (src, dst, type_)):
        return None
    if src not in refs or dst not in refs or type_ not in graph.EDGE_TYPES:
        return None
    return {"src": src, "dst": dst, "type": type_}


def _fact_type(value: Any) -> bool:
    return isinstance(value, str) and value in graph.FACT_TYPES


def _fact(item: Any, refs: set[str]) -> dict[str, Any] | None:
    if not isinstance(item, dict) or not _fact_type(item.get("type")):
        return None
    text = _str(item.get("text"), MAX_TEXT)
    node, supersedes = item.get("node"), item.get("supersedes")
    if text is None or (node is not None and not (isinstance(node, str) and node in refs)):
        return None
    if supersedes is not None and _str(supersedes, MAX_NAME) is None:
        return None
    return {"type": item["type"], "text": text, "node": node, "supersedes": supersedes}


def _concept(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    name, type_, aliases = (
        _str(item.get("name"), MAX_NAME),
        _str(item.get("type"), MAX_NAME),
        _aliases(item.get("aliases")),
    )
    if not (name and type_) or aliases is None:
        return None
    return {"name": name, "type": type_, "aliases": aliases}


def _general_fact(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict) or not _fact_type(item.get("type")):
        return None
    text, concept = _str(item.get("text"), MAX_TEXT), _str(item.get("concept"), MAX_NAME)
    if text is None or concept is None:
        return None
    return {"type": item["type"], "text": text, "concept": concept}


def empty_generation() -> Generation:
    return {"nodes": [], "edges": [], "facts": [], "general": {"concepts": [], "facts": []}}


def reply_object(text: Any) -> dict | None:
    """The JSON object of a model reply (one ```json fence tolerated), or None when the reply
    is not one: not JSON, truncated, over ``MAX_REPLY``, or another JSON value."""
    if not isinstance(text, str) or len(text) > MAX_REPLY:
        return None
    body = text.strip()
    fenced = _FENCE.match(body)
    if fenced:
        body = fenced.group(1)
    try:
        raw = json.loads(body)
    except (ValueError, RecursionError):
        return None
    return raw if isinstance(raw, dict) else None


def parse_generation(text: str) -> tuple[Generation, int]:
    """Validate a model reply item by item; invalid items are dropped and counted.

    A reply that is not a JSON object is the empty generation with one drop; ``run`` never
    stores that (it raises ``BackendBadReply`` instead)."""
    raw = reply_object(text)
    if raw is None:
        return empty_generation(), 1
    return parse_object(raw)


def kept_count(gen: Generation) -> int:
    """How many items a generation holds (both halves)."""
    general = gen.get("general") or {}
    return sum(len(gen.get(k) or []) for k in ("nodes", "edges", "facts")) + sum(
        len(general.get(k) or []) for k in ("concepts", "facts")
    )


def parse_object(raw: dict) -> tuple[Generation, int]:
    """Validate the JSON object of a reply item by item; invalid items are dropped and counted."""
    dropped = 0
    gen = empty_generation()

    def collect(value: Any, cap: int, make: Callable[[Any], dict | None]) -> list[dict]:
        nonlocal dropped
        items, over = _items(value, cap)
        dropped += over
        out = []
        for item in items:
            try:
                made = make(item)
            except (TypeError, ValueError, KeyError):
                made = None
            if made is None:
                dropped += 1
            else:
                out.append(made)
        return out

    seen: set[str] = set()

    def make_node(item: Any) -> dict | None:
        node = _node(item, seen)
        if node is not None:
            seen.add(node["ref"])
        return node

    gen["nodes"] = collect(raw.get("nodes"), MAX_NODES, make_node)
    refs = {n["ref"] for n in gen["nodes"]}
    gen["edges"] = collect(raw.get("edges"), MAX_EDGES, lambda i: _edge(i, refs))
    gen["facts"] = collect(raw.get("facts"), MAX_FACTS, lambda i: _fact(i, refs))
    general = raw.get("general")
    if general is None:
        general = {}
    if not isinstance(general, dict):
        dropped += 1
        general = {}
    gen["general"]["concepts"] = collect(general.get("concepts"), MAX_GENERAL_CONCEPTS, _concept)
    gen["general"]["facts"] = collect(general.get("facts"), MAX_GENERAL_FACTS, _general_fact)
    return gen, dropped


def _read(db: Database, ws: str, revision_id: str) -> tuple[dict, list[dict], list[str]] | None:
    """(sections, matching facts, general names) of a stored revision; else None. Every stored
    revision is distilled, also when the session has a newer one: saves are additive."""
    schema = sql.Identifier(names.ws_schema(ws))
    with db.ws_tx(ws) as conn:
        row = conn.execute(
            sql.SQL("SELECT r.sections, r.status FROM {s}.revisions r WHERE r.id = %s").format(
                s=schema
            ),
            (revision_id,),
        ).fetchone()
        if row is None or row[1] != "stored":
            return None
        sections = row[0]
        haystack = json.dumps(sections, ensure_ascii=False)
        facts = conn.execute(
            sql.SQL(
                "SELECT f.id::text, f.type, f.text, n.name FROM {s}.facts f "
                "JOIN {s}.nodes n ON n.id = f.node_id "
                "WHERE f.status = 'current' AND n.status = 'current' AND ("
                " (length(n.name) >= %(min)s AND position(lower(n.name) in lower(%(h)s)) > 0)"
                " OR EXISTS (SELECT 1 FROM unnest(n.aliases) a "
                "   WHERE length(a) >= %(min)s AND position(lower(a) in lower(%(h)s)) > 0)) "
                "ORDER BY f.created_at DESC, f.id LIMIT %(lim)s"
            ).format(s=schema),
            {"min": MIN_MATCH_LEN, "h": haystack, "lim": MAX_MATCHES},
        ).fetchall()
        concepts = conn.execute(
            "SELECT n.name FROM general.nodes n WHERE n.status = 'current' AND ("
            " (length(n.name) >= %(min)s AND position(lower(n.name) in lower(%(h)s)) > 0)"
            " OR EXISTS (SELECT 1 FROM unnest(n.aliases) a "
            "   WHERE length(a) >= %(min)s AND position(lower(a) in lower(%(h)s)) > 0)) "
            "ORDER BY n.created_at DESC, n.id LIMIT %(lim)s",
            {"min": MIN_MATCH_LEN, "h": haystack, "lim": MAX_MATCHES},
        ).fetchall()
    matches = [{"id": r[0], "type": r[1], "text": r[2], "node": r[3]} for r in facts]
    return sections, matches, [r[0] for r in concepts]


def build_messages(
    sections: dict, matches: list[dict], concepts: list[str], terms: Sequence[str] = ()
) -> tuple[list[dict], dict[str, str]]:
    """The model messages and the fN -> fact id map. Existing fact texts are scrubbed with the
    workspace terms first (a term added later, or a fact edited before scrubbing existed); a
    fact the scrubber is not confident about is left out of the prompt."""
    kept = []
    for m in matches:
        cleaned = scrub(m["text"], terms)
        if cleaned.confident:
            kept.append({**m, "text": cleaned.text})
    refs = {f"f{i}": m["id"] for i, m in enumerate(kept, 1)}
    facts = [
        {"ref": f"f{i}", "type": m["type"], "text": m["text"], "node": m["node"]}
        for i, m in enumerate(kept, 1)
    ]
    body = {"sections": sections, "existing_facts": facts, "general_concepts": concepts}
    messages = [
        {"role": "system", "content": _prompt()},
        {"role": "user", "content": json.dumps(body, ensure_ascii=False)},
    ]
    return messages, refs


def run(
    db: Database,
    env: Mapping[str, str],
    job: jobs.Job,
    backend_factory: Callable[..., backends.Backend | None] = backends.for_workspace,
) -> str:
    """Distill one revision: "done", "skipped" or "no_backend". Does not complete/fail the job."""
    ws = job.workspace
    revision_id = str(job.payload.get("revision_id") or job.key)
    read = _read(db, ws, revision_id)
    if read is None:
        return "skipped"
    s = settings.load(db, ws)
    backend = backend_factory(env, ws, s)
    if backend is None:
        return "no_backend"
    sections, matches, concepts = read
    messages, fact_refs = build_messages(sections, matches, concepts, s.terms)
    reply = backend.complete(messages, alias=s.alias)  # no transaction is open here

    raw = reply_object(reply)
    if raw is None:  # never store an empty generation: apply would supersede everything
        log.warning("distill bad_reply not_object revision=%s", revision_id)
        raise BackendBadReply("reply was not a JSON object")
    gen, dropped = parse_object(raw)
    for fact in gen["facts"]:
        if fact["supersedes"] is not None and fact["supersedes"] not in fact_refs:
            fact["supersedes"] = None
    kept = kept_count(gen)
    if dropped:
        log.warning("distill bad_reply dropped=%d kept=%d revision=%s", dropped, kept, revision_id)
    output = {
        **gen,
        "fact_refs": fact_refs,
        "dropped": dropped,
        "stats": {"kept": kept, "dropped": dropped},
    }
    schema = sql.Identifier(names.ws_schema(ws))
    with db.ws_tx(ws) as conn:
        conn.execute(
            sql.SQL(
                "INSERT INTO {s}.generations (revision_id, output) VALUES (%s, %s) "
                "ON CONFLICT (revision_id) DO UPDATE SET output = EXCLUDED.output, "
                "created_at = now()"
            ).format(s=schema),
            (revision_id, Jsonb(output)),
        )
        jobs.enqueue(conn, ws, "apply", revision_id, {"revision_id": revision_id})
    return "done"


def handle(
    db: Database,
    env: Mapping[str, str],
    job: jobs.Job,
    backend_factory: Callable[..., backends.Backend | None] = backends.for_workspace,
) -> str:
    """Run and settle the job: complete on any status, fail with a category on model errors.

    Returns the run status, or the failure category: ``model_bad_reply`` (at most
    ``BAD_REPLY_ATTEMPTS``: the same prompt tends to fail the same way), ``model_rejected`` or
    ``model_unavailable`` (``MAX_ATTEMPTS``, enough to ride out an outage of hours).
    Other exceptions propagate for the caller to categorise.
    """
    try:
        status = run(db, env, job, backend_factory)
    except BackendBadReply:
        jobs.fail(db, job, "model_bad_reply", max_attempts=BAD_REPLY_ATTEMPTS)
        return "model_bad_reply"
    except BackendRejected:
        jobs.fail(db, job, "model_rejected", max_attempts=MAX_ATTEMPTS)
        return "model_rejected"
    except BackendUnavailable:
        jobs.fail(db, job, "model_unavailable", max_attempts=MAX_ATTEMPTS)
        return "model_unavailable"
    jobs.complete(db, job, status="skipped" if status == "skipped" else "done")
    return status


def backfill(db: Database, ws: str, dry_run: bool = False) -> dict[str, int]:
    """Owner action: distill the revisions saved before the distill stage existed (or whose
    job was lost, or that were skipped while only a session's newest revision was distilled).
    Every stored revision with no generation and no distill job (other than a ``skipped``
    one, which is queued again) is re-scrubbed with the current terms. A changed scrub stores the new sections
    and journal text (the journal's embedding is dropped and an embed job queued); a scrub that
    is not confident quarantines the revision; otherwise a distill job is queued (key = the
    revision id). Returns counts only. ``dry_run`` writes nothing."""
    names.validate_workspace(ws)
    terms = settings.load(db, ws).terms  # InvalidInput: unknown workspace or bad settings
    schema = sql.Identifier(names.ws_schema(ws))
    counts = {"candidates": 0, "queued": 0, "rescrubbed": 0, "quarantined": 0}
    with db.admin_tx() as conn:
        rows = conn.execute(
            sql.SQL(
                "SELECT r.id::text, r.sections FROM {s}.revisions r "
                "WHERE r.status = 'stored' "
                "AND NOT EXISTS (SELECT 1 FROM {s}.generations g WHERE g.revision_id = r.id) "
                "AND NOT EXISTS (SELECT 1 FROM control.jobs j WHERE j.workspace = %s "
                "  AND j.kind = 'distill' AND j.key = r.id::text AND j.status <> 'skipped') "
                "ORDER BY r.created_at, r.id FOR UPDATE OF r"
            ).format(s=schema),
            (ws,),
        ).fetchall()
        for revision_id, stored in rows:
            counts["candidates"] += 1
            tally = sessions._Tally(terms)
            cleaned = tally.sections(stored)
            changed = cleaned != stored
            counts["rescrubbed"] += changed
            if not tally.confident:
                counts["quarantined"] += 1
            else:
                counts["queued"] += 1
            if dry_run:
                continue
            if changed:
                conn.execute(
                    sql.SQL(
                        "UPDATE {s}.revisions SET sections = %s, removed = removed + %s "
                        "WHERE id = %s"
                    ).format(s=schema),
                    (Jsonb(cleaned), tally.removed, revision_id),
                )
                conn.execute(
                    sql.SQL(
                        "UPDATE {s}.journal_entries SET text = %s WHERE revision_id = %s"
                    ).format(s=schema),
                    (sessions._journal_text(cleaned), revision_id),
                )
            if changed or not tally.confident:
                conn.execute(
                    sql.SQL(
                        "DELETE FROM {s}.embeddings WHERE item_kind = 'journal' AND item_id IN "
                        "(SELECT id FROM {s}.journal_entries WHERE revision_id = %s)"
                    ).format(s=schema),
                    (revision_id,),
                )
            if not tally.confident:
                conn.execute(
                    sql.SQL("UPDATE {s}.revisions SET status = 'quarantined' WHERE id = %s").format(
                        s=schema
                    ),
                    (revision_id,),
                )
                continue
            payload = {"revision_id": revision_id}
            if changed:
                jobs.enqueue(conn, ws, "embed", f"backfill:{revision_id}", payload)
            jobs.enqueue(conn, ws, "distill", revision_id, payload)
            # a revision skipped under the newest-only rule still has its job: queue it again
            conn.execute(
                "UPDATE control.jobs SET status = 'queued', attempts = 0, run_after = now(), "
                "lease_id = NULL, lease_until = NULL "
                "WHERE workspace = %s AND kind = 'distill' AND key = %s AND status = 'skipped'",
                (ws, revision_id),
            )
        if not dry_run:
            conn.execute(
                "INSERT INTO control.audit (actor, action, detail) VALUES ('cli', %s, %s)",
                ("distill.backfill", Jsonb({"workspace": ws, **counts})),
            )
    log.info(
        "distill backfill candidates=%d queued=%d rescrubbed=%d quarantined=%d dry_run=%s",
        counts["candidates"],
        counts["queued"],
        counts["rescrubbed"],
        counts["quarantined"],
        dry_run,
    )
    return counts
