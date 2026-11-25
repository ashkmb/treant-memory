"""Owner write actions, shared by the CLI (actor ``cli``) and the dashboard
(actor ``dashboard:<user>``). Every action writes one ``control.audit`` row with the actor, in
the same transaction as the change (``general_approve`` writes its row first, on purpose).

Refusals raise ``InvalidInput`` with a category, never an exception message from below. A token is
returned once by ``create_token``; the audit holds its id only.
"""

import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

from psycopg import sql
from psycopg.types.json import Jsonb

from treant import gate, gate_rules, graph, jobs, names, sessions, settings, tokens
from treant.db import Database
from treant.errors import InvalidInput
from treant.scrub import scrub

MAX_MISTAKE = 2000
MAX_FACT_TEXT = 20_000
_TOP_KEYS = ("backend", "alias", "judge_alias", "publish")
_RULE_LISTS = ("domains", "ticket_prefixes", "terms")
_RETRY_SET = (
    "SET status = 'queued', attempts = 0, run_after = now(), lease_id = NULL, lease_until = NULL "
)


class Refused(InvalidInput):
    """A refusal the CLI reports as ``error: <category>`` with exit code 1 (not found, not open)."""


class Flagged(Refused):
    """The gate rules found something in the text and the owner did not force it (``flagged``)."""


def _ws(ws: str) -> str:
    """A validated workspace name; bad input is ``InvalidInput`` (a refusal), not a bare error."""
    try:
        return names.validate_workspace(ws)
    except ValueError:
        raise InvalidInput("invalid workspace name") from None


def check_mistake_text(text: Any) -> None:
    """A mistake note is 1 to ``MAX_MISTAKE`` characters; shared with the ``report_mistake`` tool."""
    if not isinstance(text, str) or not 1 <= len(text) <= MAX_MISTAKE:
        raise InvalidInput("text length")


def audit(conn: Any, actor: str, action: str, detail: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO control.audit (actor, action, detail) VALUES (%s, %s, %s)",
        (actor, action, Jsonb(detail)),
    )


def set_workspace_settings(
    db: Database, actor: str, ws: str, changes: dict[str, Any], reset: bool = False
) -> list[str]:
    """Apply ``changes`` (``settings.dump`` shape: top-level keys and ``rules.<list>``) to a
    workspace's settings; returns the changed keys, validated before anything is written."""
    _ws(ws)
    allowed = {*_TOP_KEYS, *(f"rules.{k}" for k in _RULE_LISTS)}
    if set(changes) - allowed:
        raise InvalidInput("unknown setting key")
    with db.admin_tx() as conn:
        row = conn.execute(
            "SELECT settings, status FROM control.workspaces WHERE name = %s FOR UPDATE", (ws,)
        ).fetchone()
        if row is None or row[1] != "active":
            raise Refused("workspace not available")
        if reset:
            new = settings.dump(settings.WorkspaceSettings())
        else:
            try:
                new = settings.dump(settings.parse(row[0] or {}))
            except InvalidInput:
                raise Refused("stored settings are invalid; use --reset") from None
        changed: list[str] = []
        for key in _TOP_KEYS:
            if key in changes:
                new[key] = changes[key]
                changed.append(key)
        for key in _RULE_LISTS:
            if f"rules.{key}" in changes:
                new["rules"][key] = list(changes[f"rules.{key}"])
                changed.append(key)
        settings.parse(new)  # validate before writing
        conn.execute(
            "UPDATE control.workspaces SET settings = %s WHERE name = %s", (Jsonb(new), ws)
        )
        audit(
            conn,
            actor,
            "workspace.settings",
            {"workspace": ws, "changed": changed, "reset": bool(reset)},
        )
    return changed


def archive_workspace(db: Database, actor: str, ws: str) -> int:
    """Archive an active workspace and revoke its live tokens; returns how many."""
    _ws(ws)
    with db.control_tx() as conn:
        row = conn.execute(
            "UPDATE control.workspaces SET status = 'archived' "
            "WHERE name = %s AND status = 'active' RETURNING name",
            (ws,),
        ).fetchone()
        if row is None:
            raise Refused("workspace not available")
        revoked = conn.execute(
            "UPDATE control.tokens SET revoked_at = now() "
            "WHERE workspace = %s AND revoked_at IS NULL",
            (ws,),
        ).rowcount
        audit(conn, actor, "workspace.archive", {"workspace": ws, "tokens_revoked": revoked})
    return revoked


