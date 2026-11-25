"""Bootstrap the database and create workspaces.

Runs over the admin DSN (a superuser: creating the ``vector`` extension needs it).
Roles are cluster-wide while schemas live in one database, so role creation is
guarded by a ``pg_roles`` lookup and every grant is re-applied on each run.
Objects are created as the Treant ``admin`` role, which therefore owns them.
"""

import logging
from importlib import resources

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from treant import names

log = logging.getLogger(__name__)

_LOCK_KEY = "treant.migrate"
_SERVICE_KINDS = ("control", "gate", "publisher", "admin")
_NO_PRIVILEGES = "nosuperuser nocreatedb nocreaterole noreplication nobypassrls"
# A purge drops a schema and policies on the shared queue tables; behind a long-running query it
# gives up instead of queueing every later statement on those tables behind its locks.
PURGE_LOCK_TIMEOUT = "10s"


def _load(filename: str) -> str:
    return resources.files("treant").joinpath("sql", filename).read_text(encoding="utf-8")


def _role_ids(prefix: str) -> dict[str, sql.Identifier]:
    return {kind: sql.Identifier(names.role(prefix, kind)) for kind in _SERVICE_KINDS}


def _ensure_role(conn: psycopg.Connection, role: str, attributes: sql.Composable) -> None:
    exists = conn.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone()
    verb = "alter" if exists else "create"
    conn.execute(
        sql.SQL(verb + " role {} with " + _NO_PRIVILEGES + " ").format(sql.Identifier(role))
        + attributes
    )


def _grant_membership(conn: psycopg.Connection, role: str, login: str) -> None:
    conn.execute(
        sql.SQL("grant {} to {} with inherit false, set true, admin false").format(
            sql.Identifier(role), sql.Identifier(login)
        )
    )


def _lock(conn: psycopg.Connection) -> None:
    conn.execute("select pg_advisory_xact_lock(hashtext(%s))", (_LOCK_KEY,))


def bootstrap(admin_dsn: str, prefix: str, login_password: str) -> None:
    roles = _role_ids(prefix)
    login = names.role(prefix, "login")
    with psycopg.connect(admin_dsn) as conn, conn.transaction():
        _lock(conn)
        conn.execute("create extension if not exists vector")
        # The verifier is computed here so the plaintext never reaches the server's statement
        # text, logs or pg_stat_statements.
        verifier = conn.pgconn.encrypt_password(
            login_password.encode(), login.encode(), b"scram-sha-256"
        ).decode()
        for kind in _SERVICE_KINDS:
            _ensure_role(conn, names.role(prefix, kind), sql.SQL("nologin"))
        _ensure_role(
            conn,
            login,
            sql.SQL("login noinherit password {}").format(sql.Literal(verifier)),
        )
        for kind in _SERVICE_KINDS:
            _grant_membership(conn, names.role(prefix, kind), login)
        for schema in ("control", "general"):
            conn.execute(
                sql.SQL("create schema if not exists {} authorization {}").format(
                    sql.Identifier(schema), roles["admin"]
                )
            )
            conn.execute(
                sql.SQL("alter schema {} owner to {}").format(
                    sql.Identifier(schema), roles["admin"]
                )
            )
        conn.execute(sql.SQL("set local role {}").format(roles["admin"]))
        conn.execute(sql.SQL(_load("control.sql")).format(**roles))
        conn.execute(sql.SQL(_load("general.sql")).format(**roles))
    upgrade(admin_dsn, prefix)
    log.info("bootstrap complete")


def create_workspace(admin_dsn: str, prefix: str, name: str) -> None:
    names.validate_workspace(name)
    roles = _role_ids(prefix)
    schema = names.ws_schema(name)
    ws_role = names.ws_role(prefix, name)
    login = names.role(prefix, "login")
    with psycopg.connect(admin_dsn) as conn, conn.transaction():
        _lock(conn)
        created = conn.execute(
            "insert into control.workspaces (name) values (%s)"
            " on conflict (name) do nothing returning name",
            (name,),
        ).fetchone()
        if created is None:
            raise ValueError("workspace exists")
        if conn.execute("select 1 from pg_roles where rolname = %s", (ws_role,)).fetchone():
            raise ValueError("workspace role exists")
        _ensure_role(conn, ws_role, sql.SQL("nologin"))
        _grant_membership(conn, ws_role, login)
        conn.execute(
            sql.SQL("create schema {} authorization {}").format(
                sql.Identifier(schema), roles["admin"]
            )
        )
        conn.execute(sql.SQL("set local role {}").format(roles["admin"]))
        conn.execute(
            sql.SQL(_load("workspace.sql")).format(
                schema=sql.Identifier(schema),
                role=sql.Identifier(ws_role),
                gate=roles["gate"],
                admin=roles["admin"],
                policy=sql.Identifier(f"{schema}_insert"),
                workspace=sql.Literal(name),
            )
        )
    upgrade(admin_dsn, prefix)
    log.info("workspace created")


