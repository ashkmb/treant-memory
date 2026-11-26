"""Canaries: planted marker items per workspace, and the cross-workspace isolation check.

``plant`` writes an instance node, a ``warning`` fact on it and a ``fact`` carrying the marker
into one workspace. ``check`` then tries, as every active workspace in turn, to reach every
other workspace's markers through each read path. A returned fault is a triple
``(check_name, from_ws, to_ws)``:

* ``search``, ``brief``, ``guard``, ``walk``: workspace ``from_ws`` saw ``to_ws``'s marker or item;
* ``repo_facts``: workspace ``from_ws`` got a fact back from ``stale.repo_facts`` for a repo
  named like one of ``to_ws``'s markers;
* ``flag_stale``: ``stale.load_fact``, as ``from_ws``, found one of ``to_ws``'s canary facts;
* ``direct_select``: ``from_ws``'s role could read ``ws_<to_ws>.facts`` or ``.stale_flags``;
* ``general``: a marker of workspace ``to_ws`` sits in a ``general`` fact, node name or alias,
  or in a *published* ``general_queue`` item (markers in other queue rows are the gate
  working; see ``queue_held``)
  (``from_ws`` is the literal ``"general"``);
* ``error:<check>``: a check failed unexpectedly, so isolation could not be confirmed
  (``error:setup`` when even the workspace or canary list could not be read).

Marker text is never logged.
"""

import logging
import secrets
import uuid
from collections.abc import Callable
from functools import partial

import psycopg
from psycopg import sql

from treant import graph, jobs, names, stale
from treant.brief import CANARY_NODE_TYPE, brief
from treant.db import Database
from treant.embed import Embedder
from treant.errors import NotFound
from treant.guard import GuardEnv, guard_check
from treant.search import search
from treant.tokens import TokenContext
from treant.walk import walk
from treant.worker import run_once

log = logging.getLogger(__name__)

Fault = tuple[str, str, str]

_MAX_EMBED_RUNS = 50


def plant(db: Database, embedder: Embedder, workspace: str) -> str:
    """Plant canaries in ``workspace`` and return the marker (also the instance node's name)."""
    names.validate_workspace(workspace)
    marker = f"canary-{secrets.token_hex(8)}"
    with db.ws_tx(workspace) as conn:
        node = graph.add_node(conn, workspace, "instance", CANARY_NODE_TYPE, marker)
        warning = graph.add_fact(conn, workspace, "warning", f"canary warning {marker}", node)
        fact = graph.add_fact(conn, workspace, "fact", f"canary fact {marker}", node)
        jobs.enqueue(conn, workspace, "embed", f"canary-{node}", {"revision_id": str(uuid.uuid4())})
    with db.control_tx() as conn:
        for kind, item_id in (("node", node), ("fact", warning), ("fact", fact)):
            conn.execute(
                "INSERT INTO control.canaries (workspace, marker, item_kind, item_id) "
                "VALUES (%s, %s, %s, %s)",
                (workspace, marker, kind, item_id),
            )
    for _ in range(_MAX_EMBED_RUNS):
        if not run_once(db, embedder):
            break
    log.info("canary planted")
    return marker


def counts(db: Database) -> list[tuple[str, int]]:
    """(workspace, canary marker count) for every active workspace; markers are not returned."""
    with db.control_tx() as conn:
        rows = conn.execute(
            "SELECT w.name, count(DISTINCT c.marker) FROM control.workspaces w "
            "LEFT JOIN control.canaries c ON c.workspace = w.name "
            "WHERE w.status = 'active' GROUP BY w.name ORDER BY w.name"
        ).fetchall()
    return [(name, n) for name, n in rows]


def has_canary(db: Database, workspace: str) -> bool:
    """Whether ``workspace`` (active or archived) has a planted canary."""
    with db.control_tx() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM control.canaries WHERE workspace = %s LIMIT 1", (workspace,)
            ).fetchone()
            is not None
        )


def _active(db: Database) -> list[str]:
    with db.control_tx() as conn:
        rows = conn.execute(
            "SELECT name FROM control.workspaces WHERE status = 'active' ORDER BY name"
        ).fetchall()
    return [r[0] for r in rows]


