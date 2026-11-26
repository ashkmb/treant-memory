"""Owner CLI. Every owner action writes ``control.audit`` with actor ``cli``.

Output rules: a token is printed once, by ``token create``, and nothing else secret is ever
printed; errors are a category, never an exception message.
"""

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Mapping
from functools import partial
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from treant import (
    apply,
    browse,
    canary,
    distill,
    gate,
    jobs,
    migrate,
    names,
    owner,
    restore,
    sessions,
    settings,
    tokens,
    worker,
)
from treant.backends import BackendBadReply, BackendUnavailable
from treant.config import Settings
from treant.db import Database
from treant.embed import Embedder, EmbedderUnavailable, HttpEmbedder
from treant.errors import InvalidInput

log = logging.getLogger("treant.cli")

TARGET_KINDS = names.TARGET_KINDS
IDLE_SLEEP_S = 5
ACTOR = "cli"

# Each command reads only the settings it needs; the rest are filled with an unused value so
# that `bootstrap` works before an embedder or a login DSN exist.
_ADMIN = ("TREANT_ADMIN_DATABASE_URL",)
_DB = ("TREANT_DATABASE_URL",)
_EMBED = ("TREANT_DATABASE_URL", "TREANT_EMBED_URL")


def _settings(env: Mapping[str, str], needs: tuple[str, ...]) -> Settings:
    filled = {k: env.get(k) or "unused" for k in (*_ADMIN, *_EMBED)}
    for name in needs:
        if not env.get(name):
            raise ValueError(f"missing required setting {name}")
        filled[name] = env[name]
    return Settings.from_env({**env, **filled})


def _embedder(s: Settings) -> HttpEmbedder:
    return HttpEmbedder(s.embed_url, s.embed_model, s.embed_dims)


def _audit_admin(admin_dsn: str, action: str, detail: dict[str, Any]) -> None:
    with psycopg.connect(admin_dsn) as conn:
        conn.execute(
            "INSERT INTO control.audit (actor, action, detail) VALUES ('cli', %s, %s)",
            (action, Jsonb(detail)),
        )


