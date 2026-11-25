import uuid

import psycopg
import pytest
from conftest import LOGIN_PASSWORD

from treant import migrate, names


def _fresh_name(stem: str) -> str:
    return f"{stem}{uuid.uuid4().hex[:8]}"


def test_names():
    assert names.validate_workspace("alpha") == "alpha"
    assert names.ws_schema("alpha") == "ws_alpha"
    assert names.ws_role("treant", "alpha") == "treant_ws_alpha"
    for kind in ["control", "gate", "publisher", "admin", "login"]:
        assert names.role("treant", kind) == f"treant_{kind}"
    with pytest.raises(ValueError):
        names.role("treant", "superuser")


@pytest.mark.parametrize(
    "bad",
    ["Bad", "general", "control", "a", "1abc", "ab-c", "ab_c", "a" * 32, "alpha\n", "", None],
)
def test_validate_workspace_rejects(bad):
    with pytest.raises(ValueError, match="invalid workspace name"):
        names.validate_workspace(bad)
    with pytest.raises(ValueError, match="invalid workspace name"):
        names.ws_schema(bad)


def test_create_workspace_rejects_bad_and_reserved_names(admin_dsn, prefix, db):
    for bad in ["Bad", "general", "control", 'x"; drop schema control; --']:
        with pytest.raises(ValueError, match="invalid workspace name"):
            migrate.create_workspace(admin_dsn, prefix, bad)
    with psycopg.connect(admin_dsn) as conn:
        schemas = {
            r[0]
            for r in conn.execute(
                "select nspname from pg_namespace where nspname in ('control', 'general')"
            )
        }
        assert schemas == {"control", "general"}
        assert (
            conn.execute(
                "select count(*) from control.workspaces where lower(name) = any(%s)"
                " or name like '%%drop%%'",
                (["bad", "general", "control"],),
            ).fetchone()[0]
            == 0
        )


def test_bootstrap_and_create_workspace_are_idempotent_or_explicit(admin_dsn, prefix, db):
    migrate.bootstrap(admin_dsn, prefix, LOGIN_PASSWORD)
    migrate.bootstrap(admin_dsn, prefix, LOGIN_PASSWORD)
    name = _fresh_name("idem")
    migrate.create_workspace(admin_dsn, prefix, name)
    with pytest.raises(ValueError, match="workspace exists"):
        migrate.create_workspace(admin_dsn, prefix, name)
    migrate.bootstrap(admin_dsn, prefix, LOGIN_PASSWORD)
    # existing workspaces keep working after a re-bootstrap
    with db.ws_tx(name) as c:
        c.execute(f"insert into ws_{name}.targets (kind, value) values ('git_host', 'h')")
        c.execute("insert into control.jobs (workspace, kind, key) values (%s, 'k', 'v')", (name,))
    with db.ws_tx("alpha") as c:
        c.execute("select count(*) from ws_alpha.nodes")
    with psycopg.connect(admin_dsn) as conn:
        assert (
            conn.execute(
                "select status from control.workspaces where name = %s", (name,)
            ).fetchone()[0]
            == "active"
        )


def test_roles_have_expected_attributes(admin_dsn, prefix, db):
    with psycopg.connect(admin_dsn) as conn:
        rows = {
            r[0]: r[1:]
            for r in conn.execute(
                "select rolname, rolcanlogin, rolinherit, rolsuper, rolcreaterole, rolbypassrls"
                " from pg_roles where starts_with(rolname, %s)",
                (prefix + "_",),
            )
        }
        login = names.role(prefix, "login")
        assert rows[login] == (True, False, False, False, False)
        treant_roles = [names.role(prefix, k) for k in ["control", "gate", "publisher", "admin"]]
        treant_roles += [names.ws_role(prefix, "alpha"), names.ws_role(prefix, "beta")]
        for role in treant_roles:
            assert rows[role][0] is False, role  # NOLOGIN
            assert rows[role][2:] == (False, False, False), role
        members = {
            r[0]: r[1:]
            for r in conn.execute(
                "select r.rolname, m.inherit_option, m.set_option, m.admin_option"
                " from pg_auth_members m join pg_roles r on r.oid = m.roleid"
                " join pg_roles u on u.oid = m.member where u.rolname = %s",
                (login,),
            )
        }
        for role in treant_roles:
            assert members[role] == (False, True, False), role
        # the login role owns nothing and holds no direct privileges
        assert (
            conn.execute(
                "select count(*) from pg_class c join pg_roles r on r.oid = c.relowner"
                " where r.rolname = %s",
                (login,),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "select count(*) from information_schema.role_table_grants where grantee = %s",
                (login,),
            ).fetchone()[0]
            == 0
        )


def test_workspace_row_and_schema_created_together(admin_dsn, prefix, db):
    with psycopg.connect(admin_dsn) as conn:
        assert (
            conn.execute(
                "select count(*) from control.workspaces where name in ('alpha', 'beta')"
            ).fetchone()[0]
            == 2
        )
        tables = {
            r[0]
            for r in conn.execute(
                "select table_name from information_schema.tables where table_schema = 'ws_alpha'"
            )
        }
        assert {
            "sessions",
            "revisions",
            "journal_entries",
            "nodes",
            "edges",
            "facts",
            "support",
            "versions",
            "embeddings",
            "targets",
            "instance_names",
        } <= tables


