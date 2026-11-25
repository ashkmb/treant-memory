import uuid

import psycopg
import pytest
from psycopg import sql

from treant import names
from treant.db import Database


def _insert_general_concept(db: Database, name: str) -> uuid.UUID:
    with db.publisher_tx() as c:
        return c.execute(
            "insert into general.nodes (kind, type, name) values ('concept', 'tool', %s)"
            " returning id",
            (name,),
        ).fetchone()[0]


def test_tx_runs_as_the_requested_role(db, prefix):
    with db.ws_tx("alpha") as c:
        assert c.execute("select current_user").fetchone()[0] == names.ws_role(prefix, "alpha")
    for helper, kind in [
        (db.control_tx, "control"),
        (db.gate_tx, "gate"),
        (db.publisher_tx, "publisher"),
        (db.admin_tx, "admin"),
    ]:
        with helper() as c:
            assert c.execute("select current_user").fetchone()[0] == names.role(prefix, kind)


def test_ws_tx_rejects_invalid_workspace_name(db):
    with pytest.raises(ValueError, match="invalid workspace name"), db.ws_tx("Alpha; drop x"):
        pass


def test_workspace_role_cannot_read_other_workspace(db):
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.ws_tx("alpha") as c:
        c.execute("select * from ws_beta.nodes")


def test_workspace_role_cannot_write_other_workspace(db):
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.ws_tx("alpha") as c:
        c.execute("insert into ws_beta.targets (kind, value) values ('kube_context', 'x')")


def test_workspace_role_has_dml_on_own_schema(db):
    with db.ws_tx("alpha") as c:
        node = c.execute(
            "insert into ws_alpha.nodes (kind, type, name) values ('instance', 'host', 'box')"
            " returning id"
        ).fetchone()[0]
        c.execute("update ws_alpha.nodes set name = 'box2' where id = %s", (node,))
        assert (
            c.execute("select name from ws_alpha.nodes where id = %s", (node,)).fetchone()[0]
            == "box2"
        )
        c.execute("delete from ws_alpha.nodes where id = %s", (node,))


def test_login_role_has_no_rights_without_set_role(db):
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.pool.connection() as c:
        c.execute("select * from control.tokens")


def test_login_role_cannot_read_workspace_without_set_role(db):
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.pool.connection() as c:
        c.execute("select * from ws_alpha.nodes")


def test_pool_connection_has_no_role_after_tx(db):
    with db.ws_tx("alpha"):
        pass
    with db.pool.connection() as c:
        assert c.execute("select current_user = session_user").fetchone()[0] is True


def test_pool_reset_clears_session_level_role_and_settings(login_dsn, prefix, db):
    single = Database(login_dsn, prefix, min_size=1, max_size=1)
    try:
        with single.pool.connection() as c:
            c.execute(sql.SQL("set role {}").format(sql.Identifier(names.ws_role(prefix, "alpha"))))
            c.execute("set statement_timeout = 1234")
            pid = c.info.backend_pid
        with single.pool.connection() as c:
            assert c.info.backend_pid == pid  # same backend reused
            assert c.execute("select current_user = session_user").fetchone()[0] is True
            assert c.execute("show statement_timeout").fetchone()[0] == "0"
    finally:
        single.close()


def test_role_is_dropped_after_failed_tx(login_dsn, prefix, db):
    single = Database(login_dsn, prefix, min_size=1, max_size=1)
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), single.ws_tx("alpha") as c:
            pid = c.info.backend_pid
            c.execute("select * from ws_beta.nodes")
        with single.pool.connection() as c:
            assert c.info.backend_pid == pid  # the same backend, handed out again
            assert c.execute("select current_user = session_user").fetchone()[0] is True
    finally:
        single.close()


def test_workspace_role_reads_general_but_cannot_write_it(db):
    concept = _insert_general_concept(db, "postgres")
    with db.ws_tx("alpha") as c:
        assert (
            c.execute("select name from general.nodes where id = %s", (concept,)).fetchone()[0]
            == "postgres"
        )
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.ws_tx("alpha") as c:
        c.execute("insert into general.nodes (kind, type, name) values ('concept', 'tool', 'x')")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.ws_tx("alpha") as c:
        c.execute("update general.nodes set name = 'x' where id = %s", (concept,))


def test_local_concept_must_refine_a_general_concept(db):
    concept = _insert_general_concept(db, "kubernetes")
    with db.ws_tx("alpha") as c:
        c.execute(
            "insert into ws_alpha.nodes (kind, type, name, refines)"
            " values ('local_concept', 'tool', 'our k8s', %s)",
            (concept,),
        )
    with pytest.raises(psycopg.errors.CheckViolation), db.ws_tx("alpha") as c:
        c.execute(
            "insert into ws_alpha.nodes (kind, type, name)"
            " values ('local_concept', 'tool', 'orphan')"
        )
    with pytest.raises(psycopg.errors.ForeignKeyViolation), db.ws_tx("alpha") as c:
        c.execute(
            "insert into ws_alpha.nodes (kind, type, name, refines)"
            " values ('local_concept', 'tool', 'dangling', %s)",
            (uuid.uuid4(),),
        )


def test_gate_reads_instance_names_but_not_facts(db):
    with db.ws_tx("alpha") as c:
        c.execute(
            "insert into ws_alpha.nodes (kind, type, name, aliases)"
            " values ('instance', 'cluster', 'gate-visible', array['gv'])"
        )
    with db.gate_tx() as c:
        rows = c.execute("select name, aliases from ws_alpha.instance_names").fetchall()
        assert ("gate-visible", ["gv"]) in rows
        assert "gate-visible" not in {
            r[0] for r in c.execute("select name from ws_beta.instance_names")
        }
    for table in ["ws_alpha.facts", "ws_alpha.nodes", "general.nodes", "control.tokens"]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), db.gate_tx() as c:
            c.execute(sql.SQL("select * from {}").format(sql.Identifier(*table.split("."))))