def create_token(db: Database, actor: str, ws: str, label: str, actions: set[str]) -> str:
    """Create a token; the full string is returned once, the audit holds its id only."""
    with db.control_tx() as conn:
        try:
            token = tokens.create_token_in(conn, ws, label, actions)
        except ValueError as e:
            raise InvalidInput(str(e)) from None
        audit(
            conn,
            actor,
            "token.create",
            {
                "token_id": token.split("_")[1],
                "workspace": ws,
                "label": label,
                "actions": sorted(actions),
            },
        )
    return token


def revoke_token(db: Database, actor: str, token_id: str) -> None:
    with db.control_tx() as conn:
        cur = conn.execute(
            "UPDATE control.tokens SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL",
            (token_id,),
        )
        if cur.rowcount != 1:
            raise Refused("token not found")
        audit(conn, actor, "token.revoke", {"token_id": token_id})


def _require_active(conn: Any, ws: str) -> None:
    """Lock an active workspace's row for the transaction; archived or absent is refused."""
    row = conn.execute(
        "SELECT status FROM control.workspaces WHERE name = %s FOR SHARE", (ws,)
    ).fetchone()
    if row is None or row[0] != "active":
        raise Refused("workspace not available")


def _target_value(kind: str, value: str) -> str:
    # The guard compares git hosts lower-cased (host names are case-insensitive).
    return value.lower() if kind == "git_host" and isinstance(value, str) else value


def add_target(db: Database, actor: str, ws: str, kind: str, value: str) -> None:
    schema = sql.Identifier(names.ws_schema(_ws(ws)))
    value = _target_value(kind, value)
    with db.admin_tx() as conn:
        _require_active(conn, ws)
        conn.execute(
            sql.SQL(
                "INSERT INTO {}.targets (kind, value) VALUES (%s, %s) ON CONFLICT DO NOTHING"
            ).format(schema),
            (kind, value),
        )
        audit(conn, actor, "target.add", {"workspace": ws, "kind": kind, "value": value})


def remove_target(db: Database, actor: str, ws: str, kind: str, value: str) -> None:
    schema = sql.Identifier(names.ws_schema(_ws(ws)))
    value = _target_value(kind, value)
    with db.admin_tx() as conn:
        _require_active(conn, ws)
        gone = conn.execute(
            sql.SQL("DELETE FROM {}.targets WHERE kind = %s AND value = %s").format(schema),
            (kind, value),
        ).rowcount
        if not gone:
            raise Refused("no such target")
        audit(conn, actor, "target.remove", {"workspace": ws, "kind": kind, "value": value})


def release_quarantine(db: Database, actor: str, ws: str, revision_id: str) -> None:
    sessions.release(db, _ws(ws), revision_id, actor=actor)


def drop_quarantine(db: Database, actor: str, ws: str, revision_id: str) -> None:
    sessions.drop(db, _ws(ws), revision_id, actor=actor)


def _queue_row(db: Database, queue_id: int) -> tuple | None:
    with db.admin_tx() as conn:
        return conn.execute(
            "SELECT workspace, item, status FROM control.general_queue WHERE id = %s",
            (queue_id,),
        ).fetchone()


def _approval_plan(
    db: Database, queue_id: int, text: str | None
) -> tuple[str, list[gate_rules.Finding]]:
    row = _queue_row(db, queue_id)
    if row is None or row[2] not in gate.OPEN:
        raise Refused("queue item not found or not open")
    ws, item = row[0], dict(row[1])
    if text is not None:
        if item["kind"] == "concept":
            raise Refused("a concept takes no text override")
        item["text"] = text
    try:
        deny = gate_rules.load_deny(db)
    except InvalidInput:
        raise Refused("deny list unavailable") from None
    return ws, gate_rules.check(gate._checked_text(item), deny)  # on the final text


def check_approval(db: Database, queue_id: int, text: str | None) -> list[gate_rules.Finding]:
    """The rule findings an approval of this item (with this text) would override; no write."""
    return _approval_plan(db, queue_id, text)[1]