def test_bootstrap_never_sends_password_text(fresh, monkeypatch):
    admin, login_url, prefix = fresh
    seen: list[str] = []

    def record(target):
        real = target.execute

        def recording(self, query, params=None, **kw):
            conn = self if isinstance(self, psycopg.Connection) else self.connection
            text = query if isinstance(query, (str, bytes)) else query.as_string(conn)
            seen.append(text if isinstance(text, str) else text.decode())
            values = params.values() if isinstance(params, dict) else (params or ())
            seen.extend(str(p) for p in values)
            return real(self, query, params, **kw)

        monkeypatch.setattr(target, "execute", recording)

    record(psycopg.Connection)
    record(psycopg.Cursor)
    migrate.bootstrap(admin, prefix, LOGIN_PASSWORD)
    monkeypatch.undo()
    assert seen
    assert not any(LOGIN_PASSWORD in s for s in seen)
    assert any("SCRAM-SHA-256$" in s for s in seen)
    with psycopg.connect(login_url) as conn:
        assert conn.execute("select 1").fetchone()[0] == 1


def test_bootstrap_strips_dangerous_role_attributes(fresh):
    admin, _, prefix = fresh
    gate = names.role(prefix, "gate")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'alter role "{gate}" with bypassrls createrole')
    migrate.bootstrap(admin, prefix, LOGIN_PASSWORD)
    with psycopg.connect(admin) as conn:
        rows = conn.execute(
            "select rolname, rolbypassrls, rolcreaterole, rolsuper, rolcreatedb, rolreplication"
            " from pg_roles where starts_with(rolname, %s)",
            (prefix + "_",),
        ).fetchall()
    assert rows
    for row in rows:
        assert row[1:] == (False, False, False, False, False), row[0]


def test_create_workspace_refuses_orphan_role(fresh):
    admin, _, prefix = fresh
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'create role "{names.ws_role(prefix, "orphan")}" nologin')
    with pytest.raises(ValueError, match="workspace role exists"):
        migrate.create_workspace(admin, prefix, "orphan")


def _tables(admin, schema):
    with psycopg.connect(admin) as conn:
        return {
            r[0]
            for r in conn.execute(
                "select table_name from information_schema.tables where table_schema = %s",
                (schema,),
            )
        }


def test_upgrade_is_idempotent_and_adds_tables(fresh):
    admin, login, prefix = fresh
    migrate.create_workspace(admin, prefix, "alpha")
    assert migrate.upgrade(admin, prefix) == ["alpha"]
    assert migrate.upgrade(admin, prefix) == ["alpha"]
    assert {"general_queue", "general_origin"} <= _tables(admin, "control")
    assert {"generations", "merge_flags", "conflicts"} <= _tables(admin, "ws_alpha")
    from treant.db import Database

    database = Database(login, prefix)
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), database.ws_tx("alpha") as conn:
            conn.execute("select * from control.general_queue").fetchall()
        with pytest.raises(psycopg.errors.InsufficientPrivilege), database.ws_tx("alpha") as conn:
            conn.execute("select * from control.general_origin").fetchall()
        for stmt in ("update control.audit set actor = 'x'", "delete from control.audit"):
            for tx in (database.control_tx, database.admin_tx):
                with pytest.raises(psycopg.errors.InsufficientPrivilege), tx() as conn:
                    conn.execute(stmt)
        with database.ws_tx("alpha") as conn:
            conn.execute("select wait_concept from ws_alpha.facts").fetchall()
    finally:
        database.close()


def test_upgrade_brings_old_workspace_up_to_date(fresh, monkeypatch):
    admin, _login, prefix = fresh
    real_upgrade = migrate.upgrade
    monkeypatch.setattr(migrate, "upgrade", lambda *a, **k: [])
    migrate.create_workspace(admin, prefix, "old")
    assert "generations" not in _tables(admin, "ws_old")
    monkeypatch.setattr(migrate, "upgrade", real_upgrade)
    assert real_upgrade(admin, prefix) == ["old"]
    assert "generations" in _tables(admin, "ws_old")


def _insert_privileges(admin, role, table):
    """(table-level insert, {columns with insert}) of ``role`` on ``control.<table>``."""
    with psycopg.connect(admin) as conn:
        whole = conn.execute(
            "select has_table_privilege(%s, %s, 'INSERT')", (role, f"control.{table}")
        ).fetchone()[0]
        cols = {
            r[0]
            for r in conn.execute(
                "select column_name from information_schema.column_privileges"
                " where grantee = %s and table_schema = 'control' and table_name = %s"
                " and privilege_type = 'INSERT'",
                (role, table),
            )
        }
    return whole, cols


_JOB_COLS = {"workspace", "kind", "key", "payload", "run_after"}


