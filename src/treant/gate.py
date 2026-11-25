"""Gate stage: decide whether one queued general item may be published to ``general``.

One ``gate`` job per ``control.general_queue`` row (key = queue id, rewrites ``<id>:r<n>``).
Flow: the row must still be ``pending`` (else ``stale``, nothing written). The checked text is
the fact text, or a concept's name, aliases and type. Rules (``gate_rules``) run first; when
they find nothing, the judges are asked with the question for the item's kind (``criteria``:
``question`` for facts, ``concept_question`` for concepts). The LLM judge goes first; Jev is
asked only after its yes, and a pass needs both (the LLM's yes and Jev score >= threshold). A
pass publishes (workspace ``publish = auto``) or waits for the owner (``review``). With
``TREANT_JEV=off`` Jev is never called and the LLM's yes alone is not enough to publish: the
item waits for the owner whatever the workspace's publish mode. A failure
rewrites a fact through the origin workspace's backend (at most ``MAX_REWRITES`` times, then
``blocked``); a concept is never rewritten and goes straight to ``blocked``. A workspace with
``backend = "none"`` never sends anything to a model: its items are held as ``blocked`` with
reason ``judge:none``.

Rulings: ``item.source_text`` and ``item_key`` never change; a rewrite or an owner override
changes ``item.text`` only. Reasons are ``{"category", "start", "end"}`` of the checked text,
never the text itself. The rewrite prompt gets only the fact text and its findings (with the
span substrings, which the text already holds); the judge prompt gets only the question and the
text. Neither ever sees a workspace name, origin, another item or the deny list.

``run`` returns a status and raises ``Retry`` (category) for anything that must wait: a judge
down, refusing or answering nothing usable (``judge_unavailable``/``judge_rejected``/
``judge_bad_reply``), the rewrite model down (``model_unavailable``/``model_rejected``),
unparseable settings or ``TREANT_JEV`` (``settings_invalid``). ``handle`` completes the job, or fails it with that
category and its attempt cap: 3 for ``judge_bad_reply`` and ``judge_rejected`` (a retry repeats
the same deterministic call), ``MAX_ATTEMPTS`` (20) for the rest. An empty rewrite reply counts
as a bad rewrite (capped by ``MAX_REWRITES``), not an outage.

Logs carry the stage, queue ids, statuses and categories only.
"""

import json
import logging
import math
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any, Self

import psycopg
from psycopg.types.json import Jsonb

from treant import apply, backends, config, gate_rules, graph, jobs, settings
from treant.backends import BackendBadReply, BackendRejected, BackendUnavailable
from treant.db import Database
from treant.errors import InvalidInput
from treant.reconcile import normalise
from treant.settings import WorkspaceSettings

log = logging.getLogger(__name__)

MAX_REWRITES = 2
MAX_ATTEMPTS = 20
ATTEMPTS = {"judge_bad_reply": 3, "judge_rejected": 3}  # categories capped below MAX_ATTEMPTS
MAX_TEXT = 2000
MAX_REPLY = 20_000
MAX_OBJECT_TRIES = 50
JUDGE_MAX_TOKENS = 2000  # room for hidden reasoning; 200 gave empty replies live
REWRITE_MAX_TOKENS = 4000
KINDS = ("fact", "concept")
OPEN = ("pending", "waiting_review", "blocked")
ACTORS = ("gate", "owner")

_FENCE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL | re.IGNORECASE)

BackendFactory = Callable[..., backends.Backend | None]


