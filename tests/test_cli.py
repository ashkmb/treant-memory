import psycopg
import pytest
from psycopg import sql

from treant import cli
from treant.db import Database
from treant.errors import InvalidInput


def test_cli_token_create_prints_once_and_audits(fresh, fresh_db, capsys):
    assert cli.main(["token", "create", "alpha", "--label", "laptop", "--actions", "read"]) == 0
    token = capsys.readouterr().out.strip()
    assert token.startswith("trt_") and token.count("_") >= 2
    secret = token.split("_", 2)[2]
    with fresh_db.control_tx() as conn:
        rows = conn.execute("SELECT actor, action, detail::text FROM control.audit").fetchall()
    assert any(a == "cli" and act == "token.create" for a, act, _ in rows)
    assert all(secret not in d for _, _, d in rows)
    assert cli.main(["token", "list", "alpha"]) == 0
    listing = capsys.readouterr().out
    assert "laptop" in listing and secret not in listing
    with fresh_db.control_tx() as conn:
        digest = conn.execute("SELECT secret_sha256 FROM control.tokens").fetchone()[0]
    assert digest not in listing


def test_workspace_archive_revokes_tokens(fresh, fresh_db, capsys):
    cli.main(["token", "create", "beta", "--label", "x", "--actions", "read,create"])
    cli.main(["token", "create", "beta", "--label", "y", "--actions", "read"])
    capsys.readouterr()
    assert cli.main(["workspace", "archive", "beta"]) == 0
    with fresh_db.control_tx() as conn:
        status = conn.execute("SELECT status FROM control.workspaces WHERE name='beta'").fetchone()
        live = conn.execute(
            "SELECT count(*) FROM control.tokens WHERE workspace='beta' AND revoked_at IS NULL"
        ).fetchone()[0]
        audit = conn.execute(
            "SELECT count(*) FROM control.audit WHERE action = 'workspace.archive'"
        ).fetchone()[0]
    assert status[0] == "archived" and live == 0 and audit == 1


def test_bootstrap_requires_login_password(fresh, monkeypatch, capsys):
    monkeypatch.delenv("TREANT_LOGIN_PASSWORD", raising=False)
    assert cli.main(["bootstrap"]) == 2
    assert "TREANT_LOGIN_PASSWORD" in capsys.readouterr().err


def test_bootstrap_and_workspace_create_need_no_embed_settings(fresh, monkeypatch, capsys):
    monkeypatch.delenv("TREANT_EMBED_URL")
    monkeypatch.delenv("TREANT_DATABASE_URL")
    monkeypatch.setenv("TREANT_LOGIN_PASSWORD", "login-test")
    assert cli.main(["bootstrap"]) == 0
    assert cli.main(["workspace", "create", "delta"]) == 0
    out = capsys.readouterr().out
    assert "login-test" not in out


def test_target_add_validates_kind_and_lists(fresh, fresh_db, capsys):
    assert cli.main(["target", "add", "alpha", "kube_context", "prod-eu"]) == 0
    assert cli.main(["target", "list", "alpha"]) == 0
    assert "kube_context\tprod-eu" in capsys.readouterr().out
    try:
        cli.main(["target", "add", "alpha", "bogus", "x"])
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("expected argparse to reject the kind")


def test_target_remove(fresh, fresh_db, capsys):
    assert cli.main(["target", "add", "alpha", "kube_context", "prod-eu"]) == 0
    assert cli.main(["target", "add", "alpha", "kube_context", "stage-eu"]) == 0
    assert cli.main(["target", "remove", "alpha", "kube_context", "prod-eu"]) == 0
    assert "target removed" in capsys.readouterr().out
    assert cli.main(["target", "list", "alpha"]) == 0
    listed = capsys.readouterr().out
    assert "prod-eu" not in listed and "kube_context\tstage-eu" in listed


def test_target_remove_missing_is_exit_1(fresh, fresh_db, capsys):
    assert cli.main(["target", "remove", "alpha", "kube_context", "nope"]) == 1
    assert "no such target" in capsys.readouterr().err


def test_isolation_test_exit_codes(fresh, fresh_db, capsys, monkeypatch):
    from fakes import FakeEmbedder

    monkeypatch.setattr(cli, "_embedder", lambda s: FakeEmbedder())
    assert cli.main(["canary", "plant", "alpha"]) == 0
    assert cli.main(["canary", "plant", "beta"]) == 0
    capsys.readouterr()
    assert cli.main(["isolation-test"]) == 1  # gamma has no canary
    assert "no_canary\tgamma\tgamma" in capsys.readouterr().out
    assert cli.main(["canary", "plant", "gamma"]) == 0
    capsys.readouterr()
    assert cli.main(["isolation-test"]) == 0
    assert capsys.readouterr().out.strip().splitlines() == [
        "workspaces: 3 pairs: 6 canaries: 3 faults: 0",
        "canary markers held in queue: 0",
    ]


def test_worker_loop_survives_errors_and_sleeps_when_idle(fresh_db, monkeypatch):
    from fakes import FakeEmbedder

    calls = []

    def boom(db, emb):
        calls.append(1)
        raise RuntimeError("secret detail")

    monkeypatch.setattr(cli.worker, "run_once", boom)
    sleeps = []
    cli.worker_loop(fresh_db, FakeEmbedder(), {}, max_iterations=3, sleep=sleeps.append)
    assert len(calls) == 3 and sleeps == [5, 5, 5]
    assert isinstance(fresh_db, Database)