def test_queue_roles_insert_only_the_queue_columns(fresh):
    admin, login, prefix = fresh
    migrate.create_workspace(admin, prefix, "alpha")
    ws = names.ws_role(prefix, "alpha")
    publisher = names.role(prefix, "publisher")
    assert _insert_privileges(admin, ws, "jobs") == (False, _JOB_COLS)
    assert _insert_privileges(admin, ws, "mistakes") == (False, {"workspace", "text"})
    assert _insert_privileges(admin, publisher, "jobs") == (False, _JOB_COLS)
    from treant.db import Database

    database = Database(login, prefix)
    try:
        with database.ws_tx("alpha") as conn:  # the enqueue and report_mistake shapes still work
            conn.execute(
                "insert into control.jobs (workspace, kind, key, payload, run_after)"
                " values ('alpha', 'embed', 'k1', '{}', now()) on conflict do nothing"
            )
            conn.execute("insert into control.mistakes (workspace, text) values ('alpha', 'x')")
        for stmt in (
            (
                "insert into control.jobs (workspace, kind, key, status) values"
                " ('alpha', 'embed', 'k2', 'running')"
            ),
            "insert into control.jobs (id, workspace, kind, key) values (1, 'alpha', 'e', 'k3')",
            (
                "insert into control.jobs (workspace, kind, key, attempts) values"
                " ('alpha', 'embed', 'k4', 99)"
            ),
            "insert into control.mistakes (id, workspace, text) values (1, 'alpha', 'x')",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), database.ws_tx("alpha") as c:
                c.execute(stmt)
        with pytest.raises(psycopg.errors.InsufficientPrivilege), database.publisher_tx() as c:
            c.execute(
                "insert into control.jobs (workspace, kind, key, status) values"
                " ('alpha', 'gate', 'k5', 'dead')"
            )
    finally:
        database.close()


def test_upgrade_narrows_old_whole_table_insert_grants(fresh):
    admin, _, prefix = fresh
    migrate.create_workspace(admin, prefix, "alpha")
    ws = names.ws_role(prefix, "alpha")
    with psycopg.connect(admin, autocommit=True) as conn:  # the grants of an older release
        conn.execute(f'grant insert on control.jobs, control.mistakes to "{ws}"')
        conn.execute(f'grant insert on control.jobs to "{names.role(prefix, "publisher")}"')
    assert _insert_privileges(admin, ws, "jobs")[0] is True
    migrate.upgrade(admin, prefix)
    assert _insert_privileges(admin, ws, "jobs") == (False, _JOB_COLS)
    assert _insert_privileges(admin, ws, "mistakes") == (False, {"workspace", "text"})
    assert _insert_privileges(admin, names.role(prefix, "publisher"), "jobs") == (False, _JOB_COLS)


def test_upgrade_rehardens_workspace_roles(fresh):
    admin, _, prefix = fresh
    migrate.create_workspace(admin, prefix, "alpha")
    ws = names.ws_role(prefix, "alpha")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'alter role "{ws}" with login bypassrls createdb createrole replication')
    migrate.upgrade(admin, prefix)
    with psycopg.connect(admin) as conn:
        row = conn.execute(
            "select rolcanlogin, rolbypassrls, rolcreatedb, rolcreaterole, rolreplication,"
            " rolsuper from pg_roles where rolname = %s",
            (ws,),
        ).fetchone()
    assert row == (False,) * 6


def test_admin_functions_are_not_executable_by_public(fresh):
    admin, _, prefix = fresh
    migrate.create_workspace(admin, prefix, "alpha")
    owner = names.role(prefix, "admin")
    ws = names.ws_role(prefix, "alpha")
    with psycopg.connect(admin, autocommit=True) as conn:
        # a function left from before the default privileges were set
        conn.execute(
            f'alter default privileges for role "{owner}" grant execute on functions to public'
        )
        for schema in ("control", "general", "ws_alpha"):
            conn.execute(
                f"create function {schema}.old_fn() returns int language sql as $$select 1$$"
            )
            conn.execute(f'alter function {schema}.old_fn() owner to "{owner}"')
    migrate.upgrade(admin, prefix)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'set role "{owner}"')
        conn.execute("create function control.new_fn() returns int language sql as $$select 1$$")
        conn.execute("reset role")
        for fn in ("control.old_fn()", "general.old_fn()", "ws_alpha.old_fn()", "control.new_fn()"):
            assert (
                conn.execute(
                    "select has_function_privilege(%s, %s, 'EXECUTE')", (ws, fn)
                ).fetchone()[0]
                is False
            ), fn


def test_upgrade_grants_select_on_later_general_tables(fresh):
    admin, login, prefix = fresh
    migrate.create_workspace(admin, prefix, "alpha")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'set role "{names.role(prefix, "admin")}"')
        conn.execute("create table general.later (x int)")
    migrate.upgrade(admin, prefix)
    from treant.db import Database

    database = Database(login, prefix)
    try:
        with database.ws_tx("alpha") as conn:
            assert conn.execute("select count(*) from general.later").fetchone()[0] == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege), database.ws_tx("alpha") as c:
            c.execute("insert into general.later values (1)")
    finally:
        database.close()