def test_instance_names_excludes_local_concepts(db):
    concept = _insert_general_concept(db, "terraform")
    with db.ws_tx("beta") as c:
        c.execute(
            "insert into ws_beta.nodes (kind, type, name, refines)"
            " values ('local_concept', 'tool', 'beta-local', %s)",
            (concept,),
        )
    with db.gate_tx() as c:
        assert (
            c.execute(
                "select count(*) from ws_beta.instance_names where name = 'beta-local'"
            ).fetchone()[0]
            == 0
        )


def test_audit_is_insert_only(db):
    with db.control_tx() as c:
        c.execute(
            "insert into control.audit (actor, action, detail) values ('test', 'probe', '{}')"
        )
        assert (
            c.execute("select count(*) from control.audit where action = 'probe'").fetchone()[0]
            >= 1
        )
    for stmt in [
        "update control.audit set actor = 'x'",
        "delete from control.audit",
        "truncate control.audit",
    ]:
        for helper in (db.control_tx, db.admin_tx):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), helper() as c:
                c.execute(stmt)


def test_workspace_role_enqueues_jobs_only_for_itself(db):
    key = uuid.uuid4().hex
    with db.ws_tx("alpha") as c:
        for _ in range(2):  # second insert is a no-op, needs no SELECT
            c.execute(
                "insert into control.jobs (workspace, kind, key, payload)"
                " values ('alpha', 'embed', %s, '{}') on conflict do nothing",
                (key,),
            )
        c.execute("insert into control.mistakes (workspace, text) values ('alpha', 'oops')")
    rls = pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security")
    with rls, db.ws_tx("alpha") as c:
        c.execute(
            "insert into control.jobs (workspace, kind, key, payload)"
            " values ('beta', 'embed', %s, '{}')",
            (key,),
        )
    rls = pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security")
    with rls, db.ws_tx("alpha") as c:
        c.execute("insert into control.mistakes (workspace, text) values ('beta', 'x')")
    with db.control_tx() as c:
        rows = c.execute(
            "select workspace, status, attempts from control.jobs where key = %s", (key,)
        ).fetchall()
        assert rows == [("alpha", "queued", 0)]


def test_workspace_role_reads_nothing_in_control(db):
    for table in ["jobs", "mistakes", "tokens", "workspaces", "audit", "canaries", "metrics"]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), db.ws_tx("alpha") as c:
            c.execute(sql.SQL("select * from control.{}").format(sql.Identifier(table)))
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.ws_tx("alpha") as c:
        c.execute(
            "insert into control.jobs (workspace, kind, key) values ('alpha', 'k', 'v')"
            " returning id"
        )


def test_control_role_sees_all_jobs_and_no_workspace_data(db):
    with db.ws_tx("beta") as c:
        c.execute(
            "insert into control.jobs (workspace, kind, key) values ('beta', 'embed', %s)",
            (uuid.uuid4().hex,),
        )
    with db.control_tx() as c:
        seen = {r[0] for r in c.execute("select distinct workspace from control.jobs")}
        assert "beta" in seen
        c.execute("update control.jobs set attempts = attempts + 1 where workspace = 'beta'")
    for table in ["ws_alpha.nodes", "general.nodes"]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), db.control_tx() as c:
            c.execute(sql.SQL("select * from {}").format(sql.Identifier(*table.split("."))))


def test_publisher_writes_general_only(db):
    concept = _insert_general_concept(db, "helm")
    with db.publisher_tx() as c:
        c.execute(
            "insert into general.facts (type, text, node_id) values ('fact', 'helm is a tool', %s)",
            (concept,),
        )
    for table in ["ws_alpha.nodes", "control.jobs"]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), db.publisher_tx() as c:
            c.execute(sql.SQL("select * from {}").format(sql.Identifier(*table.split("."))))


def test_admin_reaches_everything(db):
    with db.admin_tx() as c:
        for table in [
            "ws_alpha.nodes",
            "ws_beta.facts",
            "general.nodes",
            "control.tokens",
            "control.jobs",
            "control.audit",
        ]:
            c.execute(sql.SQL("select count(*) from {}").format(sql.Identifier(*table.split("."))))
        c.execute("delete from control.jobs where kind = 'never'")
        assert (
            c.execute("select rolsuper from pg_roles where rolname = current_user").fetchone()[0]
            is False
        )


def test_embeddings_are_768_dims(db):
    with db.ws_tx("alpha") as c:
        c.execute(
            "insert into ws_alpha.embeddings (item_kind, item_id, vec) values ('node', %s, %s)",
            (uuid.uuid4(), "[" + ",".join(["0.1"] * 768) + "]"),
        )
    with pytest.raises(psycopg.errors.DataException), db.ws_tx("alpha") as c:
        c.execute(
            "insert into ws_alpha.embeddings (item_kind, item_id, vec) values ('node', %s, %s)",
            (uuid.uuid4(), "[0.1,0.2]"),
        )


def test_pool_reset_discards_temp_tables(login_dsn, prefix, db):
    single = Database(login_dsn, prefix, min_size=1, max_size=1)
    try:
        with single.ws_tx("alpha") as c:
            c.execute("create temp table x (a int)")
            pid = c.info.backend_pid
        with single.pool.connection() as c:
            assert c.info.backend_pid == pid
            assert c.execute("select to_regclass('pg_temp.x')").fetchone()[0] is None
    finally:
        single.close()