def general_approve(db: Database, actor: str, queue_id: int, text: str | None, force: bool) -> None:
    """Publish an open queue item over the gate, as the owner's final decision. Raises
    ``Flagged`` (nothing written) when the rules find something and not ``force``."""
    ws, findings = _approval_plan(db, queue_id, text)
    if findings and not force:
        raise Flagged("flagged")
    detail = {"queue_id": queue_id, "workspace": ws, "edited": text is not None}
    with db.admin_tx() as conn:  # first: an approval that skips the judges always leaves a trace
        audit(conn, actor, "general.approve", detail)
    try:
        gate.publish(db, queue_id, text, actor="owner")
    except InvalidInput:
        with db.admin_tx() as conn:
            audit(conn, actor, "general.approve_failed", detail)
        raise


def general_drop(db: Database, actor: str, queue_id: int) -> None:
    with db.admin_tx() as conn:
        row = conn.execute(
            "UPDATE control.general_queue SET status = 'dropped', decided_at = now() "
            "WHERE id = %s AND status NOT IN ('published', 'dropped') RETURNING workspace",
            (queue_id,),
        ).fetchone()
        if row is None:
            raise Refused("queue item not found or already decided")
        audit(conn, actor, "general.drop", {"queue_id": queue_id, "workspace": row[0]})


def retry_jobs(
    db: Database,
    actor: str,
    job_id: int | None = None,
    status: str | None = None,
    kind: str | None = None,
    ws: str | None = None,
) -> list[int]:
    """Re-queue one job (any status but running) or every job in ``status`` matching the
    filters. The error category stays until the next run. A gate job whose queue row was
    decided meanwhile simply runs as a no-op (``stale``)."""
    if job_id is None and status is None:
        raise InvalidInput("give a job id or --status dead")
    with db.admin_tx() as conn:
        if job_id is not None:
            rows = conn.execute(
                "UPDATE control.jobs " + _RETRY_SET + "WHERE id = %s AND status <> 'running' "
                "RETURNING id",
                (job_id,),
            ).fetchall()
            if not rows:
                raise Refused("job not found or running")
        else:
            rows = conn.execute(
                "UPDATE control.jobs " + _RETRY_SET + "WHERE status = %(s)s "
                "AND (%(k)s::text IS NULL OR kind = %(k)s) "
                "AND (%(w)s::text IS NULL OR workspace = %(w)s) RETURNING id",
                {"s": status, "k": kind, "w": ws},
            ).fetchall()
        ids = sorted(r[0] for r in rows)
        audit(conn, actor, "jobs.retry", {"ids": ids, "count": len(ids)})
    return ids


def log_mistake(db: Database, actor: str, ws: str, text: str) -> None:
    """Record a mistake note for a workspace (1 to 2000 chars), as ``report_mistake`` does."""
    check_mistake_text(text)
    _ws(ws)
    with db.control_tx() as conn:
        if (
            conn.execute("SELECT 1 FROM control.workspaces WHERE name = %s", (ws,)).fetchone()
            is None
        ):
            raise Refused("workspace not available")
        conn.execute("INSERT INTO control.mistakes (workspace, text) VALUES (%s, %s)", (ws, text))
        audit(conn, actor, "mistake.log", {"workspace": ws, "length": len(text)})


# --- facts, merges and dismissals in the graph ----------------------------------------------
# These run as the admin role (the owner of every schema), so the change and its audit row share
# one transaction: neither the workspace role nor the publisher role may insert into control.audit.


def _check_ns(ns: str) -> None:
    if ns != "general":
        try:
            names.validate_workspace(ns)
        except ValueError:
            raise InvalidInput("unknown namespace") from None


def _require_registered(db: Database, ws: str) -> None:
    with db.admin_tx() as conn:
        if (
            conn.execute("SELECT 1 FROM control.workspaces WHERE name = %s", (ws,)).fetchone()
            is None
        ):
            raise Refused("workspace not available")


def _owner_text(db: Database, ns: str, text: Any, force: bool) -> str:
    """Fact text the owner writes: workspace text passes the scrubber (unsure:
    ``InvalidInput("text")``) and comes back cleaned; general text passes the gate rules and
    raises ``Flagged`` unless ``force``."""
    cap = gate.MAX_TEXT if ns == "general" else MAX_FACT_TEXT
    if not isinstance(text, str) or not text.strip() or len(text) > cap:
        raise InvalidInput("text")
    if ns == "general":
        if gate_rules.check(text, _general_deny(db)) and not force:
            raise Flagged("flagged")
        return text
    _require_registered(db, ns)  # "workspace not available" before reading settings
    cleaned = scrub(text, settings.load(db, ns).terms)
    if not cleaned.confident:
        raise InvalidInput("text")
    return cleaned.text