class Retry(Exception):
    """The item must wait: the job fails with ``category`` and is retried."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class _Changed(Exception):
    """The row changed after the gate read it (another job or the owner acted)."""


class _NotExisting(Exception):
    """A reuse-only publish found no general concept to reuse."""


@dataclass(frozen=True)
class Judges:
    """Injected judges: each is called with (checked text, item kind "fact" | "concept")."""

    jev: Callable[[str, str], float]
    llm: Callable[[str, str], bool]


@lru_cache(maxsize=1)
def criteria() -> dict[str, Any]:
    raw = (resources.files("treant") / "criteria.toml").read_text(encoding="utf-8")
    data = tomllib.loads(raw)
    return {
        "question": str(data["question"]),
        "concept_question": str(data["concept_question"]),
        "jev_threshold": float(data["jev_threshold"]),
    }


def question_for(kind: str) -> str:
    """The question both judges answer for an item of this kind."""
    if kind not in KINDS:
        raise InvalidInput("unknown item kind")
    return criteria()["concept_question" if kind == "concept" else "question"]


def _prompt(name: str) -> str:
    return (resources.files("treant") / "prompts" / name).read_text(encoding="utf-8")


def _valid_text(value: Any) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_TEXT or not value.strip():
        return None
    if "\x00" in value:
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return value


def _json_reply(reply: Any) -> Any:
    """The JSON value of a model reply (one ```json fence tolerated); None if it is not JSON."""
    if not isinstance(reply, str) or len(reply) > MAX_REPLY:
        return None
    body = reply.strip()
    fenced = _FENCE.match(body)
    if fenced:
        body = fenced.group(1)
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        return None


def _first_object(reply: Any) -> Any:
    """The reply as JSON, else the first JSON object embedded in it (models sometimes wrap the
    object in prose); None when there is none."""
    value = _json_reply(reply)
    if value is not None or not isinstance(reply, str) or len(reply) > MAX_REPLY:
        return value
    decoder = json.JSONDecoder()
    start = reply.find("{")
    for _ in range(MAX_OBJECT_TRIES):
        if start == -1:
            break
        try:
            value, _end = decoder.raw_decode(reply, start)
        except (ValueError, RecursionError):
            start = reply.find("{", start + 1)
            continue
        return value if isinstance(value, dict) else None
    return None


def llm_judge(backend: backends.Backend, alias: str) -> Callable[[str, str], bool]:
    """The LLM judge over a backend, called with (text, kind). Its verdict is the first JSON
    object of the reply, which must be ``{"ok": true|false}``; anything else is
    ``BackendBadReply``: never a pass."""

    def judge(text: str, kind: str) -> bool:
        body = {"subject": kind, "question": question_for(kind), "text": text}
        messages = [
            {"role": "system", "content": _prompt("judge.md")},
            {"role": "user", "content": json.dumps(body, ensure_ascii=False)},
        ]
        reply = backend.complete(messages, alias=alias, max_tokens=JUDGE_MAX_TOKENS)
        value = _first_object(reply)
        if not isinstance(value, dict) or not isinstance(value.get("ok"), bool):
            raise BackendBadReply("judge reply was not usable")
        return value["ok"]

    return judge


class DefaultJudges:
    """The production judges and the origin workspace's backend for one gate run.

    Jev (URL/model from ``config.jev_endpoint``, key ``TREANT_JEV_API_KEY``) and the LLM judge
    (origin backend, ``judge_alias``). Each client is built once, on first use, so a missing
    key or backend surfaces as ``BackendUnavailable`` from the judge call; ``close`` (or the
    ``with`` block) closes what was built. The same backend serves the rewrite.
    """

    def __init__(
        self,
        env: Mapping[str, str],
        ws: str,
        s: WorkspaceSettings,
        backend_factory: BackendFactory = backends.for_workspace,
    ) -> None:
        self._env, self._ws, self._s, self._factory = env, ws, s, backend_factory
        self._jev: backends.JevJudge | None = None
        self._backend: backends.Backend | None = None
        self._built = False

    def backend(self) -> backends.Backend | None:
        if not self._built:
            self._backend = self._factory(self._env, self._ws, self._s)
            self._built = True
        return self._backend

    def jev(self, text: str, kind: str) -> float:
        if self._jev is None:
            key = self._env.get("TREANT_JEV_API_KEY")
            if not key:
                raise BackendUnavailable("jev is not configured")
            url, model = config.jev_endpoint(self._env)
            self._jev = backends.JevJudge(url, model, key)
        return self._jev.score(text, question_for(kind))

    def llm(self, text: str, kind: str) -> bool:
        backend = self.backend()
        if backend is None:
            raise BackendUnavailable("no judge backend")
        return llm_judge(backend, self._s.judge_alias)(text, kind)

    def close(self) -> None:
        for client in (self._jev, self._backend):
            close = getattr(client, "close", None)
            if callable(close):
                close()
        self._jev, self._backend, self._built = None, None, False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def default_judges(
    env: Mapping[str, str],
    ws: str,
    s: WorkspaceSettings,
    backend_factory: BackendFactory = backends.for_workspace,
) -> DefaultJudges:
    """Production judges for one run; use as a context manager so the clients get closed."""
    return DefaultJudges(env, ws, s, backend_factory)


def _checked_text(item: dict[str, Any]) -> str:
    if item["kind"] == "concept":
        return "\n".join([item["name"], *(item.get("aliases") or []), item["type"]])
    return item["text"]


def _reason(category: str, start: int, end: int) -> dict[str, Any]:
    return {"category": category, "start": start, "end": end}


def _load(db: Database, queue_id: int) -> dict[str, Any] | None:
    with db.publisher_tx() as conn:
        row = conn.execute(
            "SELECT workspace, item, status, rewrites FROM control.general_queue WHERE id = %s",
            (queue_id,),
        ).fetchone()
    if row is None:
        return None
    return {"workspace": row[0], "item": row[1], "status": row[2], "rewrites": row[3]}


def _settle(db: Database, queue_id: int, rewrites: int, status: str, reasons: list) -> bool:
    """Move a still-pending row (unchanged since it was read) to status with reasons."""
    decided = "now()" if status == "blocked" else "NULL"
    with db.publisher_tx() as conn:
        cur = conn.execute(
            "UPDATE control.general_queue SET status = %s, reasons = %s, "
            "decided_at = " + decided + " "
            "WHERE id = %s AND status = 'pending' AND rewrites = %s",
            (status, Jsonb(reasons), queue_id, rewrites),
        )
    return cur.rowcount == 1


def _hold(db: Database, queue_id: int, rewrites: int, status: str, reasons: list) -> str:
    return status if _settle(db, queue_id, rewrites, status, reasons) else "stale"


def _judge(
    judges: Judges | DefaultJudges, text: str, kind: str, jev_on: bool = True
) -> list[dict[str, Any]]:
    """Ask the LLM judge, then (only after its yes, and only when ``jev_on``) Jev; the reasons
    of the one that did not pass. A "no" from the LLM fails the item without paying for Jev.
    With Jev off the caller must not publish on an empty result: it only means "review"."""
    try:
        ok = judges.llm(text, kind)
        if ok is not True:
            return [_reason("judge:llm", 0, len(text))]
        if not jev_on:
            return []
        score = judges.jev(text, kind)
    except BackendBadReply:
        raise Retry("judge_bad_reply") from None
    except BackendRejected:
        raise Retry("judge_rejected") from None
    except BackendUnavailable:
        raise Retry("judge_unavailable") from None
    reasons = []
    if (
        isinstance(score, bool)
        or not isinstance(score, int | float)
        or not math.isfinite(score)
        or not 0 <= score <= 1
    ):  # injected judges fail closed too: never a pass on a score that is not a score
        raise Retry("judge_unavailable")
    if score < criteria()["jev_threshold"]:
        reasons.append(_reason("judge:jev", 0, len(text)))
    return reasons


def _rewrite(backend: backends.Backend, alias: str, text: str, reasons: list) -> str | None:
    """A rewritten text, or None when the reply was not ``{"text": "..."}`` within limits."""
    findings = [{**r, "span": text[r["start"] : r["end"]]} for r in reasons]
    body = {"text": text, "findings": findings}
    messages = [
        {"role": "system", "content": _prompt("rewrite.md")},
        {"role": "user", "content": json.dumps(body, ensure_ascii=False)},
    ]
    try:
        reply = backend.complete(messages, alias=alias, max_tokens=REWRITE_MAX_TOKENS)
    except BackendBadReply:
        return None  # an empty answer is a failed rewrite (counted), not an outage
    except BackendRejected:
        raise Retry("model_rejected") from None
    except BackendUnavailable:
        raise Retry("model_unavailable") from None
    value = _json_reply(reply)
    if not isinstance(value, dict):
        return None
    return _valid_text(value.get("text"))


def _store_rewrite(
    db: Database, queue_id: int, ws: str, rewrites: int, text: str | None, reasons: list
) -> bool:
    """Count a rewrite (store the new text when there is one) and enqueue the re-gate job."""
    n = rewrites + 1
    with db.publisher_tx() as conn:
        if text is None:
            cur = conn.execute(
                "UPDATE control.general_queue SET rewrites = %s, reasons = %s "
                "WHERE id = %s AND status = 'pending' AND rewrites = %s",
                (n, Jsonb(reasons), queue_id, rewrites),
            )
        else:
            cur = conn.execute(
                "UPDATE control.general_queue SET rewrites = %s, reasons = %s, "
                "item = jsonb_set(item, '{text}', to_jsonb(%s::text)) "
                "WHERE id = %s AND status = 'pending' AND rewrites = %s",
                (n, Jsonb(reasons), text, queue_id, rewrites),
            )
        if cur.rowcount != 1:
            return False
        jobs.enqueue(conn, ws, "gate", f"{queue_id}:r{n}", {"queue_id": queue_id})
    return True


def _existing_concept(db: Database, item: dict[str, Any]) -> bool:
    if item["kind"] != "concept":
        return False
    with db.publisher_tx() as conn:
        keys = [item["name"], *(item.get("aliases") or [])]
        return apply.general_concept(conn, keys) is not None


def run(
    db: Database,
    env: Mapping[str, str],
    job: jobs.Job,
    judges: Judges | None = None,
    backend_factory: BackendFactory = backends.for_workspace,
) -> str:
    """Gate one queue item: "published", "waiting_review", "blocked", "rewritten" or "stale".
    Does not complete or fail the job; raises ``Retry`` when the item must wait."""
    queue_id = int(job.payload.get("queue_id") or job.key.split(":", 1)[0])
    row = _load(db, queue_id)
    if row is None or row["status"] != "pending":
        return "stale"
    ws, item, rewrites = row["workspace"], row["item"], row["rewrites"]
    expect = ("pending", rewrites)
    if _existing_concept(db, item):
        # No new text enters general: publish (reuse) without rules or judges, whatever the
        # workspace's backend or publish mode. If the concept vanished meanwhile, gate it.
        try:
            _publish(db, queue_id, None, "gate", expect=expect, reuse_only=True)
            return "published"
        except _Changed:
            return "stale"
        except _NotExisting:
            pass
    try:
        s = settings.load(db, ws)
        deny = gate_rules.load_deny(db)
    except InvalidInput:
        raise Retry("settings_invalid") from None
    try:
        jev_on = config.jev_enabled(env)
    except ValueError:
        raise Retry("settings_invalid") from None
    with default_judges(env, ws, s, backend_factory) as clients:
        return _gate(db, queue_id, ws, item, rewrites, s, deny, judges or clients, clients, jev_on)


def _gate(
    db: Database,
    queue_id: int,
    ws: str,
    item: dict[str, Any],
    rewrites: int,
    s: WorkspaceSettings,
    deny: gate_rules.DenyList,
    judges: Judges | DefaultJudges,
    clients: DefaultJudges,
    jev_on: bool = True,
) -> str:
    text = _checked_text(item)
    reasons = [_reason(f.category, f.start, f.end) for f in gate_rules.check(text, deny)]
    if s.backend == "none":  # nothing from this workspace goes to a model provider
        return _hold(
            db, queue_id, rewrites, "blocked", [*reasons, _reason("judge:none", 0, len(text))]
        )
    if not reasons:
        reasons = _judge(judges, text, item["kind"], jev_on)

    if not reasons:
        if s.publish == "review" or not jev_on:  # one judge never publishes on its own
            return _hold(db, queue_id, rewrites, "waiting_review", [])
        try:
            _publish(db, queue_id, None, "gate", expect=("pending", rewrites))
        except _Changed:
            return "stale"
        return "published"

    if item["kind"] != "fact" or rewrites >= MAX_REWRITES:
        return _hold(db, queue_id, rewrites, "blocked", reasons)
    try:
        backend = clients.backend()
    except BackendRejected:
        raise Retry("model_rejected") from None
    except BackendUnavailable:
        raise Retry("model_unavailable") from None
    if backend is None:
        return _hold(db, queue_id, rewrites, "blocked", reasons)
    new_text = _rewrite(backend, s.alias, text, reasons)
    if new_text is None:
        log.warning("gate bad_rewrite queue=%s", queue_id)
    if not _store_rewrite(db, queue_id, ws, rewrites, new_text, reasons):
        return "stale"
    return "rewritten"


def _lock_concepts(conn: psycopg.Connection, names: list[str]) -> None:
    """Serialise lookup-then-add of general concepts by normalised name (sorted: no deadlock)."""
    for key in sorted({normalise(n) for n in names if n}):
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("treant.concept:" + key,))


def _published_id(
    conn: psycopg.Connection, item: dict[str, Any], text: str, reuse_only: bool
) -> str:
    if item["kind"] == "concept":
        aliases = list(item.get("aliases") or [])
        keys = [item["name"], *aliases]
        _lock_concepts(conn, keys)
        existing = apply.general_concept(conn, keys)
        if existing is not None:
            return existing
        if reuse_only:
            raise _NotExisting()
        return graph.add_node(conn, "general", "concept", item["type"], item["name"], aliases)
    concept = item.get("concept")
    node_id = None
    if concept:
        _lock_concepts(conn, [concept])
        node_id = apply.general_concept(conn, [concept])
    return graph.add_fact(conn, "general", item["type"], text, node_id=node_id)


def _publish(
    db: Database,
    queue_id: int,
    text_override: str | None,
    actor: str,
    expect: tuple[str, int] | None = None,
    reuse_only: bool = False,
) -> str:
    """Publish in ONE publisher transaction. ``expect`` (status, rewrites) is the gate's view
    of the row; a row that changed since raises ``_Changed``. ``reuse_only`` (concepts) raises
    ``_NotExisting`` instead of creating a node, rolling everything back."""
    if actor not in ACTORS:
        raise InvalidInput("unknown actor")
    if text_override is not None and _valid_text(text_override) is None:
        raise InvalidInput("text must be 1-2000 characters of valid text")
    with db.publisher_tx() as conn:
        row = conn.execute(
            "SELECT workspace, session_id::text, revision_id::text, item, status, rewrites "
            "FROM control.general_queue WHERE id = %s FOR UPDATE",
            (queue_id,),
        ).fetchone()
        if row is None:
            if expect is not None:
                raise _Changed()
            raise InvalidInput("queue item not found")
        ws, session_id, revision_id, item, status, rewrites = row
        if expect is not None and (status, rewrites) != expect:
            raise _Changed()
        if status not in OPEN:
            raise InvalidInput("queue item is not open")
        if item["kind"] == "concept" and text_override is not None:
            raise InvalidInput("a concept takes no text override")
        text = text_override if text_override is not None else item.get("text")
        item_id = _published_id(conn, item, text, reuse_only)
        conn.execute(
            "INSERT INTO control.general_origin "
            "(item_kind, item_id, workspace, session_id, revision_id, queue_id) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (item["kind"], item_id, ws, session_id, revision_id, queue_id),
        )
        if text_override is not None:
            conn.execute(
                "UPDATE control.general_queue SET item = jsonb_set(item, '{text}', "
                "to_jsonb(%s::text)) WHERE id = %s",
                (text_override, queue_id),
            )
        conn.execute(
            "UPDATE control.general_queue SET status = 'published', published_id = %s, "
            "reasons = '[]', decided_at = now() WHERE id = %s",
            (item_id, queue_id),
        )
        if item["kind"] == "concept":  # waiting local concepts of that revision can now be made
            jobs.enqueue(conn, ws, "apply", f"concept:{queue_id}", {"revision_id": revision_id})
    log.info("gate published queue=%s kind=%s actor=%s", queue_id, item["kind"], actor)
    return item_id


def publish(
    db: Database, queue_id: int, text_override: str | None = None, actor: str = "gate"
) -> str:
    """Publish an open queue item (pending, waiting_review or blocked); the new (or reused,
    for a concept that now exists) general id. ``text_override`` replaces a fact's text.

    Only two callers may use this: the gate itself, after the rules and both judges passed
    (``actor="gate"``), and the owner's explicit approval (Task 8 CLI, ``actor="owner"``),
    which is the owner's final decision. Nothing else publishes to general; there is no check
    here, so never call it from a code path a workspace or a token can reach."""
    return _publish(db, queue_id, text_override, actor)


def handle(
    db: Database,
    env: Mapping[str, str],
    job: jobs.Job,
    judges: Judges | None = None,
    backend_factory: BackendFactory = backends.for_workspace,
) -> str:
    """Run and settle the job: complete on any status; on ``Retry`` fail it with the category
    and its cap (``ATTEMPTS``, else ``MAX_ATTEMPTS``). Returns the status or the category."""
    try:
        status = run(db, env, job, judges, backend_factory)
    except Retry as exc:
        jobs.fail(db, job, exc.category, max_attempts=ATTEMPTS.get(exc.category, MAX_ATTEMPTS))
        log.info("gate retry job=%s category=%s", job.id, exc.category)
        return exc.category
    jobs.complete(db, job)
    log.info("gate %s job=%s", status, job.id)
    return status