def _cmd_bootstrap(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    password = env.get("TREANT_LOGIN_PASSWORD")
    if not password:
        print("error: missing required setting TREANT_LOGIN_PASSWORD", file=sys.stderr)
        return 2
    s = _settings(env, _ADMIN)
    migrate.bootstrap(s.admin_database_url, s.role_prefix, password)
    _audit_admin(s.admin_database_url, "bootstrap", {})
    print("bootstrap complete")
    return 0


def _cmd_upgrade(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    s = _settings(env, _ADMIN)
    upgraded = migrate.upgrade(s.admin_database_url, s.role_prefix)
    _audit_admin(s.admin_database_url, "upgrade", {"workspaces": len(upgraded)})
    print(f"upgrade complete ({len(upgraded)} workspaces)")
    return 0


_LISTS = (("domain", "domains"), ("ticket_prefix", "ticket_prefixes"), ("term", "terms"))


def _workspace_set(db: Database, args: argparse.Namespace) -> int:
    changes: dict[str, Any] = {}
    for key in ("backend", "alias", "judge_alias", "publish"):
        if getattr(args, key) is not None:
            changes[key] = getattr(args, key)
    for opt, key in _LISTS:
        if getattr(args, f"clear_{key}"):
            changes[f"rules.{key}"] = []
        if getattr(args, opt):
            changes[f"rules.{key}"] = list(getattr(args, opt))
    try:
        owner.set_workspace_settings(db, ACTOR, args.name, changes, reset=bool(args.reset))
    except owner.Refused as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"workspace {args.name} settings updated")
    return 0


def _cmd_workspace(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    if args.sub == "create":
        s = _settings(env, _ADMIN)
        migrate.create_workspace(s.admin_database_url, s.role_prefix, args.name)
        _audit_admin(s.admin_database_url, "workspace.create", {"workspace": args.name})
        print(f"workspace {args.name} created")
        return 0
    if args.sub == "purge":
        s = _settings(env, _ADMIN)
        if args.confirm != args.name:
            print("error: --confirm must repeat the workspace name", file=sys.stderr)
            return 1
        try:
            migrate.purge_workspace(s.admin_database_url, s.role_prefix, args.name)
        except ValueError:
            print("error: workspace not available", file=sys.stderr)
            return 1
        except psycopg.errors.LockNotAvailable:
            print("error: workspace busy (lock timeout), nothing purged; retry", file=sys.stderr)
            return 1
        print(f"purged {args.name}")
        return 0
    if args.sub == "purge-verify":
        names.validate_workspace(args.name)
        s = _settings(env, _ADMIN)
        with psycopg.connect(s.admin_database_url) as conn:
            purged = conn.execute(
                "SELECT 1 FROM control.audit WHERE action = 'workspace.purge' "
                "AND detail->>'workspace' = %s LIMIT 1",
                (args.name,),
            ).fetchone()
            if purged is None:
                print("error: no purge recorded for this workspace", file=sys.stderr)
                return 1
            schema_gone = (
                conn.execute(
                    "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
                    (names.ws_schema(args.name),),
                ).fetchone()
                is None
            )
            role_gone = (
                conn.execute(
                    "SELECT 1 FROM pg_roles WHERE rolname = %s",
                    (names.ws_role(s.role_prefix, args.name),),
                ).fetchone()
                is None
            )
        repo_gone = args.repo_gone == "yes"
        _audit_admin(
            s.admin_database_url,
            "workspace.purge.verified",
            {
                "workspace": args.name,
                "schema_gone": schema_gone,
                "role_gone": role_gone,
                "repo_gone": repo_gone,
            },
        )
        print(f"schema_gone={schema_gone} role_gone={role_gone} repo_gone={repo_gone}")
        return 0 if schema_gone and role_gone and repo_gone else 1
    s = _settings(env, _DB)
    with Database(s.database_url, s.role_prefix) as db:
        if args.sub == "set":
            return _workspace_set(db, args)
        if args.sub == "show":
            names.validate_workspace(args.name)
            try:
                current = settings.load(db, args.name)
            except InvalidInput:
                print("error: workspace not available", file=sys.stderr)
                return 1
            print(json.dumps(settings.dump(current), indent=2, sort_keys=True))
            return 0
        if args.sub == "list":
            with db.control_tx() as conn:
                rows = conn.execute(
                    "SELECT name, status, created_at FROM control.workspaces ORDER BY name"
                ).fetchall()
            for name, status, created in rows:
                print(f"{name}\t{status}\t{created:%Y-%m-%d}")
            return 0
        try:
            revoked = owner.archive_workspace(db, ACTOR, args.name)
        except owner.Refused as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"workspace {args.name} archived, {revoked} tokens revoked")
    return 0


def _cmd_token(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    s = _settings(env, _DB)
    with Database(s.database_url, s.role_prefix) as db:
        if args.sub == "create":
            actions = {a.strip() for a in args.actions.split(",") if a.strip()}
            print(owner.create_token(db, ACTOR, args.workspace, args.label, actions))
        elif args.sub == "list":
            for t in tokens.list_tokens(db, args.workspace):
                state = "revoked" if t["revoked_at"] else "active"
                print(f"{t['id']}\t{t['label']}\t{','.join(t['actions'])}\t{state}")
        else:
            try:
                owner.revoke_token(db, ACTOR, args.token_id)
            except owner.Refused as e:
                print(f"error: {e}", file=sys.stderr)
                return 1
            print(f"token {args.token_id} revoked")
    return 0


def _cmd_target(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    s = _settings(env, _DB)
    schema = sql.Identifier(names.ws_schema(args.workspace))
    with Database(s.database_url, s.role_prefix) as db:
        if args.sub == "add":
            try:
                owner.add_target(db, ACTOR, args.workspace, args.kind, args.value)
            except owner.Refused as e:
                print(f"error: {e}", file=sys.stderr)
                return 1
            print("target added")
        elif args.sub == "remove":
            try:
                owner.remove_target(db, ACTOR, args.workspace, args.kind, args.value)
            except owner.Refused as e:
                print(f"{e}", file=sys.stderr)
                return 1
            print("target removed")
        else:
            with db.ws_tx(args.workspace) as conn:
                rows = conn.execute(
                    sql.SQL("SELECT kind, value FROM {}.targets ORDER BY kind, value").format(
                        schema
                    )
                ).fetchall()
            for kind, value in rows:
                print(f"{kind}\t{value}")
    return 0


def _cmd_quarantine(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    s = _settings(env, _DB)
    with Database(s.database_url, s.role_prefix) as db:
        if args.sub == "list":
            try:
                rows = browse.quarantined(db, args.workspace)
            except InvalidInput:
                print("error: workspace not found", file=sys.stderr)
                return 1
            for r in rows:
                created = r["created_at"]
                print(
                    f"{r['workspace']}\t{r['revision_id']}\t{created:%Y-%m-%d %H:%M}\t{r['removed']}"
                )
            return 0
        if args.sub == "show":  # owner terminal only: the held text, to decide release or drop
            try:
                sections = sessions.quarantined_sections(db, args.workspace, args.revision)
            except InvalidInput:
                print("error: revision not found or not quarantined", file=sys.stderr)
                return 1
            print(json.dumps(sections, indent=2, ensure_ascii=False, sort_keys=True))
            return 0
        action = owner.release_quarantine if args.sub == "release" else owner.drop_quarantine
        try:
            action(db, ACTOR, args.workspace, args.revision)
        except InvalidInput:
            print("error: revision not found or not quarantined", file=sys.stderr)
            return 1
    print(f"revision {args.revision} {'released' if args.sub == 'release' else 'dropped'}")
    return 0


def _all(fn: Callable[..., list[dict]], db: Database, *args: Any, **kw: Any) -> list[dict]:
    """Every row of a paged ``browse`` read, so the list commands print the whole set."""
    rows: list[dict] = []
    while True:
        page = fn(db, *args, limit=browse.MAX_LIMIT, offset=len(rows), **kw)
        rows += page
        if len(page) < browse.MAX_LIMIT:
            return rows


def _general_list(db: Database, args: argparse.Namespace) -> int:
    for r in _all(browse.queue, db, args.status, args.workspace, hints=False):
        cats = ",".join(r["categories"])
        print(
            f"{r['id']}\t{r['workspace']}\t{r['status']}\t{r['kind']}\t{r['rewrites']}\t{cats}"
            f"\t{r['created_at']:%Y-%m-%d %H:%M}"
        )
    return 0


def _general_show(db: Database, args: argparse.Namespace) -> int:
    item = browse.queue_item(db, args.id)
    if item is None:
        print("error: queue item not found", file=sys.stderr)
        return 1
    print(
        f"id: {args.id}\nworkspace: {item['workspace']}\nstatus: {item['status']}"
        f"\nkind: {item['kind']}"
    )
    print(f"rewrites: {item['rewrites']}")
    if item["kind"] == "concept":
        print(f"name: {item['name']}\ntype: {item['type']}")
        print(f"aliases: {', '.join(item['aliases'])}")
    else:
        print(f"type: {item['type']}\ntext: {item['text']}")
        if item["source_text"] and item["source_text"] != item["text"]:
            print(f"source_text: {item['source_text']}")
    for r in item["reasons"]:
        print(f"reason: {r['category']} {r['start']}-{r['end']}")
    return 0


def _general_approve(db: Database, args: argparse.Namespace) -> int:
    try:
        findings = owner.check_approval(db, args.id, args.text)
        for f in findings:
            print(f"{f.category} {f.start}-{f.end}")
        if args.dry_run:
            if not findings:
                print("no findings")
            return 0
        owner.general_approve(db, ACTOR, args.id, args.text, force=args.yes)
    except owner.Flagged:
        print("error: the rules flagged this text; check it and pass --yes", file=sys.stderr)
        return 1
    except InvalidInput as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"published queue item {args.id}")
    return 0


def _general_drop(db: Database, args: argparse.Namespace) -> int:
    try:
        owner.general_drop(db, ACTOR, args.id)
    except owner.Refused as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"queue item {args.id} dropped")
    return 0


def _cmd_general(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    s = _settings(env, _DB)
    handler = {
        "list": _general_list,
        "show": _general_show,
        "approve": _general_approve,
        "drop": _general_drop,
    }[args.sub]
    with Database(s.database_url, s.role_prefix) as db:
        return handler(db, args)


def _cmd_pairs(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    """``merges list`` / ``conflicts list``: owner terminal only, names and texts included."""
    names.validate_workspace(args.workspace)
    s = _settings(env, _DB)
    read = browse.merges if args.command == "merges" else browse.conflicts
    with Database(s.database_url, s.role_prefix) as db:
        try:
            rows = read(db, args.workspace)
        except InvalidInput:
            print("error: workspace not available", file=sys.stderr)
            return 1
    for r in rows:
        if args.command == "merges":
            print(f"{r['node_a']}\t{r['node_b']}\t{r['score']:.2f}\t{r['name_a']}\t{r['name_b']}")
        else:
            print(f"{r['new_fact']}\t{r['old_fact']}\t{r['new_text']}\t{r['old_text']}")
    return 0


def _stale_list(db: Database, args: argparse.Namespace) -> int:
    for r in browse.stale_flags(db, args.workspace):
        tokens = ",".join(m["token"] for m in r["missing"])
        when = r["created_at"].strftime("%Y-%m-%d %H:%M")
        print(f"{r['id']}\t{r['fact_id']}\t{r['repo']}\t{r['commit'][:7]}\t{tokens}\t{when}")
    return 0


def _stale_show(db: Database, args: argparse.Namespace) -> int:
    r = browse.stale_flag(db, args.workspace, args.id)
    if r is None:
        print("error: stale flag not found", file=sys.stderr)  # show also shows resolved flags
        return 1
    print(r["text"])
    print(f"repo: {r['repo']} at {r['commit'][:7]}")
    for m in r["missing"]:
        removed = m.get("commit")
        subject = m.get("subject")
        if removed:
            how = f"removed in {removed[:7]}" + (f": {subject}" if subject else "")
        else:
            how = "not found"
        print(f"missing: {m['token']} ({how})")
    print(f"status: {r['resolution'] or 'open'}")
    return 0


def _stale_act(db: Database, args: argparse.Namespace) -> int:
    act = owner.keep_stale if args.sub == "keep" else owner.retract_stale
    try:
        act(db, ACTOR, args.workspace, args.id)
    except owner.Refused as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"stale flag {args.id} {'kept' if args.sub == 'keep' else 'retracted'}")
    return 0


def _cmd_stale(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    names.validate_workspace(args.workspace)
    s = _settings(env, _DB)
    handler = {
        "list": _stale_list,
        "show": _stale_show,
        "keep": _stale_act,
        "retract": _stale_act,
    }[args.sub]
    with Database(s.database_url, s.role_prefix) as db:
        try:
            return handler(db, args)
        except InvalidInput:
            print("error: workspace not available", file=sys.stderr)
            return 1


def _jobs_retry(db: Database, args: argparse.Namespace) -> int:
    if args.id is None and args.status is None:
        print("error: give a job id or --status dead", file=sys.stderr)
        return 2
    try:
        ids = owner.retry_jobs(db, ACTOR, args.id, args.status, args.kind, args.workspace)
    except owner.Refused as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"retried {len(ids)}")
    return 0


def _cmd_jobs(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    s = _settings(env, _DB)
    if args.sub == "retry":
        with Database(s.database_url, s.role_prefix) as db:
            return _jobs_retry(db, args)
    with Database(s.database_url, s.role_prefix) as db:
        rows = _all(browse.jobs, db, args.status, None, None)
    for r in rows:
        print(
            f"{r['id']}\t{r['workspace']}\t{r['kind']}\t{r['status']}\t{r['attempts']}"
            f"\t{r['error_category'] or ''}"
        )
    return 0


def _cmd_distill(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    s = _settings(env, _DB)
    with Database(s.database_url, s.role_prefix) as db:
        try:
            counts = distill.backfill(db, args.workspace, dry_run=args.dry_run)
        except InvalidInput:
            print("error: workspace not available or its settings are invalid", file=sys.stderr)
            return 1
    line = " ".join(f"{k}={v}" for k, v in counts.items())
    print(line + (" (dry run)" if args.dry_run else ""))
    return 0


def _cmd_restore(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    s = _settings(env, _DB)
    with Database(s.database_url, s.role_prefix) as db:
        try:
            counts = restore.run(db, args.namespace, dry_run=args.dry_run)
        except (InvalidInput, ValueError):
            print("error: unknown workspace", file=sys.stderr)
            return 1
    line = " ".join(f"{k}={v}" for k, v in counts.items())
    print(line + (" (dry run)" if args.dry_run else ""))
    return 0


def _cmd_duplicates(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    """Groups of near-identical current facts: counts and sizes only, never text (the dashboard
    shows and merges them)."""
    s = _settings(env, _DB)
    with Database(s.database_url, s.role_prefix) as db:
        try:
            groups = browse.duplicates(db, args.namespace, args.threshold)
        except InvalidInput as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    sizes = [len(g["facts"]) for g in groups]
    print(f"groups={len(groups)} facts={sum(sizes)} sizes={','.join(map(str, sizes))}")
    return 0


def _cmd_schemas(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    s = _settings(env, _ADMIN)
    with psycopg.connect(s.admin_database_url) as conn:
        rows = conn.execute("SELECT name FROM control.workspaces ORDER BY name").fetchall()
    for schema in ("control", "general", *(names.ws_schema(r[0]) for r in rows)):
        print(schema)
    return 0


def _cmd_canary(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    if args.sub == "list":
        s = _settings(env, _DB)
        with Database(s.database_url, s.role_prefix) as db:
            for name, n in canary.counts(db):
                print(f"{name}\t{n}")
        return 0
    s = _settings(env, _EMBED)
    embedder = _embedder(s)
    with Database(s.database_url, s.role_prefix) as db:
        if args.if_missing:
            names.validate_workspace(args.workspace)
            if canary.has_canary(db, args.workspace):  # archived workspaces too
                print("exists")
                return 0
        canary.plant(db, embedder, args.workspace)
        with db.control_tx() as conn:
            owner.audit(conn, ACTOR, "canary.plant", {"workspace": args.workspace})
    print(f"canary planted in {args.workspace}")
    return 0


def _cmd_isolation(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    s = _settings(env, _EMBED)
    with Database(s.database_url, s.role_prefix) as db:
        faults = canary.check(db, _embedder(s))
        workspaces, pairs, canaries = canary.stats(db)
        held = canary.queue_held(db)
        with db.control_tx() as conn:
            for name, value in (("isolation.faults", len(faults)), ("isolation.checks", pairs)):
                conn.execute(
                    "INSERT INTO control.metrics (name, workspace, value) VALUES (%s, NULL, %s)",
                    (name, value),
                )
    print(f"workspaces: {workspaces} pairs: {pairs} canaries: {canaries} faults: {len(faults)}")
    print(f"canary markers held in queue: {held}")
    for check, src, dst in faults:
        print(f"{check}\t{src}\t{dst}")
    return 1 if faults else 0


JobHandler = Callable[[jobs.Job], Any]


def _default_handlers(db: Database, env: Mapping[str, str]) -> dict[str, JobHandler]:
    return {
        "distill": lambda job: distill.handle(db, env, job),
        "apply": lambda job: apply.handle(db, job),
        "gate": lambda job: gate.handle(db, env, job),
    }


_STAGE_ATTEMPTS = {
    "gate": gate.MAX_ATTEMPTS,
    "distill": distill.MAX_ATTEMPTS,
    "apply": apply.MAX_ATTEMPTS,
}


def _failure_category(stage: str, exc: Exception) -> str:
    if isinstance(exc, EmbedderUnavailable):
        return "embedder"
    if isinstance(exc, BackendBadReply):
        return "judge_bad_reply" if stage == "gate" else "model_bad_reply"
    if isinstance(exc, BackendUnavailable):
        return "judge_unavailable" if stage == "gate" else "model_unavailable"
    return "internal"


def _max_attempts(stage: str, category: str) -> int:
    if category == "model_bad_reply":
        return distill.BAD_REPLY_ATTEMPTS
    if stage == "gate":
        return gate.ATTEMPTS.get(category, gate.MAX_ATTEMPTS)
    return _STAGE_ATTEMPTS.get(stage, 8)


def _run_job_stage(db: Database, stage: str, handler: JobHandler) -> bool:
    """Claim and handle one job of ``stage``; True if a job was claimed. The handlers settle
    the job themselves; an exception that escapes one fails it here, by category."""
    job = jobs.claim(db, {stage})
    if job is None:
        return False
    try:
        handler(job)
    except Exception as e:  # noqa: BLE001 - recorded by category and type name only
        category = _failure_category(stage, e)
        log.error("worker stage=%s failed category=%s error=%s", stage, category, type(e).__name__)
        try:
            jobs.fail(db, job, category, max_attempts=_max_attempts(stage, category))
        except Exception as e2:  # noqa: BLE001 - the lease expiry re-queues the job
            log.error("worker stage=%s fail-record error=%s", stage, type(e2).__name__)
    return True


def worker_loop(
    db: Database,
    embedder: Embedder,
    env: Mapping[str, str],
    max_iterations: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    handlers: Mapping[str, JobHandler] | None = None,
) -> None:
    """Run every stage forever: per iteration one embed, distill, apply and gate job, then the
    general embeddings. A stage error is logged by type name and the loop goes on; it sleeps
    only when no stage found work. ``handlers`` replaces the job stage handlers (tests)."""
    handlers = handlers if handlers is not None else _default_handlers(db, env)
    done = 0
    while max_iterations is None or done < max_iterations:
        done += 1
        busy = False
        stages: list[tuple[str, Callable[[], Any]]] = [
            ("embed", lambda: worker.run_once(db, embedder))
        ]
        stages += [
            (name, partial(_run_job_stage, db, name, handlers[name]))
            for name in ("distill", "apply", "gate")
            if name in handlers
        ]
        stages.append(("embed_general", lambda: worker.embed_general(db, embedder)))
        for name, stage in stages:
            try:
                busy = bool(stage()) or busy
            except Exception as e:  # noqa: BLE001 - the worker must never die; type name only
                log.error("worker stage=%s failed error=%s", name, type(e).__name__)
        if not busy:
            sleep(IDLE_SLEEP_S)


def _cmd_worker(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    s = _settings(env, _EMBED)
    with Database(s.database_url, s.role_prefix) as db:
        worker_loop(db, _embedder(s), env)
    return 0


def _cmd_serve(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    if args.list_tools:
        # Tool listing for MCP directories: stdio, no settings, no database, every call refused.
        from treant.server import build_listing_mcp

        build_listing_mcp().run("stdio")
        return 0
    import uvicorn

    from treant.server import build_app

    s = _settings(env, _EMBED)
    host, _, port = s.bind.rpartition(":")
    with Database(s.database_url, s.role_prefix) as db:
        uvicorn.run(
            build_app(db, _embedder(s), s.allowed_hosts), host=host or "127.0.0.1", port=int(port)
        )
    return 0


def _cmd_dashboard(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    import uvicorn

    from treant.dashboard.app import build_dashboard
    from treant.dashboard.config import DashboardSettings
    from treant.dashboard.identity import NoProvider, OidcProvider

    s = _settings(env, _EMBED)
    ds = DashboardSettings.from_env(env)
    host, _, port = ds.bind.rpartition(":")
    with Database(s.database_url, s.role_prefix) as db:
        provider = OidcProvider(ds) if ds.auth == "oidc" else NoProvider()
        app = build_dashboard(db, _embedder(s), ds, provider)
        # No access log: the login callback's query string carries the OIDC code and state.
        uvicorn.run(app, host=host or "0.0.0.0", port=int(port), access_log=False)
    return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="treant")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("bootstrap").set_defaults(fn=_cmd_bootstrap)
    sub.add_parser("upgrade").set_defaults(fn=_cmd_upgrade)

    ws = sub.add_parser("workspace").add_subparsers(dest="sub", required=True)
    for name in ("create", "archive", "purge", "purge-verify"):
        c = ws.add_parser(name)
        c.add_argument("name")
        if name == "purge":
            c.add_argument("--confirm", required=True)
        if name == "purge-verify":
            c.add_argument("--repo-gone", required=True, choices=("yes", "no"))
        c.set_defaults(fn=_cmd_workspace)
    ws.add_parser("list").set_defaults(fn=_cmd_workspace)
    c = ws.add_parser("show")
    c.add_argument("name")
    c.set_defaults(fn=_cmd_workspace)
    c = ws.add_parser("set")
    c.add_argument("name")
    c.add_argument("--backend")
    c.add_argument("--alias")
    c.add_argument("--judge-alias", dest="judge_alias")
    c.add_argument("--publish")
    c.add_argument("--reset", action="store_true", help="start from defaults, not the stored row")
    for opt, plural in _LISTS:
        flag = opt.replace("_", "-")
        c.add_argument(f"--{flag}", dest=opt, action="append", default=[])
        c.add_argument(
            f"--clear-{plural.replace('_', '-')}", dest=f"clear_{plural}", action="store_true"
        )
    c.set_defaults(fn=_cmd_workspace)

    tk = sub.add_parser("token").add_subparsers(dest="sub", required=True)
    c = tk.add_parser("create")
    c.add_argument("workspace")
    c.add_argument("--label", required=True)
    c.add_argument("--actions", required=True, help="comma list of create,read,update,delete")
    c = tk.add_parser("list")
    c.add_argument("workspace")
    c = tk.add_parser("revoke")
    c.add_argument("token_id")
    for c in tk.choices.values():
        c.set_defaults(fn=_cmd_token)

    tg = sub.add_parser("target").add_subparsers(dest="sub", required=True)
    for name in ("add", "remove"):
        c = tg.add_parser(name)
        c.add_argument("workspace")
        c.add_argument("kind", choices=TARGET_KINDS)
        c.add_argument("value")
    c = tg.add_parser("list")
    c.add_argument("workspace")
    for c in tg.choices.values():
        c.set_defaults(fn=_cmd_target)

    cn = sub.add_parser("canary").add_subparsers(dest="sub", required=True)
    c = cn.add_parser("plant")
    c.add_argument("workspace")
    c.add_argument("--if-missing", action="store_true")
    c.set_defaults(fn=_cmd_canary)
    cn.add_parser("list").set_defaults(fn=_cmd_canary)

    qn = sub.add_parser("quarantine").add_subparsers(dest="sub", required=True)
    c = qn.add_parser("list")
    c.add_argument("--workspace")
    for name in ("show", "release", "drop"):
        c = qn.add_parser(name)
        c.add_argument("workspace")
        c.add_argument("revision")
    for c in qn.choices.values():
        c.set_defaults(fn=_cmd_quarantine)

    gn = sub.add_parser("general").add_subparsers(dest="sub", required=True)
    c = gn.add_parser("list")
    c.add_argument("--status")
    c.add_argument("--workspace")
    for name in ("show", "approve", "drop"):
        c = gn.add_parser(name)
        c.add_argument("id", type=int)
        if name == "approve":
            c.add_argument("--text")
            c.add_argument("--yes", action="store_true", help="publish despite rule findings")
            c.add_argument("--dry-run", action="store_true", help="print findings only")
    for c in gn.choices.values():
        c.set_defaults(fn=_cmd_general)
    for name in ("merges", "conflicts"):
        c = sub.add_parser(name).add_subparsers(dest="sub", required=True).add_parser("list")
        c.add_argument("workspace")
        c.set_defaults(fn=_cmd_pairs)
    st = sub.add_parser("stale").add_subparsers(dest="sub", required=True)
    for name in ("list", "show", "keep", "retract"):
        c = st.add_parser(name)
        c.add_argument("workspace")
        if name != "list":
            c.add_argument("id", type=int)
        c.set_defaults(fn=_cmd_stale)
    jb = sub.add_parser("jobs").add_subparsers(dest="sub", required=True)
    c = jb.add_parser("list")
    c.add_argument("--status")
    c = jb.add_parser("retry")
    c.add_argument("id", type=int, nargs="?")
    c.add_argument("--status", choices=("dead",))
    c.add_argument("--kind")
    c.add_argument("--workspace")
    for c in jb.choices.values():
        c.set_defaults(fn=_cmd_jobs)

    c = sub.add_parser("distill").add_subparsers(dest="sub", required=True).add_parser("backfill")
    c.add_argument("workspace")
    c.add_argument("--dry-run", action="store_true")
    c.set_defaults(fn=_cmd_distill)

    c = sub.add_parser("restore", help="restore items superseded by the old replace-on-save rule")
    c.add_argument("namespace", help="a workspace name, or general")
    c.add_argument("--dry-run", action="store_true")
    c.set_defaults(fn=_cmd_restore)

    c = sub.add_parser("duplicates", help="count groups of near-identical current facts")
    c.add_argument("namespace", help="a workspace name, or general")
    c.add_argument("--threshold", type=float, default=browse.DUPLICATE_THRESHOLD)
    c.set_defaults(fn=_cmd_duplicates)

    sub.add_parser("schemas").set_defaults(fn=_cmd_schemas)
    sub.add_parser("isolation-test").set_defaults(fn=_cmd_isolation)
    sv = sub.add_parser("serve")
    sv.add_argument(
        "--list-tools",
        action="store_true",
        help="serve only the tool list over stdio, without a database (for MCP directories)",
    )
    sv.set_defaults(fn=_cmd_serve)
    sub.add_parser("worker").set_defaults(fn=_cmd_worker)
    sub.add_parser("dashboard").set_defaults(fn=_cmd_dashboard)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.fn(args, os.environ)
    except ValueError as e:
        # Messages raised by this package's own validation are safe; settings errors name a
        # variable, never a value.
        print(f"error: {e}", file=sys.stderr)
        return 2
    except psycopg.Error as e:
        print(f"error: database ({type(e).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
