import pytest

from treant import owner
from treant.errors import InvalidInput


def _audit_rows(db, action):
    with db.admin_tx() as c:
        return c.execute(
            "SELECT actor, detail FROM control.audit WHERE action = %s ORDER BY id", (action,)
        ).fetchall()


def test_every_owner_write_audits_its_actor(fresh_db):
    tok = owner.create_token(fresh_db, "dashboard:alice", "alpha", "lbl", {"read", "create"})
    tid = tok.split("_")[1]
    detail = _audit_rows(fresh_db, "token.create")[-1]
    assert detail[0] == "dashboard:alice"
    assert tok not in str(detail[1]) and tok.split("_", 2)[2] not in str(detail[1])
    assert detail[1] == {
        "token_id": tid,
        "workspace": "alpha",
        "label": "lbl",
        "actions": ["create", "read"],
    }
    owner.revoke_token(fresh_db, "dashboard:alice", tid)
    assert _audit_rows(fresh_db, "token.revoke")[-1] == ("dashboard:alice", {"token_id": tid})
    owner.add_target(fresh_db, "cli", "alpha", "git_host", "h.example")
    owner.remove_target(fresh_db, "dashboard:x", "alpha", "git_host", "h.example")
    assert _audit_rows(fresh_db, "target.add")[-1][0] == "cli"
    assert _audit_rows(fresh_db, "target.remove")[-1] == (
        "dashboard:x",
        {"workspace": "alpha", "kind": "git_host", "value": "h.example"},
    )


def test_revoke_unknown_token_is_invalid(fresh_db):
    with pytest.raises(InvalidInput, match="token not found"):
        owner.revoke_token(fresh_db, "cli", "0" * 12)
    assert _audit_rows(fresh_db, "token.revoke") == []


def test_remove_unknown_target_is_invalid(fresh_db):
    with pytest.raises(InvalidInput, match="no such target"):
        owner.remove_target(fresh_db, "cli", "alpha", "git_host", "nope")


def test_settings_change_returns_changed_keys_and_validates(fresh_db):
    assert owner.set_workspace_settings(fresh_db, "cli", "alpha", {"publish": "review"}) == [
        "publish"
    ]
    changed = owner.set_workspace_settings(
        fresh_db, "dashboard:a", "alpha", {"rules.terms": ["foo-bar"], "alias": "x"}
    )
    assert changed == ["alias", "terms"]
    assert _audit_rows(fresh_db, "workspace.settings")[-1] == (
        "dashboard:a",
        {"workspace": "alpha", "changed": ["alias", "terms"], "reset": False},
    )
    with pytest.raises(InvalidInput):
        owner.set_workspace_settings(fresh_db, "cli", "alpha", {"publish": "sometimes"})
    with pytest.raises(InvalidInput, match="workspace not available"):
        owner.set_workspace_settings(fresh_db, "cli", "nowhere", {"publish": "auto"})


def test_general_approve_flagged_needs_force(fresh_db, queued_item):
    assert owner.check_approval(fresh_db, queued_item, None)
    with pytest.raises(InvalidInput, match="flagged"):
        owner.general_approve(fresh_db, "cli", queued_item, None, force=False)
    assert _audit_rows(fresh_db, "general.approve") == []
    owner.general_approve(fresh_db, "cli", queued_item, None, force=True)
    assert _audit_rows(fresh_db, "general.approve")[-1][0] == "cli"


def test_general_approve_and_drop_unknown_item(fresh_db):
    with pytest.raises(InvalidInput, match="queue item not found or not open"):
        owner.general_approve(fresh_db, "cli", 999999, None, force=True)
    with pytest.raises(InvalidInput, match="queue item not found or already decided"):
        owner.general_drop(fresh_db, "cli", 999999)


def test_general_drop_audits_actor(fresh_db, queued_item):
    owner.general_drop(fresh_db, "dashboard:z", queued_item)
    assert _audit_rows(fresh_db, "general.drop")[-1] == (
        "dashboard:z",
        {"queue_id": queued_item, "workspace": "alpha"},
    )


def test_archive_revokes_tokens(fresh_db):
    owner.create_token(fresh_db, "cli", "gamma", "a", {"read"})
    owner.create_token(fresh_db, "cli", "gamma", "b", {"read"})
    assert owner.archive_workspace(fresh_db, "dashboard:q", "gamma") == 2
    assert _audit_rows(fresh_db, "workspace.archive")[-1] == (
        "dashboard:q",
        {"workspace": "gamma", "tokens_revoked": 2},
    )
    with pytest.raises(InvalidInput, match="workspace not available"):
        owner.archive_workspace(fresh_db, "cli", "gamma")