def upgrade(admin_dsn: str, prefix: str) -> list[str]:
    """Apply the idempotent upgrade SQL to control, general and every workspace.

    Returns the names of the workspaces upgraded (active and archived).
    """
    roles = _role_ids(prefix)
    with psycopg.connect(admin_dsn) as conn, conn.transaction():
        _lock(conn)
        workspaces = [
            r[0] for r in conn.execute("select name from control.workspaces order by name")
        ]
        # Re-assert the role attributes while still superuser, so drift on a workspace role
        # (say a manual ALTER ROLE ... BYPASSRLS) does not survive a deploy.
        for name in workspaces:
            ws_role = names.ws_role(prefix, name)
            if conn.execute("select 1 from pg_roles where rolname = %s", (ws_role,)).fetchone():
                _ensure_role(conn, ws_role, sql.SQL("nologin"))
        conn.execute(sql.SQL("set local role {}").format(roles["admin"]))
        conn.execute(sql.SQL(_load("upgrade_control.sql")).format(**roles))
        conn.execute(sql.SQL(_load("upgrade_general.sql")).format(**roles))
        for name in workspaces:
            conn.execute(
                sql.SQL(_load("upgrade_workspace.sql")).format(
                    schema=sql.Identifier(names.ws_schema(name)),
                    role=sql.Identifier(names.ws_role(prefix, name)),
                )
            )
    log.info("upgrade complete workspaces=%d", len(workspaces))
    return workspaces


def purge_workspace(admin_dsn: str, prefix: str, name: str) -> None:
    """Irreversibly remove an archived workspace: schema, role and its control rows.

    One transaction over the admin DSN, with ``PURGE_LOCK_TIMEOUT`` (a held lock raises
    ``LockNotAvailable`` and nothing is removed). ``control.audit`` rows stay (audit is insert-only), and
    so do ``control.general_origin`` rows (the provenance of published general items).
    Raises ``ValueError`` for an invalid name or a workspace that is not archived.
    """
    names.validate_workspace(name)
    schema = sql.Identifier(names.ws_schema(name))
    ws_role = sql.Identifier(names.ws_role(prefix, name))
    policy = sql.Identifier(f"{names.ws_schema(name)}_insert")
    with psycopg.connect(admin_dsn) as conn, conn.transaction():
        conn.execute("select set_config('lock_timeout', %s, true)", (PURGE_LOCK_TIMEOUT,))
        _lock(conn)
        row = conn.execute(
            "select status from control.workspaces where name = %s for update", (name,)
        ).fetchone()
        if row is None or row[0] != "archived":
            raise ValueError("workspace not available")
        conn.execute(
            "update control.tokens set revoked_at = now() where workspace = %s "
            "and revoked_at is null",
            (name,),
        )
        for table in ("jobs", "mistakes"):
            conn.execute(
                sql.SQL("drop policy if exists {} on control.{}").format(
                    policy, sql.Identifier(table)
                )
            )
        conn.execute(sql.SQL("drop schema {} cascade").format(schema))
        conn.execute(sql.SQL("drop owned by {}").format(ws_role))
        conn.execute(sql.SQL("drop role {}").format(ws_role))
        # general_origin rows stay: they record where published general items came from.
        for table in (
            "metrics",
            "mistakes",
            "canaries",
            "jobs",
            "tokens",
            "general_queue",
            "general_session_head",
        ):
            conn.execute(
                sql.SQL("delete from control.{} where workspace = %s").format(
                    sql.Identifier(table)
                ),
                (name,),
            )
        conn.execute("delete from control.workspaces where name = %s", (name,))
        conn.execute(
            "insert into control.audit (actor, action, detail) values ('cli', %s, %s)",
            ("workspace.purge", Jsonb({"workspace": name})),
        )
    log.info("workspace purged")