def _canaries(db: Database, workspaces: list[str] | None) -> dict[str, tuple[list[str], set[str]]]:
    """workspace -> (markers, canary item ids), for ``workspaces`` or (None) every workspace
    with canaries, archived ones included. The marker doubles as the instance node name."""
    out: dict[str, tuple[list[str], set[str]]] = {}
    with db.control_tx() as conn:
        rows = conn.execute(
            "SELECT workspace, marker, item_id::text FROM control.canaries "
            "WHERE %(all)s OR workspace = ANY(%(ws)s) ORDER BY workspace, marker",
            {"all": workspaces is None, "ws": workspaces or []},
        ).fetchall()
    for ws, marker, item_id in rows:
        markers, ids = out.setdefault(ws, ([], set()))
        if marker not in markers:
            markers.append(marker)
        ids.add(item_id)
    return out


def _check_search(db, embedder, ctx, marker, ids) -> bool:
    result = search(db, embedder, ctx, marker)
    return any(marker in str(h.get("text", "")) or h.get("id") in ids for h in result.hits)


def _check_brief(db, ctx, marker, name) -> bool:
    text = brief(db, ctx, name)
    return marker in text or name in text


def _check_guard(db, ctx, marker, name) -> bool:
    env = GuardEnv(cwd="/", kube_context=None, aws_profile=None)
    result = guard_check(db, ctx, f"kubectl --context {name} get po", env)
    return any(marker in w for w in result.get("warnings", []))


def _check_walk(db, ctx, ids) -> bool:
    schema = sql.Identifier(names.ws_schema(ctx.workspace))
    with db.ws_tx(ctx.workspace) as conn:
        rows = conn.execute(
            sql.SQL("SELECT id::text FROM {}.nodes WHERE status = 'current'").format(schema)
        ).fetchall()
        reached = walk(conn, ctx.workspace, [("ws", r[0]) for r in rows], hops=2)
    return any(ref[1] in ids for ref in reached)


def _check_repo_facts(db, ctx, name) -> bool:
    with db.ws_tx(ctx.workspace) as conn:
        facts, _ = stale.repo_facts(conn, ctx.workspace, name)
    return bool(facts)


def _check_flag_stale(db, ctx, ids) -> bool:
    """True when ``stale.load_fact`` (read-only here: a row lock, no write) finds any of ``ids``
    in ``ctx.workspace``. An absent fact is the expected, isolated answer."""
    with db.ws_tx(ctx.workspace) as conn:
        for item_id in sorted(ids):
            try:
                stale.load_fact(conn, ctx.workspace, item_id)
            except NotFound:
                continue
            return True
    return False


def _check_direct(db, a: str, b: str) -> bool:
    """True on a fault: ``a``'s role could read ``ws_<b>.facts`` or ``ws_<b>.stale_flags``.

    A read of ``a``'s own schema comes first and is not guarded: if the role switch or the
    schema is broken that raises (reported as ``error:direct_select``) instead of passing as
    "access denied".
    """
    own = sql.Identifier(names.ws_schema(a))
    other = sql.Identifier(names.ws_schema(b))
    for table in ("facts", "stale_flags"):
        read = sql.SQL("SELECT 1 FROM {}.{} LIMIT 1")
        with db.ws_tx(a) as conn:
            conn.execute(read.format(own, sql.Identifier(table))).fetchall()
        try:
            with db.ws_tx(a) as conn:
                conn.execute(read.format(other, sql.Identifier(table))).fetchall()
        except psycopg.errors.InsufficientPrivilege:
            continue
        return True
    return False


def _embedded(db: Database, ws: str, ids: set[str]) -> bool:
    """True when every canary item of ``ws`` has a vector."""
    schema = sql.Identifier(names.ws_schema(ws))
    with db.ws_tx(ws) as conn:
        n = conn.execute(
            sql.SQL(
                "SELECT count(DISTINCT item_id) FROM {}.embeddings WHERE item_id = ANY(%s::uuid[])"
            ).format(schema),
            (sorted(ids),),
        ).fetchone()[0]
    return n == len(ids)


def stats(db: Database) -> tuple[int, int, int]:
    """(active workspaces, ordered pairs, workspaces holding canaries)."""
    workspaces = _active(db)
    planted = _canaries(db, workspaces)
    n = len(workspaces)
    return n, n * (n - 1), len(planted)


def _check_general(db: Database, markers: dict[str, list[str]]) -> list[Fault]:
    faults: list[Fault] = []
    with db.publisher_tx() as conn:
        texts = [r[0] for r in conn.execute("SELECT text FROM general.facts").fetchall()]
        texts += [
            " ".join([r[0], *r[1]])
            for r in conn.execute("SELECT name, aliases FROM general.nodes").fetchall()
        ]
    with db.control_tx() as conn:
        texts += [
            " ".join(
                [r[0] or "", r[1] or "", *(r[2] or [])]  # fact text, concept name, aliases
            )
            for r in conn.execute(
                "SELECT item->>'text', item->>'name', "
                "ARRAY(SELECT jsonb_array_elements_text(coalesce(item->'aliases', '[]'))) "
                "FROM control.general_queue WHERE status = 'published'"
            ).fetchall()
        ]
    for ws, ws_markers in sorted(markers.items()):
        if any(m in t for m in ws_markers for t in texts):
            faults.append(("general", "general", ws))
    return faults