def test_log_mistake_length_bounds(fresh_db):
    for bad in ("", "x" * 2001):
        with pytest.raises(InvalidInput):
            owner.log_mistake(fresh_db, "cli", "alpha", bad)
    owner.log_mistake(fresh_db, "dashboard:m", "alpha", "x")
    assert _audit_rows(fresh_db, "mistake.log")[-1][0] == "dashboard:m"
    with fresh_db.admin_tx() as c:
        assert c.execute("SELECT count(*) FROM control.mistakes").fetchone()[0] == 1


def test_retry_jobs(fresh_db):
    with fresh_db.admin_tx() as c:
        jid = c.execute(
            "INSERT INTO control.jobs (workspace, kind, key, payload, status, attempts) "
            "VALUES ('alpha', 'embed', 'k1', '{}', 'dead', 5) RETURNING id"
        ).fetchone()[0]
    assert owner.retry_jobs(fresh_db, "dashboard:j", status="dead", ws="alpha") == [jid]
    assert _audit_rows(fresh_db, "jobs.retry")[-1] == (
        "dashboard:j",
        {"ids": [jid], "count": 1},
    )
    with pytest.raises(InvalidInput, match="give a job id or --status dead"):
        owner.retry_jobs(fresh_db, "cli")
    with pytest.raises(InvalidInput, match="job not found or running"):
        owner.retry_jobs(fresh_db, "cli", job_id=999999)


def test_quarantine_release_and_drop_audit_actor(fresh_db):
    from treant.sessions import save_session
    from treant.tokens import TokenContext

    ctx = TokenContext("c1c1c1c1c1c1", "alpha", frozenset({"create"}))
    for sid in ("q1", "q2"):
        save_session(fresh_db, ctx, sid, {"journal": "words"}, unsure=True)
    with fresh_db.ws_tx("alpha") as conn:
        ids = {
            n: str(i)
            for n, i in conn.execute(
                "SELECT s.client_session_id, r.id FROM ws_alpha.revisions r "
                "JOIN ws_alpha.sessions s ON s.id = r.session_id"
            )
        }
    owner.release_quarantine(fresh_db, "dashboard:r", "alpha", ids["q1"])
    owner.drop_quarantine(fresh_db, "cli", "alpha", ids["q2"])
    assert _audit_rows(fresh_db, "quarantine.release")[-1][0] == "dashboard:r"
    assert _audit_rows(fresh_db, "quarantine.drop")[-1][0] == "cli"
    with pytest.raises(InvalidInput):
        owner.drop_quarantine(fresh_db, "cli", "alpha", ids["q2"])


def _mk_fact(db, ns, text="a", type="lesson"):
    from treant import graph

    tx = db.publisher_tx if ns == "general" else (lambda: db.ws_tx(ns))
    with tx() as c:
        return graph.add_fact(c, ns, type, text)


def _row(db, schema, fid):
    with db.admin_tx() as c:
        return c.execute(
            f"SELECT text, status, version FROM {schema}.facts WHERE id = %s", (fid,)
        ).fetchone()


def test_edit_workspace_fact_enqueues_embed_and_audits(fresh_db):
    fid = _mk_fact(fresh_db, "alpha")
    v = owner.edit_fact(fresh_db, "dashboard:a", "alpha", fid, "new text", None)
    assert v == 2 and _row(fresh_db, "ws_alpha", fid) == ("new text", "current", 2)
    with fresh_db.admin_tx() as c:
        assert c.execute("SELECT key FROM control.jobs WHERE kind = 'embed'").fetchall() == [
            (f"fact:{fid}:2",)
        ]
    assert _audit_rows(fresh_db, "fact.edit")[-1] == (
        "dashboard:a",
        {"ns": "alpha", "fact_id": fid, "version": 2},
    )


def test_edit_workspace_fact_scrubbed(fresh_db):
    fid = _mk_fact(fresh_db, "alpha")
    many = " ".join(f"10.0.0.{i}" for i in range(1, 15))
    with pytest.raises(InvalidInput, match="text"):
        owner.edit_fact(fresh_db, "cli", "alpha", fid, f"hosts {many}", None)
    assert _row(fresh_db, "ws_alpha", fid)[0] == "a"
    with pytest.raises(InvalidInput, match="text"):
        owner.edit_fact(fresh_db, "cli", "alpha", fid, "  ", None)