def _revoke_audits(db):
    with db.control_tx() as conn:
        return conn.execute(
            "SELECT count(*) FROM control.audit WHERE action = 'token.revoke'"
        ).fetchone()[0]


def test_token_revoke_unknown_id_fails_without_audit(fresh, fresh_db, capsys):
    assert cli.main(["token", "revoke", "0123456789ab"]) == 1
    out = capsys.readouterr()
    assert "not found" in out.err and "revoked" not in out.out
    assert _revoke_audits(fresh_db) == 0
    assert cli.main(["token", "create", "alpha", "--label", "r", "--actions", "read"]) == 0
    token_id = capsys.readouterr().out.strip().split("_")[1]
    assert cli.main(["token", "revoke", token_id]) == 0
    assert f"token {token_id} revoked" in capsys.readouterr().out
    assert _revoke_audits(fresh_db) == 1
    assert cli.main(["token", "revoke", token_id]) == 1  # already revoked
    assert "not found" in capsys.readouterr().err
    assert _revoke_audits(fresh_db) == 1


def _count(db, sql_text, params=()):
    with db.control_tx() as conn:
        return conn.execute(sql_text, params).fetchone()[0]


def _exists(admin, schema=None, role=None):
    with psycopg.connect(admin) as conn:
        s = conn.execute(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (schema,)
        ).fetchone()
        r = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    return s is not None, r is not None


def test_canary_plant_if_missing_is_idempotent(fresh, fresh_db, capsys, monkeypatch):
    from fakes import FakeEmbedder

    monkeypatch.setattr(cli, "_embedder", lambda s: FakeEmbedder())
    assert cli.main(["canary", "plant", "alpha", "--if-missing"]) == 0
    assert "planted" in capsys.readouterr().out
    rows = _count(fresh_db, "SELECT count(*) FROM control.canaries WHERE workspace = 'alpha'")
    assert cli.main(["canary", "plant", "alpha", "--if-missing"]) == 0
    assert capsys.readouterr().out.strip() == "exists"
    assert (
        _count(fresh_db, "SELECT count(*) FROM control.canaries WHERE workspace = 'alpha'") == rows
    )
    assert cli.main(["canary", "list"]) == 0
    lines = capsys.readouterr().out.splitlines()

    assert [ln.split("\t")[0] for ln in lines] == ["alpha", "beta", "gamma"]
    assert all("canary-" not in ln for ln in lines)


def test_purge_requires_archived_and_confirm(fresh, fresh_db, capsys):
    _, _, prefix = fresh
    assert cli.main(["workspace", "purge", "beta", "--confirm", "beta"]) == 1
    cli.main(["workspace", "archive", "beta"])
    assert cli.main(["workspace", "purge", "beta", "--confirm", "other"]) == 1
    capsys.readouterr()
    assert _exists(fresh[0], "ws_beta", f"{prefix}_ws_beta") == (True, True)
    assert _count(fresh_db, "SELECT count(*) FROM control.workspaces WHERE name = 'beta'") == 1
    assert (
        _count(fresh_db, "SELECT count(*) FROM control.audit WHERE action = 'workspace.purge'") == 0
    )


def test_purge_drops_schema_role_and_control_rows_keeps_audit(fresh, fresh_db, capsys, monkeypatch):
    from fakes import FakeEmbedder

    monkeypatch.setattr(cli, "_embedder", lambda s: FakeEmbedder())
    admin, _, prefix = fresh
    cli.main(["canary", "plant", "beta"])
    cli.main(["token", "create", "beta", "--label", "x", "--actions", "read"])

    with fresh_db.ws_tx("beta") as conn:
        conn.execute("INSERT INTO control.mistakes (workspace, text) VALUES ('beta', 'm')")
    with fresh_db.control_tx() as conn:
        conn.execute("INSERT INTO control.metrics (name, workspace, value) VALUES ('m','beta',1)")
    with psycopg.connect(admin) as conn:
        general_before = conn.execute("SELECT count(*) FROM general.facts").fetchone()[0]
    cli.main(["workspace", "archive", "beta"])
    capsys.readouterr()
    assert cli.main(["workspace", "purge", "beta", "--confirm", "beta"]) == 0
    assert capsys.readouterr().out.strip() == "purged beta"
    assert _exists(admin, "ws_beta", f"{prefix}_ws_beta") == (False, False)
    with fresh_db.control_tx() as conn:
        for table, col in (
            ("workspaces", "name"), ("tokens", "workspace"), ("jobs", "workspace"),
            ("canaries", "workspace"), ("mistakes", "workspace"), ("metrics", "workspace"),
        ):  # fmt: skip
            n = conn.execute(
                sql.SQL("SELECT count(*) FROM control.{} WHERE {} = 'beta'").format(
                    sql.Identifier(table), sql.Identifier(col)
                )
            ).fetchone()[0]
            assert n == 0, table
        actions = [
            r[0]
            for r in conn.execute(
                "SELECT action FROM control.audit WHERE detail::text LIKE '%beta%' ORDER BY id"
            )
        ]
    assert [a for a in actions if a.startswith("workspace.")] == [
        "workspace.archive",
        "workspace.purge",
    ]
    with psycopg.connect(admin) as conn:
        assert conn.execute("SELECT count(*) FROM general.facts").fetchone()[0] == general_before
    # other workspaces still work: own schema, general reads and their queue policies
    assert _exists(admin, "ws_alpha", f"{prefix}_ws_alpha") == (True, True)
    with fresh_db.ws_tx("alpha") as conn:
        conn.execute("SELECT count(*) FROM ws_alpha.facts").fetchone()
        conn.execute("SELECT count(*) FROM general.facts").fetchone()
        conn.execute(
            "INSERT INTO control.jobs (workspace, kind, key) VALUES ('alpha', 'k', 'after-purge')"
        )
        conn.execute("INSERT INTO control.mistakes (workspace, text) VALUES ('alpha', 'm')")
    with psycopg.connect(admin) as conn:
        policies = {
            r[0]
            for r in conn.execute("SELECT policyname FROM pg_policies WHERE schemaname = 'control'")
        }
    assert {"ws_alpha_insert", "ws_gamma_insert"} <= policies
    assert "ws_beta_insert" not in policies


