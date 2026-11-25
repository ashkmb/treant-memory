import socket
import time
import uuid

import psycopg
import pytest
from psycopg import sql

PG_HOST = "127.0.0.1"
PG_PORT = 55432
PG_USER = "postgres"
PG_PASSWORD = "test"
LOGIN_PASSWORD = "login-test"


def _port_open() -> bool:
    try:
        with socket.create_connection((PG_HOST, PG_PORT), timeout=1):
            return True
    except OSError:
        return False


def _dsn(dbname: str, user: str = PG_USER, password: str = PG_PASSWORD) -> str:
    return f"postgresql://{user}:{password}@{PG_HOST}:{PG_PORT}/{dbname}"


_ready: list[bool] = []


def _pg_ready(attempts: int = 20, delay_s: float = 0.5) -> bool:
    """The test Postgres accepts connections (it may still be starting with the port open)."""
    if not _ready:
        ok = False
        if _port_open():
            for _ in range(attempts):
                try:
                    with psycopg.connect(_dsn("postgres"), connect_timeout=2) as conn:
                        conn.execute("select 1")
                    ok = True
                    break
                except psycopg.OperationalError:
                    time.sleep(delay_s)
        _ready.append(ok)
    return _ready[0]


def _require_pg() -> None:
    if not _pg_ready():
        pytest.skip(
            f"test Postgres not reachable on {PG_HOST}:{PG_PORT}; "
            "run: docker compose -f compose.test.yml up -d"
        )


def _create_test_db() -> tuple[str, str]:
    """Create a throwaway database; return (dbname, role prefix)."""
    dbname = f"treant_test_{uuid.uuid4().hex[:8]}"
    prefix = f"t{uuid.uuid4().hex[:8]}"
    with psycopg.connect(_dsn("postgres"), autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    return dbname, prefix


def _drop_test_db(dbname: str, prefix: str) -> None:
    with psycopg.connect(_dsn("postgres"), autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname))
        )
        roles = [
            r[0]
            for r in conn.execute(
                "SELECT rolname FROM pg_roles WHERE starts_with(rolname, %s)", (prefix,)
            )
        ]
        for role in roles:
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


@pytest.fixture(scope="session")
def pg():
    """Yield (admin_dsn, login_dsn, prefix) for a throwaway database and role prefix."""
    _require_pg()
    dbname, prefix = _create_test_db()
    try:
        yield _dsn(dbname), _dsn(dbname, f"{prefix}_login", LOGIN_PASSWORD), prefix
    finally:
        _drop_test_db(dbname, prefix)


@pytest.fixture(scope="session")
def admin_dsn(pg):
    return pg[0]


@pytest.fixture(scope="session")
def login_dsn(pg):
    return pg[1]


@pytest.fixture(scope="session")
def prefix(pg):
    return pg[2]


@pytest.fixture(scope="session")
def db(admin_dsn, login_dsn, prefix):
    """A Database on a bootstrapped test database with workspaces `alpha` and `beta`."""
    from treant import migrate
    from treant.db import Database

    migrate.bootstrap(admin_dsn, prefix, LOGIN_PASSWORD)
    migrate.create_workspace(admin_dsn, prefix, "alpha")
    migrate.create_workspace(admin_dsn, prefix, "beta")
    database = Database(login_dsn, prefix)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def ctx_a():
    from treant.tokens import TokenContext

    return TokenContext("a1a1a1a1a1a1", "alpha", frozenset({"create", "read", "update", "delete"}))


@pytest.fixture
def ctx_a2():
    from treant.tokens import TokenContext

    return TokenContext("a2a2a2a2a2a2", "alpha", frozenset({"create", "read", "update", "delete"}))


@pytest.fixture
def fresh(monkeypatch):
    """A throwaway bootstrapped database (own role prefix) with no workspaces.

    Yields (admin_dsn, login_dsn, prefix); also points the CLI's env at it.
    """
    _require_pg()
    from treant import migrate

    dbname, prefix = _create_test_db()
    admin, login = _dsn(dbname), _dsn(dbname, f"{prefix}_login", LOGIN_PASSWORD)
    try:
        migrate.bootstrap(admin, prefix, LOGIN_PASSWORD)
        monkeypatch.setenv("TREANT_ADMIN_DATABASE_URL", admin)
        monkeypatch.setenv("TREANT_DATABASE_URL", login)
        monkeypatch.setenv("TREANT_ROLE_PREFIX", prefix)
        monkeypatch.setenv("TREANT_EMBED_URL", "http://127.0.0.1:1")
        yield admin, login, prefix
    finally:
        _drop_test_db(dbname, prefix)


@pytest.fixture
def fresh_db(fresh):
    from treant import migrate
    from treant.db import Database

    admin, login, prefix = fresh
    for name in ("alpha", "beta", "gamma"):
        migrate.create_workspace(admin, prefix, name)
    database = Database(login, prefix)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def queued_item(fresh_db):
    """An open queue row (alpha) whose text names a planted instance node ('zorblax')."""
    import uuid

    from psycopg.types.json import Jsonb

    from treant import apply, graph

    with fresh_db.ws_tx("alpha") as conn:
        graph.add_node(conn, "alpha", "instance", "cluster", "zorblax")
    item = {
        "kind": "fact",
        "type": "lesson",
        "text": "use zorblax for x",
        "concept": None,
        "source_text": "use zorblax for x",
    }
    with fresh_db.publisher_tx() as conn:
        return conn.execute(
            "INSERT INTO control.general_queue "
            "(workspace, session_id, revision_id, item, item_key, status) "
            "VALUES (%s, %s, %s, %s, %s, 'waiting_review') RETURNING id",
            (
                "alpha",
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                Jsonb(item),
                apply.item_key("fact", item["text"]),
            ),
        ).fetchone()[0]