def test_edit_owner_may_reopen_retracted_and_unknown_fact(fresh_db):
    fid = _mk_fact(fresh_db, "alpha")
    owner.retract_fact(fresh_db, "cli", "alpha", fid)
    assert _row(fresh_db, "ws_alpha", fid)[1] == "retracted"
    owner.edit_fact(fresh_db, "cli", "alpha", fid, None, "current")
    assert _row(fresh_db, "ws_alpha", fid)[1] == "current"
    with pytest.raises(owner.Refused, match="fact not found"):
        owner.edit_fact(fresh_db, "cli", "alpha", "00000000-0000-0000-0000-000000000000", "x", None)
    with pytest.raises(InvalidInput):
        owner.edit_fact(fresh_db, "cli", "alpha", fid, None, "bogus")
    with pytest.raises(InvalidInput, match="unknown namespace"):
        owner.edit_fact(fresh_db, "cli", "no such", fid, "x", None)


def test_owner_edit_general_fact_flagged_needs_force(fresh_db, queued_item):
    fid = _mk_fact(fresh_db, "general", "clean")
    with pytest.raises(owner.Flagged, match="flagged"):
        owner.edit_fact(fresh_db, "cli", "general", fid, "use zorblax now", None)
    assert _row(fresh_db, "general", fid)[0] == "clean"
    assert _audit_rows(fresh_db, "fact.edit") == []
    v = owner.edit_fact(fresh_db, "cli", "general", fid, "use zorblax now", None, force=True)
    assert v == 2 and _row(fresh_db, "general", fid)[0] == "use zorblax now"
    with fresh_db.admin_tx() as c:
        assert c.execute("SELECT count(*) FROM general.embeddings").fetchone()[0] == 0
        assert (
            c.execute("SELECT count(*) FROM control.jobs WHERE kind = 'embed'").fetchone()[0] == 0
        )
    assert _audit_rows(fresh_db, "fact.edit")[-1][1] == {
        "ns": "general",
        "fact_id": fid,
        "version": 2,
    }


def test_retract_and_restore_fact_general_and_workspace(fresh_db):
    for ns, schema in (("general", "general"), ("alpha", "ws_alpha")):
        fid = _mk_fact(fresh_db, ns, "orig")
        owner.retract_fact(fresh_db, "cli", ns, fid)
        assert _row(fresh_db, schema, fid)[1] == "retracted"
        assert owner.restore_fact(fresh_db, "dashboard:z", ns, fid, 1) == 3
        assert _row(fresh_db, schema, fid) == ("orig", "current", 3)
        assert _audit_rows(fresh_db, "fact.restore")[-1] == (
            "dashboard:z",
            {"ns": ns, "fact_id": fid, "version": 1},
        )
        assert _audit_rows(fresh_db, "fact.retract")[-1][1] == {"ns": ns, "fact_id": fid}
    with pytest.raises(owner.Refused, match="version not found"):
        owner.restore_fact(fresh_db, "cli", "alpha", fid, 99)


def _flag(db):
    from treant import graph

    with db.ws_tx("alpha") as c:
        a = graph.add_node(c, "alpha", "instance", "db", "postgres")
        b = graph.add_node(c, "alpha", "instance", "db", "postgresql")
        fid = graph.add_fact(c, "alpha", "fact", "f", node_id=b)
        fid_old = graph.add_fact(c, "alpha", "fact", "g", node_id=a)
        flag = c.execute(
            "INSERT INTO ws_alpha.merge_flags (node_a, node_b, score) VALUES (%s, %s, 0.9) RETURNING id",
            (a, b),
        ).fetchone()[0]
        conf = c.execute(
            "INSERT INTO ws_alpha.conflicts (new_fact, old_fact) VALUES (%s, %s) RETURNING id",
            (fid, fid_old),
        ).fetchone()[0]
    return a, b, fid, flag, conf


def test_merge_flag_merges_and_resolves(fresh_db):
    a, b, fid, flag, _ = _flag(fresh_db)
    owner.merge(fresh_db, "dashboard:a", "alpha", flag)
    with fresh_db.admin_tx() as c:
        assert (
            c.execute("SELECT node_id::text FROM ws_alpha.facts WHERE id = %s", (fid,)).fetchone()[
                0
            ]
            == a
        )
        assert c.execute(
            "SELECT resolution, resolved_at IS NOT NULL FROM ws_alpha.merge_flags WHERE id = %s",
            (flag,),
        ).fetchone() == ("merged", True)
    assert _audit_rows(fresh_db, "merge.merge")[-1] == (
        "dashboard:a",
        {"workspace": "alpha", "flag_id": flag, "keep": a, "gone": b, "also_resolved": []},
    )
    with pytest.raises(owner.Refused, match="merge flag not found or resolved"):
        owner.merge(fresh_db, "cli", "alpha", flag)
    with pytest.raises(owner.Refused, match="merge flag not found or resolved"):
        owner.dismiss_merge(fresh_db, "cli", "alpha", flag)


