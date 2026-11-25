import io
import os
import stat
from pathlib import Path

from treant_guard.cli import main
from treant_guard.config import load_config


def _cfg(tmp_path: Path) -> Path:
    d = tmp_path / "treant"
    d.mkdir(mode=0o700)
    d.chmod(0o700)
    return d


def _write(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o600)


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_check_config_ok_and_bad(tmp_path, capsys):
    d = _cfg(tmp_path)
    _write(d / "guard.toml", '[workspaces]\n"/a" = "one"\n[allow.one]\nbogus_kind = ["x"]\n')
    assert main(["check-config", "--config-dir", str(d)]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("ok: ") and out.splitlines()[0].endswith(
        "1 workspaces, mode protect"
    )
    assert "warning: kind bogus_kind is in allow/deny/protect but no rule uses it" in out
    _write(d / "guard.toml", "not = [valid")
    assert main(["check-config", "--config-dir", str(d)]) == 1
    assert f"invalid: {d / 'guard.toml'}" in capsys.readouterr().err


def test_check_exit_codes(tmp_path, capsys):
    d = _cfg(tmp_path)
    w = tmp_path / "w"
    w.mkdir()
    _write(
        d / "guard.toml",
        f'[workspaces]\n"{w}" = "one"\n[allow.one]\nkube_context = ["ok"]\n'
        '[protect."*"]\nkube_context = ["prod"]\n',
    )
    base = ["check", "--cwd", str(w), "--config-dir", str(d), "--"]
    assert main([*base, "kubectl", "--context", "ok", "get", "pods"]) == 0
    assert capsys.readouterr().out.splitlines() == ["allow"]
    assert main([*base, "kubectl", "--context", "prod", "get", "pods"]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "ask" and "kube_context=prod" in lines[1]


def test_init_writes_once_with_modes(tmp_path, capsys):
    d = tmp_path / "newcfg"
    assert main(["init", "--config-dir", str(d)]) == 0
    assert _mode(d) == 0o700 and _mode(d / "guard.toml") == 0o600
    capsys.readouterr()
    assert main(["check-config", "--config-dir", str(d)]) == 0
    assert capsys.readouterr().out.startswith("ok: ")
    assert main(["init", "--config-dir", str(d)]) == 1


def test_import_round_trip(tmp_path, monkeypatch, capsys):
    d = _cfg(tmp_path)
    stdin = 'kube_context\tctx1\nnotab\n\naws_profile\t he"llo \nkube_context\tctx1\n'
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    assert main(["import", "one", "--config-dir", str(d)]) == 0
    assert "imported 2 targets for one" in capsys.readouterr().out
    f = d / "guard.d" / "one.toml"
    assert _mode(f) == 0o600 and _mode(d / "guard.d") == 0o700
    cfg = load_config(d)
    assert cfg.error is None
    assert cfg.allow == {"one": {"aws_profile": ['he"llo'], "kube_context": ["ctx1"]}}


def test_import_rejects_bad_workspace_name(tmp_path, monkeypatch, capsys):
    d = _cfg(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("kube_context\tx\n"))
    assert main(["import", "Bad Name", "--config-dir", str(d)]) == 2
    assert capsys.readouterr().err
    assert not (d / "guard.d").exists()


def test_import_skips_invalid_kinds(tmp_path, monkeypatch, capsys):
    d = _cfg(tmp_path)
    stdin = 'k = ["evil"] #\tv\nkube_context\tctx1\nBad.Kind\tx\n'
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    assert main(["import", "one", "--config-dir", str(d)]) == 0
    cap = capsys.readouterr()
    assert "imported 1 targets for one" in cap.out
    assert cap.err.splitlines() == ["skipped line 1: invalid kind", "skipped line 3: invalid kind"]
    cfg = load_config(d)
    assert cfg.error is None
    assert cfg.allow == {"one": {"kube_context": ["ctx1"]}}


def test_import_rejects_trailing_newline_workspace(tmp_path, monkeypatch, capsys):
    d = _cfg(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("kube_context\tx\n"))
    assert main(["import", "one\n", "--config-dir", str(d)]) == 2
    assert not (d / "guard.d").exists()


def test_check_config_tilde_user_key_is_invalid_not_a_crash(tmp_path, capsys):
    d = _cfg(tmp_path)
    _write(d / "guard.toml", '[workspaces]\n"~Documents/Projects/Acme" = "acme"\n')
    assert main(["check-config", "--config-dir", str(d)]) == 1
    assert capsys.readouterr().err.strip() == f"invalid: {d / 'guard.toml'}"


def test_check_config_prints_rename_hint(tmp_path, capsys):
    d = _cfg(tmp_path)
    _write(d / "guard.toml", '[[rule]]\ncommand = ["x"]\nkind = "k"\nresolver = "tf_root"\n')
    assert main(["check-config", "--config-dir", str(d)]) == 1
    err = capsys.readouterr().err
    assert err.strip() == f"invalid: {d / 'guard.toml'}: tf_root was renamed to terraform_dir"


def test_check_exit_codes_with_deny(tmp_path, capsys):
    d = _cfg(tmp_path)
    w = tmp_path / "w"
    w.mkdir()
    _write(
        d / "guard.toml",
        f'[workspaces]\n"{w}" = "one"\n[allow.one]\nkube_context = ["ok"]\n'
        '[deny."*"]\nkube_context = ["prod"]\n',
    )
    base = ["check", "--cwd", str(w), "--config-dir", str(d), "--"]
    assert main([*base, "kubectl", "--context", "prod", "get", "pods"]) == 2
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "deny"
    assert lines[1] == "Treant guard (one): denied everywhere: kube_context=prod"
    assert main([*base, "kubectl", "--context", "ok", "get", "pods"]) == 0
    # protect mode: a target nobody lists runs
    assert main([*base, "kubectl", "--context", "other", "get", "pods"]) == 0


def test_check_config_warnings(tmp_path, capsys):
    d = _cfg(tmp_path)
    _write(
        d / "guard.toml",
        '[workspaces]\n"/a" = "one"\n'
        '[allow.one]\nkube_context = ["x", "y"]\nbogus = ["z"]\n'
        '[deny.one]\nkube_context = ["x"]\n'
        '[deny."*"]\nother_bogus = ["q"]\n'
        '[about]\none = "fine"\nghost = "not a workspace"\n',
    )
    assert main(["check-config", "--config-dir", str(d)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("ok: ")
    assert out[1:] == [
        "warning: kind bogus is in allow/deny/protect but no rule uses it",
        "warning: kind other_bogus is in allow/deny/protect but no rule uses it",
        "warning: kube_context=x is in both allow and deny for one (deny wins)",
        "warning: [about] key ghost is not a workspace in [workspaces]",
    ]


def _import(monkeypatch, d, stdin, ws="acme"):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return main(["import", ws, "--config-dir", str(d)])


def test_import_writes_allow_and_keeps_deny(tmp_path, monkeypatch, capsys):
    d = _cfg(tmp_path)
    (d / "guard.d").mkdir(mode=0o700)
    _write(
        d / "guard.d" / "acme.toml",
        '[deny.acme]\nkube_context = ["prod"]\n[allow.acme]\nkube_context = ["old"]\n',
    )
    assert _import(monkeypatch, d, "kube_context\tdev\n") == 0
    text = (d / "guard.d" / "acme.toml").read_text()
    assert text.startswith("# written by treant-guard import; comments in this file are not kept\n")
    cfg = load_config(d)
    assert cfg.error is None
    assert cfg.allow == {"acme": {"kube_context": ["dev"]}}
    assert cfg.deny == {"acme": {"kube_context": ["prod"]}}
    assert _mode(d / "guard.d" / "acme.toml") == 0o600


def test_import_omits_deny_table_when_none(tmp_path, monkeypatch, capsys):
    d = _cfg(tmp_path)
    assert _import(monkeypatch, d, "kube_context\tdev\n") == 0
    assert "deny" not in (d / "guard.d" / "acme.toml").read_text()


def test_import_translates_old_kind_names(tmp_path, monkeypatch, capsys):
    d = _cfg(tmp_path)
    stdin = "git_host\tgit.example.com\ntf_root\t/infra\ncf_account\tabc\n"
    assert _import(monkeypatch, d, stdin) == 0
    assert "imported 3 targets for acme" in capsys.readouterr().out
    cfg = load_config(d)
    assert cfg.error is None
    assert cfg.allow == {
        "acme": {
            "git_remote_host": ["git.example.com"],
            "terraform_dir": ["/infra"],
            "cloudflare_account": ["abc"],
        }
    }


def test_import_refuses_file_with_other_tables(tmp_path, monkeypatch, capsys):
    d = _cfg(tmp_path)
    (d / "guard.d").mkdir(mode=0o700)
    for body in (
        '[about]\nacme = "x"\n',
        '[deny."*"]\nkube_context = ["p"]\n',
        '[allow.other]\nkube_context = ["p"]\n',
        '[deny.other]\nkube_context = ["p"]\n',
        '[workspaces]\n"/a" = "acme"\n',
        '[targets.acme]\nkube_context = ["p"]\n',
        '[[rule]]\nname = "x"\n',
    ):
        f = d / "guard.d" / "acme.toml"
        _write(f, body)
        assert _import(monkeypatch, d, "kube_context\tdev\n") == 1
        assert "guard.toml" in capsys.readouterr().err
        assert f.read_text() == body


def test_import_refuses_invalid_toml(tmp_path, monkeypatch, capsys):
    d = _cfg(tmp_path)
    (d / "guard.d").mkdir(mode=0o700)
    f = d / "guard.d" / "acme.toml"
    _write(f, "not = [valid")
    assert _import(monkeypatch, d, "kube_context\tdev\n") == 1
    assert capsys.readouterr().err
    assert f.read_text() == "not = [valid"


def test_import_leaves_guard_toml_alone(tmp_path, monkeypatch, capsys):
    d = _cfg(tmp_path)
    body = '[workspaces]\n"/a" = "acme"\n[deny."*"]\nkube_context = ["p"]\n'
    _write(d / "guard.toml", body)
    assert _import(monkeypatch, d, "kube_context\tdev\n") == 0
    assert (d / "guard.toml").read_text() == body
    assert load_config(d).error is None


def test_init_sample_uses_allow_deny_about(tmp_path, capsys):
    d = tmp_path / "newcfg"
    assert main(["init", "--config-dir", str(d)]) == 0
    text = (d / "guard.toml").read_text()
    for needle in ("[workspaces]", "[allow.example]", "[deny.example]", '[deny."*"]', "[about]"):
        assert needle in text
    assert all(ln.startswith("#") or not ln for ln in text.splitlines())
    capsys.readouterr()
    assert main(["check-config", "--config-dir", str(d)]) == 0


def test_check_config_warns_on_unknown_allow_deny_workspace(tmp_path, capsys):
    d = _cfg(tmp_path)
    _write(
        d / "guard.toml",
        '[workspaces]\n"/a" = "one"\n'
        '[allow.one]\nkube_context = ["x"]\n[allow.ghost]\nkube_context = ["x"]\n'
        '[deny.one]\nkube_context = ["y"]\n[deny.phantom]\nkube_context = ["y"]\n'
        '[deny."*"]\nkube_context = ["z"]\n',
    )
    assert main(["check-config", "--config-dir", str(d)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "warning: [allow.ghost] names a workspace not in [workspaces]" in out
    assert "warning: [deny.phantom] names a workspace not in [workspaces]" in out
    assert not any('"*"' in line or "[deny.*]" in line for line in out)
    assert len(out) == 3


def test_check_config_warns_switch_kind_without_rule(tmp_path, capsys):
    d = _cfg(tmp_path)
    _write(d / "guard.toml", '[[switch]]\ncommand = ["kctx"]\nkinds = ["bogus_kind"]\n')
    assert main(["check-config", "--config-dir", str(d)]) == 0
    assert "warning: [[switch]] kind bogus_kind is used by no rule" in capsys.readouterr().out


def test_import_keeps_protect_table(tmp_path, monkeypatch, capsys):
    d = _cfg(tmp_path)
    (d / "guard.d").mkdir(mode=0o700)
    _write(
        d / "guard.d" / "acme.toml",
        '[protect.acme]\naws_profile = ["live"]\n[deny.acme]\nkube_context = ["prod"]\n',
    )
    assert _import(monkeypatch, d, "kube_context\tdev\n") == 0
    cfg = load_config(d)
    assert cfg.error is None
    assert cfg.allow == {"acme": {"kube_context": ["dev"]}}
    assert cfg.deny == {"acme": {"kube_context": ["prod"]}}
    assert cfg.protect == {"acme": {"aws_profile": ["live"]}}


def test_import_refuses_other_protect_tables(tmp_path, monkeypatch, capsys):
    d = _cfg(tmp_path)
    (d / "guard.d").mkdir(mode=0o700)
    for body in ('[protect."*"]\nkube_context = ["p"]\n', '[guard]\nmode = "protect"\n'):
        f = d / "guard.d" / "acme.toml"
        _write(f, body)
        assert _import(monkeypatch, d, "kube_context\tdev\n") == 1
        assert f.read_text() == body


def test_check_config_reports_mode_and_protect_warnings(tmp_path, capsys):
    d = _cfg(tmp_path)
    _write(
        d / "guard.toml",
        '[workspaces]\n"/a" = "one"\n[allow.one]\nkube_context = ["x"]\n'
        '[protect."*"]\nkube_context = ["x"]\nbogus = ["z"]\n[protect.ghost]\naws_profile = ["p"]\n',
    )
    assert main(["check-config", "--config-dir", str(d)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "ok: 8 rules, 1 workspaces, mode protect"
    assert out[1:] == [
        "warning: kind bogus is in allow/deny/protect but no rule uses it",
        "warning: [protect.ghost] names a workspace not in [workspaces]",
    ]


def test_init_sample_mentions_mode_and_protect(tmp_path, capsys):
    d = tmp_path / "newcfg"
    assert main(["init", "--config-dir", str(d)]) == 0
    text = (d / "guard.toml").read_text()
    assert '# mode = "protect"' in text
    assert '# [protect."*"]' in text


def _stray(d: Path) -> list[str]:
    return [p.name for p in (d / "guard.d").glob(".*.tmp")] if (d / "guard.d").exists() else []


def test_import_with_nothing_keeps_an_existing_allow_list(tmp_path, monkeypatch, capsys):
    """An empty or unreadable list (a failed export) must not wipe the allow list: in protect
    mode it tells other workspaces which targets belong here."""
    d = _cfg(tmp_path)
    (d / "guard.d").mkdir(mode=0o700)
    f = d / "guard.d" / "acme.toml"
    body = '[allow.acme]\nkube_context = ["ops"]\n'
    _write(f, body)
    for stdin in ("", "\n", "notab\n", "Bad.Kind\tx\n"):
        assert _import(monkeypatch, d, stdin) == 1
        err = capsys.readouterr().err
        assert "nothing to import" in err and "left as is" in err
        assert f.read_text() == body
    assert _stray(d) == []


def test_import_with_nothing_writes_a_new_file(tmp_path, monkeypatch, capsys):
    d = _cfg(tmp_path)
    assert _import(monkeypatch, d, "") == 0
    assert "imported 0 targets for acme" in capsys.readouterr().out
    cfg = load_config(d)
    assert cfg.error is None
    assert cfg.allow == {"acme": {}}
    # a file with only a deny table has no allow list to lose
    _write(d / "guard.d" / "acme.toml", '[deny.acme]\nkube_context = ["prod"]\n')
    assert _import(monkeypatch, d, "") == 0
    assert load_config(d).deny == {"acme": {"kube_context": ["prod"]}}


def test_import_keeps_value_order_and_sorts_kinds(tmp_path, monkeypatch, capsys):
    d = _cfg(tmp_path)
    stdin = "kube_context\tb\naws_profile\tz\nkube_context\ta\nkube_context\tb\naws_profile\ty\n"
    assert _import(monkeypatch, d, stdin) == 0
    assert "imported 4 targets for acme" in capsys.readouterr().out
    lines = (d / "guard.d" / "acme.toml").read_text().splitlines()
    assert lines[1:] == [
        "[allow.acme]",
        'aws_profile = ["z", "y"]',
        'kube_context = ["b", "a"]',
    ]


def test_import_re_emits_kept_values_with_escapes(tmp_path, monkeypatch, capsys):
    d = _cfg(tmp_path)
    (d / "guard.d").mkdir(mode=0o700)
    _write(
        d / "guard.d" / "acme.toml",
        '[deny.acme]\nkube_context = ["a\\"b", "c\\\\d"]\naws_profile = ["tab\\there"]\n'
        '[protect.acme]\nkube_context = ["new\\nline", "uni\\u0001"]\n',
    )
    before = load_config(d)
    assert _import(monkeypatch, d, "kube_context\tdev\n") == 0
    after = load_config(d)
    assert after.error is None
    assert (
        after.deny
        == before.deny
        == {"acme": {"kube_context": ['a"b', "c\\d"], "aws_profile": ["tab\there"]}}
    )
    assert after.protect == before.protect
    assert after.protect["acme"]["kube_context"] == ["new\nline", "uni\x01"]


def test_import_refusals_leave_no_temporary_file(tmp_path, monkeypatch, capsys):
    d = _cfg(tmp_path)
    (d / "guard.d").mkdir(mode=0o700)
    f = d / "guard.d" / "acme.toml"
    for body in ("not = [valid", '[about]\nacme = "x"\n'):
        _write(f, body)
        assert _import(monkeypatch, d, "kube_context\tdev\n") == 1
        assert _stray(d) == []
    assert _import(monkeypatch, d, "kube_context\tdev\n", ws="Bad") == 2
    assert _stray(d) == []


def test_import_rejects_input_that_is_not_utf8(tmp_path, monkeypatch, capsys):
    d = _cfg(tmp_path)

    class Stdin:
        buffer = io.BytesIO(b"kube_context\t\xff\xfe\n")

    monkeypatch.setattr("sys.stdin", Stdin())
    assert main(["import", "acme", "--config-dir", str(d)]) == 1
    assert "not UTF-8" in capsys.readouterr().err
    assert not (d / "guard.d" / "acme.toml").exists()


def test_check_with_an_empty_command_allows(tmp_path, capsys):
    d = _cfg(tmp_path)
    assert main(["check", "--cwd", str(tmp_path), "--config-dir", str(d), "--"]) == 0
    assert capsys.readouterr().out.splitlines() == ["allow"]
    assert main(["check", "--cwd", str(tmp_path), "--config-dir", str(d)]) == 0


def test_init_creates_missing_parents_private(tmp_path, capsys):
    d = tmp_path / "home" / ".config" / "treant"
    assert main(["init", "--config-dir", str(d)]) == 0
    assert _mode(tmp_path / "home") == _mode(d.parent) == _mode(d) == 0o700
    capsys.readouterr()
    assert main(["check-config", "--config-dir", str(d)]) == 0


def test_check_config_warns_on_allow_entries_deny_shadows(tmp_path, capsys):
    d = _cfg(tmp_path)
    _write(
        d / "guard.toml",
        '[workspaces]\n"/a" = "one"\n"/b" = "two"\n'
        '[allow.one]\nkube_context = ["x", "y", "ok"]\nterraform_dir = ["/infra/prod/eu", "/infra/dev"]\n'
        '[deny.one]\nkube_context = ["x"]\nterraform_dir = ["/infra/prod"]\n'
        '[deny."*"]\nkube_context = ["y"]\n'
        '[allow.two]\nkube_context = ["y"]\n',
    )
    assert main(["check-config", "--config-dir", str(d)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1:] == [
        "warning: kube_context=x is in both allow and deny for one (deny wins)",
        "warning: kube_context=y is in allow for one but denied everywhere (deny wins)",
        (
            "warning: terraform_dir=/infra/prod/eu is in allow for one but inside a denied folder"
            " (deny wins)"
        ),
        "warning: kube_context=y is in allow for two but denied everywhere (deny wins)",
    ]


def test_check_config_warns_on_capital_hosts_in_allow(tmp_path, capsys):
    d = _cfg(tmp_path)
    _write(
        d / "guard.toml",
        '[workspaces]\n"/a" = "one"\n[allow.one]\ngit_remote_host = ["GitHub.com", "gitlab.com"]\n'
        '[deny."*"]\ngit_remote_host = ["Evil.Example"]\n',
    )
    assert main(["check-config", "--config-dir", str(d)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1:] == [
        (
            "warning: git_remote_host=GitHub.com in [allow.one] never matches: hosts are read in"
            " lower case"
        ),
    ]


def test_check_config_shows_and_checks_workspace_modes(tmp_path, capsys):
    d = _cfg(tmp_path)
    _write(
        d / "guard.toml",
        '[guard.modes]\none = "production"\ntypo = "production"\n[workspaces]\n"/a" = "one"\n',
    )
    assert main(["check-config", "--config-dir", str(d)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].endswith("mode protect, one production, typo production")
    assert "warning: [guard.modes] key typo is not a workspace in [workspaces]" in out
    assert not any("key one" in line for line in out)
