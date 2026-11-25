import pytest

from treant import cli, settings
from treant.errors import InvalidInput


def test_parse_defaults():
    s = settings.parse({})
    assert (s.backend, s.alias, s.judge_alias, s.publish) == (
        "litellm",
        "default",
        "default",
        "auto",
    )
    assert s.domains == s.ticket_prefixes == s.terms == ()


def test_parse_stored_layout():
    s = settings.parse(
        {"backend": "none", "publish": "review", "rules": {"domains": ["a.com"], "terms": ["xyz"]}}
    )
    assert s.backend == "none" and s.publish == "review"
    assert s.domains == ("a.com",) and s.terms == ("xyz",)


@pytest.mark.parametrize(
    "raw",
    [
        {"bogus": 1},
        {"rules": {"bogus": []}},
        {"alias": 3},
        {"rules": {"terms": "x"}},
        {"rules": {"terms": [1]}},
        {"rules": []},
        {"publish": "maybe"},
        {"backend": "other"},
        {"backend": "delegate:claude"},
    ],
)
def test_parse_rejects_unknown_and_delegate(raw):
    with pytest.raises(InvalidInput):
        settings.parse(raw)


def test_delegate_not_available_yet():
    with pytest.raises(InvalidInput, match="not available yet"):
        settings.parse({"backend": "delegate:claude"})


def test_workspace_set_cli_round_trip(fresh, fresh_db, capsys):
    assert settings.load(fresh_db, "alpha") == settings.WorkspaceSettings()
    assert (
        cli.main(
            [
                "workspace",
                "set",
                "alpha",
                "--backend",
                "none",
                "--publish",
                "review",
                "--domain",
                "a.com",
                "--domain",
                "b.com",
                "--term",
                "secretword",
            ]
        )
        == 0
    )
    s = settings.load(fresh_db, "alpha")
    assert (s.backend, s.publish, s.domains, s.terms) == (
        "none",
        "review",
        ("a.com", "b.com"),
        ("secretword",),
    )
    # options not given keep current values; clear flags empty a list
    assert cli.main(["workspace", "set", "alpha", "--alias", "fast", "--clear-domains"]) == 0
    s = settings.load(fresh_db, "alpha")
    assert (s.alias, s.backend, s.domains, s.terms) == ("fast", "none", (), ("secretword",))
    assert settings.load_all(fresh_db)["alpha"] == s
    assert settings.load_all(fresh_db)["beta"] == settings.WorkspaceSettings()
    with fresh_db.control_tx() as conn:
        rows = conn.execute(
            "SELECT actor, detail::text FROM control.audit WHERE action = 'workspace.settings'"
        ).fetchall()
    assert len(rows) == 2 and all(a == "cli" for a, _ in rows)
    assert all("secretword" not in d and "a.com" not in d for _, d in rows)
    assert "terms" in rows[0][1] and "alias" in rows[1][1]
    capsys.readouterr()
    assert cli.main(["workspace", "show", "alpha"]) == 0
    assert "fast" in capsys.readouterr().out


def test_workspace_set_rejects_bad_values(fresh, fresh_db, capsys):
    assert cli.main(["workspace", "set", "alpha", "--backend", "delegate:x"]) == 2
    assert cli.main(["workspace", "set", "nonesuch", "--alias", "x"]) == 1


@pytest.mark.parametrize("bad", ["", "  ", "\t"])
def test_parse_rejects_empty_entries(bad):
    with pytest.raises(InvalidInput):
        settings.parse({"rules": {"domains": ["a.com", bad]}})


def test_parse_strips_entries():
    assert settings.parse({"rules": {"terms": [" xyz "]}}).terms == ("xyz",)


def _corrupt(db, ws="alpha"):
    with db.admin_tx() as conn:
        conn.execute(
            "UPDATE control.workspaces SET settings = '{\"bogus\": 1}' WHERE name = %s", (ws,)
        )


def test_workspace_set_bad_row_needs_reset(fresh, fresh_db, capsys):
    _corrupt(fresh_db)
    assert cli.main(["workspace", "set", "alpha", "--alias", "x"]) == 1
    assert "use --reset" in capsys.readouterr().err
    assert cli.main(["workspace", "set", "alpha", "--reset", "--alias", "x", "--term", "tee"]) == 0
    s = settings.load(fresh_db, "alpha")
    assert (s.alias, s.terms, s.backend) == ("x", ("tee",), "litellm")
    with fresh_db.control_tx() as conn:
        detail = conn.execute(
            "SELECT detail FROM control.audit WHERE action = 'workspace.settings'"
        ).fetchone()[0]
    assert detail["reset"] is True and "tee" not in detail["changed"]


def test_load_all_fails_closed_on_bad_row(fresh, fresh_db):
    _corrupt(fresh_db)
    with pytest.raises(InvalidInput):
        settings.load_all(fresh_db)


def test_save_with_bad_settings_row_quarantines_and_queues_nothing(fresh, fresh_db):
    from treant import sessions
    from treant.tokens import TokenContext

    _corrupt(fresh_db)
    ctx = TokenContext("c1c1c1c1c1c1", "alpha", frozenset({"create", "read"}))
    res = sessions.save_session(fresh_db, ctx, "s-1", {"journal": "hello"})
    assert res.quarantined is True
    with fresh_db.ws_tx("alpha") as conn:
        status = conn.execute("SELECT status FROM ws_alpha.revisions").fetchone()[0]
    with fresh_db.control_tx() as conn:
        jobs_n = conn.execute("SELECT count(*) FROM control.jobs").fetchone()[0]
    assert status == "quarantined" and jobs_n == 0
    with pytest.raises(InvalidInput):
        sessions.save_session(
            fresh_db, TokenContext("c1c1c1c1c1c1", "nonesuch", frozenset({"create"})), "s", {}
        )


def test_parse_rejects_short_terms():
    with pytest.raises(InvalidInput):
        settings.parse({"rules": {"terms": ["ab"]}})
    with pytest.raises(InvalidInput):
        settings.parse({"rules": {"terms": [" ab "]}})
    assert settings.parse({"rules": {"terms": ["abc"]}}).terms == ("abc",)


@pytest.mark.parametrize("key", ["domains", "ticket_prefixes", "terms"])
def test_parse_caps_list_length(key):
    ok = [f"item{i:04d}" for i in range(200)]
    assert len(getattr(settings.parse({"rules": {key: ok}}), key)) == 200
    with pytest.raises(InvalidInput):
        settings.parse({"rules": {key: ok + ["item9999"]}})