def test_purge_verify_requires_repo_gone(fresh, fresh_db, capsys):
    cli.main(["workspace", "archive", "gamma"])
    cli.main(["workspace", "purge", "gamma", "--confirm", "gamma"])
    assert cli.main(["workspace", "purge-verify", "gamma", "--repo-gone", "no"]) == 1
    with fresh_db.control_tx() as conn:
        detail = conn.execute(
            "SELECT detail FROM control.audit WHERE action = 'workspace.purge.verified' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert detail == {
        "workspace": "gamma",
        "schema_gone": True,
        "role_gone": True,
        "repo_gone": False,
    }
    assert cli.main(["workspace", "purge-verify", "gamma", "--repo-gone", "yes"]) == 0


def test_schemas_lists_control_general_and_workspaces(fresh, fresh_db, capsys):
    cli.main(["workspace", "archive", "beta"])
    capsys.readouterr()
    assert cli.main(["schemas"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "control", "general", "ws_alpha", "ws_beta", "ws_gamma",
    ]  # fmt: skip


def test_quarantine_list_release_drop(fresh, fresh_db, capsys):
    from treant import jobs  # noqa: F401
    from treant.sessions import save_session
    from treant.tokens import TokenContext

    ctx = TokenContext("c1c1c1c1c1c1", "alpha", frozenset({"create"}))
    r1 = save_session(fresh_db, ctx, "q1", {"journal": "secret-ish words"}, unsure=True)
    r2 = save_session(fresh_db, ctx, "q2", {"journal": "other words"}, unsure=True)
    with fresh_db.ws_tx("alpha") as conn:
        ids = {
            n: str(i)
            for n, i in conn.execute(
                "SELECT s.client_session_id, r.id FROM ws_alpha.revisions r "
                "JOIN ws_alpha.sessions s ON s.id = r.session_id"
            )
        }
    assert (r1.revision, r2.revision) == (1, 1)
    capsys.readouterr()
    assert cli.main(["quarantine", "list", "--workspace", "alpha"]) == 0
    out = capsys.readouterr().out
    assert ids["q1"] in out and ids["q2"] in out and "words" not in out
    assert cli.main(["quarantine", "release", "alpha", ids["q1"]]) == 0
    assert cli.main(["quarantine", "drop", "alpha", ids["q2"]]) == 0
    assert cli.main(["quarantine", "drop", "alpha", ids["q2"]]) == 1
    capsys.readouterr()
    assert cli.main(["quarantine", "list"]) == 0
    assert capsys.readouterr().out == ""
    with fresh_db.control_tx() as conn:
        acts = [r[0] for r in conn.execute("SELECT action FROM control.audit")]
    assert "quarantine.release" in acts and "quarantine.drop" in acts


def test_quarantine_unknown_workspace_and_symmetric_messages(fresh, fresh_db, capsys):
    from treant.sessions import save_session
    from treant.tokens import TokenContext

    ctx = TokenContext("c1c1c1c1c1c1", "alpha", frozenset({"create"}))
    save_session(fresh_db, ctx, "q9", {"journal": "x"}, unsure=True)
    with fresh_db.ws_tx("alpha") as conn:
        rid = str(conn.execute("SELECT id FROM ws_alpha.revisions").fetchone()[0])
    capsys.readouterr()
    for sub in ("release", "drop"):
        assert cli.main(["quarantine", sub, "nosuchws", rid]) == 1
        assert "not found or not quarantined" in capsys.readouterr().err
    assert cli.main(["quarantine", "list", "--workspace", "nosuchws"]) == 1
    capsys.readouterr()
    assert cli.main(["quarantine", "release", "alpha", rid]) == 0
    assert capsys.readouterr().out.strip() == f"revision {rid} released"
    save_session(fresh_db, ctx, "q10", {"journal": "y"}, unsure=True)
    with fresh_db.ws_tx("alpha") as conn:
        rid2 = str(
            conn.execute("SELECT id FROM ws_alpha.revisions WHERE status='quarantined'").fetchone()[
                0
            ]
        )
    assert cli.main(["quarantine", "drop", "alpha", rid2]) == 0
    assert capsys.readouterr().out.strip() == f"revision {rid2} dropped"


# --- owner commands for the general queue, merges, conflicts, jobs -------------------------


def _queue_row(db, ws, item, status="pending", rewrites=0, reasons=()):
    import uuid

    from psycopg.types.json import Jsonb

    from treant import apply

    source = item.get("name") if item["kind"] == "concept" else item["text"]
    item = {**item, "source_text": source}
    with db.publisher_tx() as conn:
        return conn.execute(
            "INSERT INTO control.general_queue "
            "(workspace, session_id, revision_id, item, item_key, status, rewrites, reasons) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                ws,
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                Jsonb(item),
                apply.item_key(item["kind"], source),
                status,
                rewrites,
                Jsonb(list(reasons)),
            ),
        ).fetchone()[0]