@contextmanager
def _graph_unit(db: Database, ns: str) -> Iterator[Any]:
    """One admin transaction over a namespace; graph "not found" refusals become ``Refused``."""
    _check_ns(ns)
    try:
        with db.admin_tx() as conn:
            if (
                ns != "general"
                and conn.execute(
                    "SELECT 1 FROM control.workspaces WHERE name = %s", (ns,)
                ).fetchone()
                is None
            ):
                raise Refused("workspace not available")
            yield conn
    except Refused:
        raise
    except InvalidInput as e:
        if str(e).endswith("not found") or str(e) == "node not current":
            raise Refused(str(e)) from None
        raise


def edit_fact(
    db: Database,
    actor: str,
    ns: str,
    fact_id: str,
    text: str | None,
    status: str | None,
    force: bool = False,
) -> int:
    """Edit a fact's text and/or status as the owner (any status, no "final" refusal); returns the
    new version. Workspace text passes the scrubber (unsure: ``InvalidInput("text")``); general
    text passes the gate rules and raises ``Flagged`` unless ``force``. A workspace edit enqueues
    the embed job; a general one leaves the re-embedding to ``worker.embed_general``."""
    _check_ns(ns)
    if text is None and status is None:
        raise InvalidInput("nothing to change")
    new_text = None if text is None else _owner_text(db, ns, text, force)
    with _graph_unit(db, ns) as conn:
        version = graph.update_fact(conn, ns, fact_id, new_text, status)
        if new_text is not None and ns != "general":
            jobs.enqueue(conn, ns, "embed", f"fact:{fact_id}:{version}", {"revision_id": None})
        audit(conn, actor, "fact.edit", {"ns": ns, "fact_id": str(fact_id), "version": version})
    return version


def retract_fact(db: Database, actor: str, ns: str, fact_id: str) -> None:
    with _graph_unit(db, ns) as conn:
        graph.retract(conn, ns, "fact", fact_id)
        audit(conn, actor, "fact.retract", {"ns": ns, "fact_id": str(fact_id)})


def _general_deny(db: Database) -> Any:
    try:
        return gate_rules.load_deny(db)
    except InvalidInput:
        raise Refused("deny list unavailable") from None


def restore_fact(
    db: Database, actor: str, ns: str, fact_id: str, version: int, force: bool = False
) -> int:
    """Put a fact's text and status back to an earlier version; returns the new version. A
    ``general`` restore re-publishes that text, so it passes the gate rules first and raises
    ``Flagged`` (nothing written) unless ``force``."""
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise InvalidInput("version")
    deny = _general_deny(db) if ns == "general" else None
    with _graph_unit(db, ns) as conn:
        new = graph.restore_fact(conn, ns, fact_id, version)
        if deny is not None:
            restored = conn.execute(
                "SELECT text FROM general.facts WHERE id = %s", (str(fact_id),)
            ).fetchone()
            if gate_rules.check(restored[0], deny) and not force:
                raise Flagged("flagged")  # rolls the restore back
        detail = {"ns": ns, "fact_id": str(fact_id), "version": version}
        audit(conn, actor, "fact.restore", detail)
        if ns != "general":
            jobs.enqueue(conn, ns, "embed", f"fact:{fact_id}:{new}", {"revision_id": None})
    return new