def queue_held(db: Database) -> int:
    """Count of unpublished queue rows (blocked, pending, ...) holding a canary marker: the gate
    doing its job, reported as info and never a fault."""
    with db.control_tx() as conn:
        markers = [r[0] for r in conn.execute("SELECT DISTINCT marker FROM control.canaries")]
        if not markers:
            return 0
        return conn.execute(
            "SELECT count(*) FROM control.general_queue WHERE status <> 'published' "
            "AND EXISTS (SELECT 1 FROM unnest(%s::text[]) m WHERE position(m in item::text) > 0)",
            (markers,),
        ).fetchone()[0]


def _pair(
    db: Database,
    embedder: Embedder,
    a: str,
    b: str,
    planted: tuple[list[str], set[str]] | None,
) -> list[Fault]:
    ctx = TokenContext(token_id="canary", workspace=a, actions=frozenset({"read"}))
    checks: list[tuple[str, Callable[[], bool]]] = []
    if planted is not None:
        markers, ids = planted
        checks = [
            ("search", partial(_any, markers, lambda m: _check_search(db, embedder, ctx, m, ids))),
            ("brief", partial(_any, markers, lambda m: _check_brief(db, ctx, m, m))),
            ("guard", partial(_any, markers, lambda m: _check_guard(db, ctx, m, m))),
            ("walk", lambda: _check_walk(db, ctx, ids)),
            ("repo_facts", partial(_any, markers, lambda m: _check_repo_facts(db, ctx, m))),
            ("flag_stale", lambda: _check_flag_stale(db, ctx, ids)),
        ]
    checks.append(("direct_select", lambda: _check_direct(db, a, b)))
    faults: list[Fault] = []
    for label, fn in checks:
        try:
            leaked = fn()
        except Exception as e:  # noqa: BLE001 - recorded as a fault, by type only
            log.error("isolation check errored check=%s error=%s", label, type(e).__name__)
            faults.append((f"error:{label}", a, b))
            continue
        if leaked:
            faults.append((label, a, b))
    return faults


def _any(markers: list[str], fn: Callable[[str], bool]) -> bool:
    return any(fn(m) for m in markers)


def check(db: Database, embedder: Embedder) -> list[Fault]:
    """Run every isolation check; returns the faults found (empty when clean).

    A clean result must mean something was measured, so missing coverage is itself a fault:
    ``("coverage", "-", "-")`` with fewer than two active workspaces, ``("no_canary", ws, ws)``
    for an active workspace with no planted canary, ``("not_embedded", ws, ws)`` when a
    canary's items lack vectors (the search check would then prove nothing).
    """
    try:
        workspaces = _active(db)
        planted = _canaries(db, workspaces)
        every = _canaries(db, None)  # archived workspaces' markers must not reach general either
    except Exception as e:  # noqa: BLE001 - nothing could be measured: one fault, by type only
        log.error("isolation check errored check=setup error=%s", type(e).__name__)
        return [("error:setup", "-", "-")]
    faults: list[Fault] = []
    if len(workspaces) < 2:
        faults.append(("coverage", "-", "-"))
    for ws in workspaces:
        if ws not in planted:
            faults.append(("no_canary", ws, ws))
            continue
        try:
            if not _embedded(db, ws, planted[ws][1]):
                faults.append(("not_embedded", ws, ws))
        except Exception as e:  # noqa: BLE001
            log.error("isolation check errored check=embedded error=%s", type(e).__name__)
            faults.append(("error:embedded", ws, ws))
    for a in workspaces:
        for b in workspaces:
            if a != b:
                faults += _pair(db, embedder, a, b, planted.get(b))
    try:
        faults += _check_general(db, {w: m for w, (m, _) in every.items()})
    except Exception as e:  # noqa: BLE001
        log.error("isolation check errored check=general error=%s", type(e).__name__)
        faults.append(("error:general", "general", "general"))
    pairs = len(workspaces) * (len(workspaces) - 1)
    log.info("isolation check pairs=%d faults=%d", pairs, len(faults))
    return faults