def _fact_item(text):
    return {"kind": "fact", "type": "lesson", "text": text, "concept": None}


def _audit_rows(db, action):
    with db.control_tx() as conn:
        return conn.execute(
            "SELECT actor, detail FROM control.audit WHERE action = %s", (action,)
        ).fetchall()


def test_general_list_prints_no_text(fresh, fresh_db, capsys):
    qid = _queue_row(
        fresh_db,
        "alpha",
        _fact_item("secret sentence here"),
        status="blocked",
        rewrites=2,
        reasons=[{"category": "term", "start": 0, "end": 3}],
    )
    _queue_row(fresh_db, "beta", _fact_item("other"), status="published")
    assert cli.main(["general", "list", "--status", "blocked"]) == 0
    out = capsys.readouterr().out
    cols = out.strip().split("\t")
    assert cols[:5] == [str(qid), "alpha", "blocked", "fact", "2"] and cols[5] == "term"
    assert "secret" not in out and out.count("\n") == 1
    assert cli.main(["general", "list", "--workspace", "beta"]) == 0
    assert "beta" in capsys.readouterr().out


def test_general_show_prints_text_and_reasons(fresh, fresh_db, capsys):
    qid = _queue_row(
        fresh_db,
        "alpha",
        _fact_item("the full text"),
        status="waiting_review",
        reasons=[{"category": "judge", "start": 0, "end": 0}],
    )
    assert cli.main(["general", "show", str(qid)]) == 0
    out = capsys.readouterr().out
    assert "the full text" in out and "judge" in out
    assert cli.main(["general", "show", "999999"]) == 1


def test_approve_shows_rule_findings_and_publishes(fresh, fresh_db, capsys):
    from treant import graph

    with fresh_db.ws_tx("beta") as conn:
        graph.add_node(conn, "beta", "instance", "cluster", "zorbulon-prod")
    qid = _queue_row(fresh_db, "alpha", _fact_item("restart zorbulon-prod nightly"), "blocked")
    assert cli.main(["general", "approve", str(qid), "--yes"]) == 0
    out = capsys.readouterr().out
    assert "name 8-21" in out and "zorbulon" not in out
    assert "published" in out
    with fresh_db.control_tx() as conn:
        status, pid = conn.execute(
            "SELECT status, published_id::text FROM control.general_queue WHERE id = %s", (qid,)
        ).fetchone()
        origin = conn.execute(
            "SELECT count(*) FROM control.general_origin WHERE queue_id = %s", (qid,)
        ).fetchone()[0]
    assert status == "published" and pid and origin == 1
    rows = _audit_rows(fresh_db, "general.approve")
    assert rows == [("cli", {"queue_id": qid, "workspace": "alpha", "edited": False})]


def test_approve_with_text_edits_and_audits(fresh, fresh_db, capsys):
    qid = _queue_row(fresh_db, "alpha", _fact_item("original"), "waiting_review")
    assert cli.main(["general", "approve", str(qid), "--text", "edited words"]) == 0
    with fresh_db.publisher_tx() as conn:
        texts = [r[0] for r in conn.execute("SELECT text FROM general.facts").fetchall()]
    assert texts == ["edited words"]
    (row,) = _audit_rows(fresh_db, "general.approve")
    assert row[1]["edited"] is True and "edited words" not in str(row[1])


def test_approve_concept_rejects_text_and_closed_rows(fresh, fresh_db, capsys):
    concept = {"kind": "concept", "name": "signoz", "type": "tool", "aliases": []}
    qid = _queue_row(fresh_db, "alpha", concept, "blocked")
    assert cli.main(["general", "approve", str(qid), "--text", "x"]) == 1
    assert "concept" in capsys.readouterr().err
    done = _queue_row(fresh_db, "alpha", _fact_item("done already"), "published")
    assert cli.main(["general", "approve", str(done)]) == 1
    assert cli.main(["general", "approve", "999999"]) == 1
    assert _audit_rows(fresh_db, "general.approve") == []


def test_general_drop(fresh, fresh_db, capsys):
    qid = _queue_row(fresh_db, "alpha", _fact_item("drop me"), "blocked")
    assert cli.main(["general", "drop", str(qid)]) == 0
    with fresh_db.control_tx() as conn:
        status, decided = conn.execute(
            "SELECT status, decided_at FROM control.general_queue WHERE id = %s", (qid,)
        ).fetchone()
    assert status == "dropped" and decided is not None
    assert _audit_rows(fresh_db, "general.drop") == [
        ("cli", {"queue_id": qid, "workspace": "alpha"})
    ]
    pub = _queue_row(fresh_db, "alpha", _fact_item("kept"), "published")
    assert cli.main(["general", "drop", str(pub)]) == 1