def test_dismiss_merge_sets_resolution_and_audits(fresh_db):
    _, b, _, flag, _ = _flag(fresh_db)
    owner.dismiss_merge(fresh_db, "cli", "alpha", flag)
    with fresh_db.admin_tx() as c:
        assert (
            c.execute("SELECT status FROM ws_alpha.nodes WHERE id = %s", (b,)).fetchone()[0]
            == "current"
        )
        assert (
            c.execute(
                "SELECT resolution FROM ws_alpha.merge_flags WHERE id = %s", (flag,)
            ).fetchone()[0]
            == "dismissed"
        )
    assert _audit_rows(fresh_db, "merge.dismiss")[-1][1] == {"workspace": "alpha", "flag_id": flag}


def test_dismiss_conflict_sets_resolution_and_audits(fresh_db):
    *_, conf = _flag(fresh_db)
    owner.dismiss_conflict(fresh_db, "dashboard:a", "alpha", conf)
    with fresh_db.admin_tx() as c:
        assert c.execute(
            "SELECT resolution, resolved_at IS NOT NULL FROM ws_alpha.conflicts WHERE id = %s",
            (conf,),
        ).fetchone() == ("dismissed", True)
    assert _audit_rows(fresh_db, "conflict.dismiss")[-1][1] == {
        "workspace": "alpha",
        "conflict_id": conf,
    }
    with pytest.raises(owner.Refused, match="conflict not found or resolved"):
        owner.dismiss_conflict(fresh_db, "cli", "alpha", conf)


def test_mistake_limit_shared_with_tools():
    from treant import tools

    assert tools.MAX_MISTAKE is owner.MAX_MISTAKE


def test_merge_resolves_other_open_flags_on_gone_and_refuses_stale(fresh_db):
    from treant import graph

    a, b, _, flag_ab, _ = _flag(fresh_db)
    with fresh_db.ws_tx("alpha") as c:
        n = graph.add_node(c, "alpha", "instance", "db", "pgsql")
        flag_bn = c.execute(
            "INSERT INTO ws_alpha.merge_flags (node_a, node_b, score) VALUES (%s, %s, 0.9) RETURNING id",
            (n, b),
        ).fetchone()[0]
        flag_an = c.execute(
            "INSERT INTO ws_alpha.merge_flags (node_a, node_b, score) VALUES (%s, %s, 0.9) RETURNING id",
            (a, n),
        ).fetchone()[0]
    owner.merge(fresh_db, "cli", "alpha", flag_ab)  # b goes into a
    assert _audit_rows(fresh_db, "merge.merge")[-1][1]["also_resolved"] == [flag_bn]
    with fresh_db.admin_tx() as c:
        got = dict(c.execute("SELECT id, resolution FROM ws_alpha.merge_flags").fetchall())
    assert got[flag_bn] == "superseded" and got[flag_ab] == "merged" and got[flag_an] is None
    # keep side not current: merging n into a is fine, then a flag whose keep (n) is superseded
    owner.merge(fresh_db, "cli", "alpha", flag_an)  # n goes into a
    with fresh_db.ws_tx("alpha") as c:
        flag_na = c.execute(
            "INSERT INTO ws_alpha.merge_flags (node_a, node_b, score) VALUES (%s, %s, 0.9) RETURNING id",
            (n, a),
        ).fetchone()[0]
    with pytest.raises(owner.Refused, match="node not current"):  # keep n is superseded
        owner.merge(fresh_db, "cli", "alpha", flag_na)
    with fresh_db.admin_tx() as c:
        assert (
            c.execute(
                "SELECT resolved_at FROM ws_alpha.merge_flags WHERE id = %s", (flag_na,)
            ).fetchone()[0]
            is None
        )  # refusal rolled back


def test_restore_fact_version_must_be_positive_int(fresh_db):
    fid = _mk_fact(fresh_db, "alpha")
    for bad in ("1", 0, -1, True, None, 1.5):
        with pytest.raises(InvalidInput, match="version"):
            owner.restore_fact(fresh_db, "cli", "alpha", fid, bad)