def _fact_id(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise InvalidInput("invalid id") from None


def merge_facts(
    db: Database,
    actor: str,
    ns: str,
    keep_id: str,
    drop_ids: Iterable[str],
    text: str | None = None,
) -> None:
    """Fold duplicate facts into ``keep_id``, in one transaction. Every id must be a current fact
    of ``ns`` and all of one type. ``text`` (optional) first edits the kept fact like
    ``edit_fact`` (scrubbed, or for ``general`` checked by the gate rules: ``Flagged``; an
    unchanged text is no edit). Each dropped fact is snapshotted (reason ``merge``) and set
    ``superseded``; in a workspace its ``support`` rows are copied to the kept fact. One audit
    row ``fact.merge`` carries the ids, never text."""
    _check_ns(ns)
    keep = _fact_id(keep_id)
    drops = sorted({_fact_id(d) for d in drop_ids})
    if not drops:
        raise InvalidInput("nothing to merge")
    if keep in drops:
        raise InvalidInput("keep is also dropped")
    new_text = None if text is None else _owner_text(db, ns, text, False)
    schema = graph.schema_of(ns)
    with _graph_unit(db, ns) as conn:
        rows = conn.execute(
            sql.SQL(
                "SELECT id::text, type, status, text FROM {}.facts WHERE id = ANY(%s::uuid[]) "
                "ORDER BY id FOR UPDATE"
            ).format(schema),
            ([keep, *drops],),
        ).fetchall()
        if len(rows) != len(drops) + 1:
            raise Refused("fact not found")
        if any(r[2] != "current" for r in rows):
            raise Refused("fact not current")
        if len({r[1] for r in rows}) != 1:
            raise InvalidInput("mixed fact types")
        kept_text = next(r[3] for r in rows if r[0] == keep)
        edited = new_text is not None and new_text != kept_text
        if edited:
            version = graph.update_fact(conn, ns, keep, new_text, None)
            if ns != "general":
                jobs.enqueue(conn, ns, "embed", f"fact:{keep}:{version}", {"revision_id": None})
        for drop in drops:
            graph.set_status(conn, ns, "fact", drop, "superseded", "merge")
        if ns != "general":
            conn.execute(
                sql.SQL(
                    "INSERT INTO {}.support (item_kind, item_id, revision_id) "
                    "SELECT 'fact', %s, revision_id FROM {}.support "
                    "WHERE item_kind = 'fact' AND item_id = ANY(%s::uuid[]) ON CONFLICT DO NOTHING"
                ).format(schema, schema),
                (keep, drops),
            )
        audit(
            conn,
            actor,
            "fact.merge",
            {"ns": ns, "keep": keep, "dropped": drops, "edited": edited},
        )


def _resolve(conn: Any, ws: str, table: str, row_id: int, resolution: str, cols: str) -> tuple:
    row = conn.execute(
        sql.SQL(
            "UPDATE {}.{} SET resolved_at = now(), resolution = %s "
            "WHERE id = %s AND resolved_at IS NULL RETURNING " + cols
        ).format(sql.Identifier(names.ws_schema(ws)), sql.Identifier(table)),
        (resolution, row_id),
    ).fetchone()
    if row is None:
        label = "merge flag" if table == "merge_flags" else "conflict"
        raise Refused(f"{label} not found or resolved")
    return row


def merge(db: Database, actor: str, ws: str, flag_id: int) -> None:
    """Merge a flagged pair (``node_b`` into ``node_a``) and mark the flag ``merged``."""
    if ws == "general":
        raise InvalidInput("unknown namespace")
    with _graph_unit(db, ws) as conn:
        keep, gone = _resolve(conn, ws, "merge_flags", flag_id, "merged", "node_a, node_b")
        if keep is None or gone is None:
            raise Refused("node not found")
        graph.merge_nodes(conn, ws, str(keep), str(gone))
        # Other open flags on the node that just went away can no longer be acted on.
        also = [
            r[0]
            for r in conn.execute(
                sql.SQL(
                    "UPDATE {}.merge_flags SET resolved_at = now(), resolution = 'superseded' "
                    "WHERE resolved_at IS NULL AND (node_a = %s OR node_b = %s) RETURNING id"
                ).format(sql.Identifier(names.ws_schema(ws))),
                (gone, gone),
            )
        ]
        audit(
            conn,
            actor,
            "merge.merge",
            {
                "workspace": ws,
                "flag_id": flag_id,
                "keep": str(keep),
                "gone": str(gone),
                "also_resolved": sorted(also),
            },
        )


def dismiss_merge(db: Database, actor: str, ws: str, flag_id: int) -> None:
    if ws == "general":
        raise InvalidInput("unknown namespace")
    with _graph_unit(db, ws) as conn:
        _resolve(conn, ws, "merge_flags", flag_id, "dismissed", "id")
        audit(conn, actor, "merge.dismiss", {"workspace": ws, "flag_id": flag_id})


def dismiss_conflict(db: Database, actor: str, ws: str, conflict_id: int) -> None:
    if ws == "general":
        raise InvalidInput("unknown namespace")
    with _graph_unit(db, ws) as conn:
        _resolve(conn, ws, "conflicts", conflict_id, "dismissed", "id")
        audit(conn, actor, "conflict.dismiss", {"workspace": ws, "conflict_id": conflict_id})