def test_merges_and_conflicts_list(fresh, fresh_db, capsys):
    from treant import graph

    with fresh_db.ws_tx("alpha") as conn:
        a = graph.add_node(conn, "alpha", "instance", "cluster", "node-aaa")
        b = graph.add_node(conn, "alpha", "instance", "cluster", "node-bbb")
        f1 = graph.add_fact(conn, "alpha", "fact", "old fact text", a)
        f2 = graph.add_fact(conn, "alpha", "fact", "new fact text", a)
        conn.execute(
            "INSERT INTO ws_alpha.merge_flags (node_a, node_b, score) VALUES (%s, %s, 0.93)",
            (a, b),
        )
        conn.execute(
            "INSERT INTO ws_alpha.conflicts (new_fact, old_fact) VALUES (%s, %s)", (f2, f1)
        )
    assert cli.main(["merges", "list", "alpha"]) == 0
    out = capsys.readouterr().out
    assert a in out and b in out and "node-aaa" in out and "node-bbb" in out and "0.93" in out
    assert cli.main(["conflicts", "list", "alpha"]) == 0
    out = capsys.readouterr().out
    assert f1 in out and f2 in out and "old fact text" in out and "new fact text" in out
    assert cli.main(["merges", "list", "nope"]) == 1


def test_jobs_list_filters_by_status(fresh, fresh_db, capsys):
    from treant import jobs

    with fresh_db.control_tx() as conn:
        jobs.enqueue(conn, "alpha", "gate", "k1", {})
        jobs.enqueue(conn, "alpha", "distill", "k2", {})
        conn.execute(
            "UPDATE control.jobs SET status='dead', attempts=8, error_category='model_rejected' "
            "WHERE key='k2'"
        )
    assert cli.main(["jobs", "list", "--status", "dead"]) == 0
    out = capsys.readouterr().out
    cols = out.strip().split("\t")
    assert cols[1:] == ["alpha", "distill", "dead", "8", "model_rejected"] and out.count("\n") == 1
    assert cli.main(["jobs", "list"]) == 0
    assert capsys.readouterr().out.count("\n") == 2


def test_approve_audits_before_publishing_and_records_failure(fresh, fresh_db, capsys, monkeypatch):
    qid = _queue_row(fresh_db, "alpha", _fact_item("some text"), "blocked")
    seen = {}

    def boom(db, queue_id, text, actor):
        seen["audited"] = [r[0] for r in _audit_rows(fresh_db, "general.approve")]
        raise InvalidInput("queue item is not open")

    monkeypatch.setattr(cli.gate, "publish", boom)
    assert cli.main(["general", "approve", str(qid)]) == 1
    assert seen["audited"] == ["cli"]  # the audit row existed before publish ran
    detail = {"queue_id": qid, "workspace": "alpha", "edited": False}
    assert _audit_rows(fresh_db, "general.approve") == [("cli", detail)]
    assert _audit_rows(fresh_db, "general.approve_failed") == [("cli", detail)]


def test_approve_dropped_row_is_refused(fresh, fresh_db, capsys):
    qid = _queue_row(fresh_db, "alpha", _fact_item("gone"), "dropped")
    assert cli.main(["general", "approve", str(qid)]) == 1
    assert "not open" in capsys.readouterr().err
    assert _audit_rows(fresh_db, "general.approve") == []


def test_double_approve_publishes_once(fresh, fresh_db, capsys):
    qid = _queue_row(fresh_db, "alpha", _fact_item("only once"), "blocked")
    assert cli.main(["general", "approve", str(qid)]) == 0
    capsys.readouterr()
    assert cli.main(["general", "approve", str(qid)]) == 1
    assert "not open" in capsys.readouterr().err
    with fresh_db.publisher_tx() as conn:
        n = conn.execute("SELECT count(*) FROM general.facts WHERE text = 'only once'").fetchone()[
            0
        ]
    assert n == 1
    assert len(_audit_rows(fresh_db, "general.approve")) == 1


def test_failure_category_mappings():
    from treant.backends import BackendUnavailable
    from treant.embed import EmbedderUnavailable

    assert cli._failure_category("embed", EmbedderUnavailable("x")) == "embedder"
    assert cli._failure_category("distill", BackendUnavailable("x")) == "model_unavailable"
    assert cli._failure_category("gate", BackendUnavailable("x")) == "judge_unavailable"
    assert cli._failure_category("apply", RuntimeError("x")) == "internal"


def test_gate_stage_failure_uses_max_attempts_20(fresh_db, monkeypatch):
    from treant import jobs

    with fresh_db.control_tx() as conn:
        jobs.enqueue(conn, "alpha", "gate", "g1", {})
    seen = []
    real = cli.jobs.fail

    def spy(db, job, category, max_attempts=8):
        seen.append((category, max_attempts))
        return real(db, job, category, max_attempts=max_attempts)

    monkeypatch.setattr(cli.jobs, "fail", spy)

    def boom(job):
        raise RuntimeError("x")

    assert cli._run_job_stage(fresh_db, "gate", boom) is True
    assert seen == [("internal", 20)]


# --- final review fixes: approve safety, jobs retry, quarantine show, purge -----------------


def _queue_status(db, qid):
    with db.control_tx() as conn:
        return conn.execute(
            "SELECT status FROM control.general_queue WHERE id = %s", (qid,)
        ).fetchone()[0]


def test_approve_with_findings_needs_yes(fresh, fresh_db, capsys):
    from treant import graph

    with fresh_db.ws_tx("beta") as conn:
        graph.add_node(conn, "beta", "instance", "cluster", "quexil-prod")
    qid = _queue_row(fresh_db, "alpha", _fact_item("restart quexil-prod nightly"), "blocked")
    assert cli.main(["general", "approve", str(qid)]) == 1
    captured = capsys.readouterr()
    assert "name 8-19" in captured.out and "quexil" not in captured.out + captured.err
    assert "--yes" in captured.err
    assert _queue_status(fresh_db, qid) == "blocked"
    assert _audit_rows(fresh_db, "general.approve") == []
    with fresh_db.publisher_tx() as conn:
        assert conn.execute(
            "SELECT count(*) FROM general.facts WHERE text LIKE '%quexil%'"
        ).fetchone() == (0,)
    # the final text is what the rules check: an edit that removes the name passes
    assert cli.main(["general", "approve", str(qid), "--text", "restart the cluster nightly"]) == 0
    assert _queue_status(fresh_db, qid) == "published"