@pytest.mark.parametrize(
    "call",
    [
        lambda db, ws: owner.set_workspace_settings(db, "cli", ws, {}),
        lambda db, ws: owner.archive_workspace(db, "cli", ws),
        lambda db, ws: owner.log_mistake(db, "cli", ws, "x"),
        lambda db, ws: owner.release_quarantine(db, "cli", ws, "1"),
        lambda db, ws: owner.drop_quarantine(db, "cli", ws, "1"),
    ],
)
def test_bad_workspace_name_is_invalid_input(fresh_db, call):
    for bad in ("Bad Name!", "", "../x", "general"):
        with pytest.raises(InvalidInput, match="invalid workspace name"):
            call(fresh_db, bad)


def test_bad_workspace_name_in_target_functions(fresh_db):
    with pytest.raises(InvalidInput, match="invalid workspace name"):
        owner.add_target(fresh_db, "cli", "Bad Name!", "git_host", "h")
    with pytest.raises(InvalidInput, match="invalid workspace name"):
        owner.remove_target(fresh_db, "cli", "Bad Name!", "git_host", "h")


def test_restore_general_fact_runs_gate_rules(fresh_db, queued_item):
    fid = _mk_fact(fresh_db, "general", "use zorblax for x")
    owner.edit_fact(fresh_db, "cli", "general", fid, "use a cluster for x", None)
    with pytest.raises(owner.Flagged, match="flagged"):
        owner.restore_fact(fresh_db, "cli", "general", fid, 1)
    assert _row(fresh_db, "general", fid) == ("use a cluster for x", "current", 2)
    assert _audit_rows(fresh_db, "fact.restore") == []
    assert owner.restore_fact(fresh_db, "cli", "general", fid, 1, force=True) == 3
    assert _row(fresh_db, "general", fid) == ("use zorblax for x", "current", 3)
    # workspace restores do not run the general rules
    wid = _mk_fact(fresh_db, "alpha", "use zorblax for x")
    owner.edit_fact(fresh_db, "cli", "alpha", wid, "other", None)
    assert owner.restore_fact(fresh_db, "cli", "alpha", wid, 1) == 3


def test_general_edit_is_capped_at_the_gate_limit(fresh_db):
    from treant import gate

    gid = _mk_fact(fresh_db, "general", "short")
    with pytest.raises(InvalidInput, match="text"):
        owner.edit_fact(fresh_db, "cli", "general", gid, "x" * (gate.MAX_TEXT + 1), None)
    wid = _mk_fact(fresh_db, "alpha", "short")
    owner.edit_fact(fresh_db, "cli", "alpha", wid, "word " * 500, None)  # 2500 chars: fine


def test_edit_fact_on_unknown_workspace_is_not_available(fresh_db):
    fid = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(owner.Refused, match="workspace not available"):
        owner.edit_fact(fresh_db, "cli", "zeta", fid, "x", None)


def test_archived_workspace_settings_and_targets_are_refused(fresh_db):
    owner.add_target(fresh_db, "cli", "gamma", "git_host", "h")
    owner.archive_workspace(fresh_db, "cli", "gamma")
    for call in (
        lambda: owner.set_workspace_settings(fresh_db, "cli", "gamma", {"publish": "review"}),
        lambda: owner.add_target(fresh_db, "cli", "gamma", "git_host", "h2"),
        lambda: owner.remove_target(fresh_db, "cli", "gamma", "git_host", "h"),
    ):
        with pytest.raises(owner.Refused, match="workspace not available"):
            call()
    with pytest.raises(owner.Refused, match="workspace not available"):
        owner.add_target(fresh_db, "cli", "zeta", "git_host", "h")
    assert _audit_rows(fresh_db, "target.remove") == []


def test_general_approve_failed_publish_is_audited(fresh_db, queued_item, monkeypatch):
    from treant import gate

    def broken(*a, **k):
        raise InvalidInput("publish failed")

    monkeypatch.setattr(gate, "publish", broken)
    with pytest.raises(InvalidInput, match="publish failed"):
        owner.general_approve(fresh_db, "dashboard:a", queued_item, None, force=True)
    detail = {"queue_id": queued_item, "workspace": "alpha", "edited": False}
    assert _audit_rows(fresh_db, "general.approve") == [("dashboard:a", detail)]
    assert _audit_rows(fresh_db, "general.approve_failed") == [("dashboard:a", detail)]


def test_create_token_bad_actions_is_invalid_input(fresh_db):
    for bad in (set(), {"fly"}):
        with pytest.raises(InvalidInput, match="unknown action"):
            owner.create_token(fresh_db, "cli", "alpha", "l", bad)
    owner.archive_workspace(fresh_db, "cli", "gamma")
    with pytest.raises(InvalidInput, match="workspace not available"):
        owner.create_token(fresh_db, "cli", "gamma", "l", {"read"})
    assert _audit_rows(fresh_db, "token.create") == []


def test_git_host_targets_are_stored_lowercased(fresh_db):
    from treant.guard import GuardEnv, guard_check
    from treant.tokens import TokenContext

    owner.add_target(fresh_db, "cli", "alpha", "git_host", "Git.Example.COM")
    with fresh_db.ws_tx("alpha") as c:
        rows = c.execute("SELECT kind, value FROM ws_alpha.targets").fetchall()
    assert rows == [("git_host", "git.example.com")]
    ctx = TokenContext("a1a1a1a1a1a1", "alpha", frozenset({"read"}))
    env = GuardEnv("/x", None, None, {"origin": "git@git.example.com:g/r.git"})
    assert guard_check(fresh_db, ctx, "git push origin main", env)["unlisted"] == []
    owner.remove_target(fresh_db, "cli", "alpha", "git_host", "GIT.example.com")
    with fresh_db.ws_tx("alpha") as c:
        assert c.execute("SELECT count(*) FROM ws_alpha.targets").fetchone()[0] == 0


def _stale_flag(db, text="uses `old_name.py` here"):
    from treant import graph, stale

    missing = [{"token": "old_name.py", "commit": "abc1234", "subject": "rename old_name"}]
    with db.ws_tx("alpha") as c:
        node = graph.add_node(c, "alpha", "instance", "repo", "demo")
        fid = graph.add_fact(c, "alpha", "fact", text, node)
        flag = stale.flag(c, "alpha", fid, "demo", "a" * 40, missing, [])["flag_id"]
    return str(fid), flag


def _stale_state(db, flag):
    with db.admin_tx() as c:
        return c.execute(
            "SELECT resolution, resolved_at IS NOT NULL FROM ws_alpha.stale_flags WHERE id = %s",
            (flag,),
        ).fetchone()


def test_keep_stale_resolves_and_audits(fresh_db):
    _fid, flag = _stale_flag(fresh_db)
    owner.keep_stale(fresh_db, "cli", "alpha", flag)
    assert _stale_state(fresh_db, flag) == ("kept", True)
    assert _audit_rows(fresh_db, "stale.keep")[-1] == (
        "cli",
        {"workspace": "alpha", "flag_id": flag},
    )
    with pytest.raises(owner.Refused, match="stale flag not found or resolved"):
        owner.keep_stale(fresh_db, "cli", "alpha", flag)


def test_retract_stale_retracts_the_fact(fresh_db):
    fid, flag = _stale_flag(fresh_db)
    owner.retract_stale(fresh_db, "dashboard:a", "alpha", flag)
    with fresh_db.admin_tx() as c:
        status = c.execute("SELECT status FROM ws_alpha.facts WHERE id = %s", (fid,)).fetchone()[0]
    assert status == "retracted"
    assert _stale_state(fresh_db, flag) == ("retracted", True)
    actor, detail = _audit_rows(fresh_db, "stale.retract")[-1]
    assert actor == "dashboard:a"
    assert detail == {"workspace": "alpha", "flag_id": flag, "fact_id": fid}
    assert "old_name" not in str(detail)
    with pytest.raises(owner.Refused, match="stale flag not found or resolved"):
        owner.retract_stale(fresh_db, "cli", "alpha", flag)


def test_stale_actions_refuse_general_and_edited_flags(fresh_db):
    fid, flag = _stale_flag(fresh_db)
    for act in (owner.keep_stale, owner.retract_stale):
        with pytest.raises(InvalidInput, match="unknown namespace"):
            act(fresh_db, "cli", "general", flag)
    owner.edit_fact(fresh_db, "cli", "alpha", fid, "uses `new_name.py` now", None)
    for act in (owner.keep_stale, owner.retract_stale):
        with pytest.raises(owner.Refused, match="stale flag not found or resolved"):
            act(fresh_db, "cli", "alpha", flag)
    assert _stale_state(fresh_db, flag) == ("superseded-by-edit", True)
    assert _audit_rows(fresh_db, "stale.keep") == []