def test_approve_text_override_with_findings_is_refused(fresh, fresh_db, capsys):
    qid = _queue_row(fresh_db, "alpha", _fact_item("generic wording"), "waiting_review")
    assert cli.main(["general", "approve", str(qid), "--text", "see ABC-1234 at 10.1.2.3"]) == 1
    assert _queue_status(fresh_db, qid) == "waiting_review"
    assert _audit_rows(fresh_db, "general.approve") == []


def test_approve_dry_run_prints_findings_and_publishes_nothing(fresh, fresh_db, capsys):
    from treant import graph

    with fresh_db.ws_tx("beta") as conn:
        graph.add_node(conn, "beta", "instance", "cluster", "vortaxa-prod")
    qid = _queue_row(fresh_db, "alpha", _fact_item("restart vortaxa-prod nightly"), "blocked")
    assert cli.main(["general", "approve", str(qid), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "name 8-20" in out and "published" not in out
    assert _queue_status(fresh_db, qid) == "blocked"
    assert _audit_rows(fresh_db, "general.approve") == []
    clean = _queue_row(fresh_db, "alpha", _fact_item("pin chart versions"), "blocked")
    assert cli.main(["general", "approve", str(clean), "--dry-run"]) == 0
    assert "no findings" in capsys.readouterr().out
    assert _queue_status(fresh_db, clean) == "blocked"


def _job_row(db, jid):
    with db.control_tx() as conn:
        return conn.execute(
            "SELECT status, attempts, error_category, lease_id, lease_until, "
            "run_after <= now() FROM control.jobs WHERE id = %s",
            (jid,),
        ).fetchone()


def _make_dead(db, ws, kind, key, category="model_bad_reply"):
    from treant import jobs

    with db.control_tx() as conn:
        jobs.enqueue(conn, ws, kind, key, {})
        return conn.execute(
            "UPDATE control.jobs SET status = 'dead', attempts = 3, error_category = %s, "
            "lease_id = gen_random_uuid(), lease_until = now(), "
            "run_after = now() + interval '1 day' WHERE workspace = %s AND kind = %s AND key = %s "
            "RETURNING id",
            (category, ws, kind, key),
        ).fetchone()[0]


def test_jobs_retry_one_and_dead_by_filter(fresh, fresh_db, capsys):
    one = _make_dead(fresh_db, "alpha", "distill", "r1")
    two = _make_dead(fresh_db, "alpha", "distill", "r2")
    other_kind = _make_dead(fresh_db, "alpha", "apply", "r3")
    other_ws = _make_dead(fresh_db, "beta", "distill", "r4")
    assert cli.main(["jobs", "retry", str(one)]) == 0
    assert _job_row(fresh_db, one) == ("queued", 0, "model_bad_reply", None, None, True)
    assert _job_row(fresh_db, two)[0] == "dead"
    assert cli.main(["jobs", "retry", "--status", "dead", "--kind", "distill", "--workspace",
                     "alpha"]) == 0  # fmt: skip
    assert _job_row(fresh_db, two)[:2] == ("queued", 0)
    assert _job_row(fresh_db, other_kind)[0] == "dead"
    assert _job_row(fresh_db, other_ws)[0] == "dead"
    assert cli.main(["jobs", "retry", "--status", "dead"]) == 0
    assert _job_row(fresh_db, other_kind)[0] == _job_row(fresh_db, other_ws)[0] == "queued"
    out = capsys.readouterr().out
    assert "retried 1" in out and "retried 2" in out
    details = [d for _, d in _audit_rows(fresh_db, "jobs.retry")]
    assert details == [
        {"ids": [one], "count": 1},
        {"ids": [two], "count": 1},
        {"ids": sorted([other_kind, other_ws]), "count": 2},
    ]


def test_jobs_retry_refuses_running_unknown_and_no_target(fresh, fresh_db, capsys):
    from treant import jobs

    with fresh_db.control_tx() as conn:
        jobs.enqueue(conn, "alpha", "distill", "run1", {})
    running = jobs.claim(fresh_db, {"distill"})
    assert cli.main(["jobs", "retry", str(running.id)]) == 1
    assert _job_row(fresh_db, running.id)[0] == "running"
    assert cli.main(["jobs", "retry", "99999999"]) == 1
    assert cli.main(["jobs", "retry"]) == 2
    assert _audit_rows(fresh_db, "jobs.retry") == []


def test_jobs_retry_gate_job_of_decided_row_is_a_noop_run(fresh, fresh_db, capsys):
    from treant import gate, jobs

    qid = _queue_row(fresh_db, "alpha", _fact_item("decided meanwhile"), "dropped")
    jid = _make_dead(fresh_db, "alpha", "gate", str(qid), "judge_rejected")
    with fresh_db.control_tx() as conn:
        conn.execute("UPDATE control.jobs SET payload = %s WHERE id = %s",
                     (psycopg.types.json.Jsonb({"queue_id": qid}), jid))  # fmt: skip
    assert cli.main(["jobs", "retry", str(jid)]) == 0
    job = jobs.claim(fresh_db, {"gate"})
    assert job.id == jid
    assert gate.handle(fresh_db, {}, job) == "stale"
    assert _job_row(fresh_db, jid)[0] == "done"
    assert _queue_status(fresh_db, qid) == "dropped"


def test_stage_failures_use_their_attempt_caps(fresh_db, monkeypatch):
    from treant import jobs
    from treant.backends import BackendBadReply

    seen = []
    real = cli.jobs.fail

    def spy(db, job, category, max_attempts=8):
        seen.append((job.kind, category, max_attempts))
        return real(db, job, category, max_attempts=max_attempts)

    monkeypatch.setattr(cli.jobs, "fail", spy)

    def boom(job):
        raise RuntimeError("x")

    def bad(job):
        raise BackendBadReply("x")

    for kind in ("distill", "apply"):
        with fresh_db.control_tx() as conn:
            jobs.enqueue(conn, "alpha", kind, f"cap-{kind}", {})
        assert cli._run_job_stage(fresh_db, kind, boom) is True
    with fresh_db.control_tx() as conn:
        jobs.enqueue(conn, "alpha", "distill", "cap-bad", {})
    assert cli._run_job_stage(fresh_db, "distill", bad) is True
    assert seen == [
        ("distill", "internal", 12),
        ("apply", "internal", 12),
        ("distill", "model_bad_reply", 3),
    ]


def test_quarantine_show_prints_sections(fresh, fresh_db, capsys):
    import json

    from treant.sessions import save_session
    from treant.tokens import TokenContext

    ctx = TokenContext("c1c1c1c1c1c1", "alpha", frozenset({"create"}))
    save_session(fresh_db, ctx, "qs1", {"journal": "held words", "lessons": ["l1"]}, unsure=True)
    save_session(fresh_db, ctx, "qs2", {"journal": "stored words"})
    with fresh_db.ws_tx("alpha") as conn:
        ids = {
            n: str(i)
            for n, i in conn.execute(
                "SELECT s.client_session_id, r.id FROM ws_alpha.revisions r "
                "JOIN ws_alpha.sessions s ON s.id = r.session_id"
            )
        }
    capsys.readouterr()
    assert cli.main(["quarantine", "show", "alpha", ids["qs1"]]) == 0
    assert json.loads(capsys.readouterr().out) == {"journal": "held words", "lessons": ["l1"]}
    assert cli.main(["quarantine", "show", "alpha", ids["qs2"]]) == 1  # not quarantined
    assert cli.main(["quarantine", "show", "beta", ids["qs1"]]) == 1
    assert cli.main(["quarantine", "show", "nosuchws", ids["qs1"]]) == 1
    assert cli.main(["quarantine", "show", "alpha", "not-a-uuid"]) == 1
    assert "words" not in capsys.readouterr().err


def test_purge_removes_queue_and_session_head_rows_keeps_origin(fresh, fresh_db, capsys):
    import uuid

    admin, _, _ = fresh
    qid = _queue_row(fresh_db, "beta", _fact_item("queued for beta"), "published")
    session = str(uuid.uuid4())
    with fresh_db.publisher_tx() as conn:
        conn.execute(
            "INSERT INTO control.general_session_head (workspace, session_id, n) "
            "VALUES ('beta', %s, 2)",
            (session,),
        )
        conn.execute(
            "INSERT INTO control.general_origin "
            "(item_kind, item_id, workspace, session_id, revision_id, queue_id) "
            "VALUES ('fact', %s, 'beta', %s, %s, %s)",
            (str(uuid.uuid4()), session, str(uuid.uuid4()), qid),
        )
    alpha_q = _queue_row(fresh_db, "alpha", _fact_item("queued for alpha"))
    cli.main(["workspace", "archive", "beta"])
    capsys.readouterr()
    assert cli.main(["workspace", "purge", "beta", "--confirm", "beta"]) == 0
    with psycopg.connect(admin) as conn:
        counts = [
            conn.execute(q).fetchone()[0]
            for q in (
                "SELECT count(*) FROM control.general_queue WHERE workspace = 'beta'",
                "SELECT count(*) FROM control.general_session_head WHERE workspace = 'beta'",
                "SELECT count(*) FROM control.general_origin WHERE workspace = 'beta'",
            )
        ]
        alpha_left = conn.execute(
            "SELECT count(*) FROM control.general_queue WHERE id = %s", (alpha_q,)
        ).fetchone()[0]
    assert counts == [0, 0, 1] and alpha_left == 1


def test_isolation_test_writes_metrics(fresh, fresh_db, monkeypatch):
    from fakes import FakeEmbedder

    monkeypatch.setattr(cli, "_embedder", lambda s: FakeEmbedder())
    for w in ("alpha", "beta"):
        assert cli.main(["canary", "plant", w]) == 0
    assert cli.main(["isolation-test"]) == 1  # gamma has no canary: 1 fault
    with fresh_db.admin_tx() as c:
        rows = c.execute(
            "SELECT name, value, workspace FROM control.metrics WHERE name LIKE 'isolation.%' "
            "ORDER BY at, name DESC"
        ).fetchall()
    assert rows == [("isolation.faults", 1, None), ("isolation.checks", 6, None)]
    assert cli.main(["canary", "plant", "gamma"]) == 0
    assert cli.main(["isolation-test"]) == 0
    with fresh_db.admin_tx() as c:
        last = c.execute(
            "SELECT value FROM control.metrics WHERE name = 'isolation.faults' ORDER BY at DESC LIMIT 1"
        ).fetchone()
    assert last == (0,)


def test_general_list_reads_no_similar_hints(fresh, fresh_db, queued_item, monkeypatch, capsys):
    from treant import browse

    def boom(*a, **k):
        raise AssertionError("hints computed")

    monkeypatch.setattr(browse, "_similar_hints", boom)
    assert cli.main(["general", "list"]) == 0
    assert f"{queued_item}\talpha\twaiting_review" in capsys.readouterr().out


def test_archived_workspace_settings_and_targets_refused(fresh, fresh_db, capsys):
    cli.main(["workspace", "archive", "gamma"])
    capsys.readouterr()
    assert cli.main(["target", "add", "gamma", "git_host", "h"]) == 1
    assert capsys.readouterr().err.strip() == "error: workspace not available"


def test_purge_negative_cases(fresh, fresh_db, capsys):
    cli.main(["workspace", "archive", "beta"])
    assert cli.main(["workspace", "purge", "beta", "--confirm", "beta"]) == 0
    assert cli.main(["workspace", "purge", "beta", "--confirm", "beta"]) == 1  # second purge
    for bad in ("general", "control", "../x", "nosuch"):
        assert cli.main(["workspace", "purge", bad, "--confirm", bad]) == 1, bad
    with psycopg.connect(fresh[0]) as conn:
        schemas = {
            r[0]
            for r in conn.execute(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name IN ('general', 'control')"
            )
        }
    assert schemas == {"general", "control"}
    assert (
        _count(fresh_db, "SELECT count(*) FROM control.audit WHERE action = 'workspace.purge'") == 1
    )


def test_purge_verify_refuses_a_never_purged_name(fresh, fresh_db, capsys):
    capsys.readouterr()
    assert cli.main(["workspace", "purge-verify", "neverwas", "--repo-gone", "yes"]) == 1
    assert "no purge recorded" in capsys.readouterr().err
    assert (
        _count(
            fresh_db,
            "SELECT count(*) FROM control.audit WHERE action = 'workspace.purge.verified'",
        )
        == 0
    )


def test_canary_if_missing_counts_an_archived_workspace_canary(
    fresh, fresh_db, capsys, monkeypatch
):
    from fakes import FakeEmbedder

    monkeypatch.setattr(cli, "_embedder", lambda s: FakeEmbedder())
    assert cli.main(["canary", "plant", "gamma"]) == 0
    cli.main(["workspace", "archive", "gamma"])
    capsys.readouterr()
    assert cli.main(["canary", "plant", "gamma", "--if-missing"]) == 0
    assert capsys.readouterr().out.strip() == "exists"
    assert (
        _count(
            fresh_db,
            "SELECT count(DISTINCT marker) FROM control.canaries WHERE workspace = 'gamma'",
        )
        == 1
    )


def test_purge_gives_up_on_a_held_lock(fresh, fresh_db, monkeypatch):
    from treant import migrate

    admin, _, prefix = fresh
    cli.main(["workspace", "archive", "beta"])
    monkeypatch.setattr(migrate, "PURGE_LOCK_TIMEOUT", "200ms")
    with psycopg.connect(admin) as holder:
        holder.execute("LOCK TABLE ws_beta.facts IN ACCESS SHARE MODE")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            migrate.purge_workspace(admin, prefix, "beta")
        assert cli.main(["workspace", "purge", "beta", "--confirm", "beta"]) == 1
    assert _exists(admin, "ws_beta", f"{prefix}_ws_beta") == (True, True)


def test_stale_list_show_keep_retract(fresh, fresh_db, capsys):
    from treant import graph, stale

    missing = [
        {"token": "old_name.py", "commit": "abc1234", "subject": "rename old_name to new_name"}
    ]
    with fresh_db.ws_tx("alpha") as conn:
        node = graph.add_node(conn, "alpha", "instance", "repo", "demo")
        text = "entry point is `old_name.py`"
        fact = graph.add_fact(conn, "alpha", "fact", text, node)
        fid = stale.flag(conn, "alpha", fact, "demo", "abc1234" + "0" * 33, missing, [])["flag_id"]
    assert cli.main(["stale", "list", "alpha"]) == 0
    line = capsys.readouterr().out.strip().split("\t")
    assert line[:5] == [str(fid), str(fact), "demo", "abc1234", "old_name.py"]
    assert len(line[5]) == len("2026-01-01 00:00")
    assert cli.main(["stale", "show", "alpha", str(fid)]) == 0
    out = capsys.readouterr().out
    assert text in out
    assert "missing: old_name.py (removed in abc1234: rename old_name to new_name)" in out
    assert cli.main(["stale", "keep", "alpha", str(fid)]) == 0
    assert cli.main(["stale", "keep", "alpha", str(fid)]) == 1
    assert "error: stale flag not found or resolved" in capsys.readouterr().err
    assert cli.main(["stale", "retract", "alpha", str(fid)]) == 1
    assert cli.main(["stale", "show", "alpha", "999999"]) == 1
    assert cli.main(["stale", "list", "general"]) == 1


def test_stale_command_arguments_need_no_database(capsys):
    for argv in (["stale"], ["stale", "keep", "alpha"], ["stale", "keep", "alpha", "x"]):
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
        assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        cli.main(["stale", "list", "--help"])
    assert e.value.code == 0
