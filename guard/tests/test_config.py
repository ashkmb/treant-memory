import os
from pathlib import Path

import pytest

from treant_guard.config import is_trusted, load_config
from treant_guard.rules import default_rules
from treant_guard.targets import LocalTargets
from treant_guard.workspaces import resolve


def _dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o600)
    return path


def _cfg_dir(tmp_path: Path) -> Path:
    return _dir(tmp_path / "treant")


def test_missing_config_is_empty_not_error(tmp_path):
    cfg = load_config(_cfg_dir(tmp_path))
    assert cfg.error is None
    assert cfg.workspaces == {} and cfg.allow == {}
    assert cfg.deny == {} and cfg.about == {}
    assert cfg.rules == default_rules()


def test_workspaces_and_targets_merge_across_files(tmp_path):
    d = _cfg_dir(tmp_path)
    _write(
        d / "guard.toml",
        '[workspaces]\n"/a" = "one"\n"/b" = "two"\n'
        '[allow.one]\nkube_context = [" ctx1 ", "ctx2"]\n',
    )
    gd = _dir(d / "guard.d")
    _write(
        gd / "10-x.toml",
        '[workspaces]\n"/b" = "three"\n[allow.one]\nkube_context = ["ctx2", "ctx3"]\naws_profile = ["p"]\n',
    )
    cfg = load_config(d)
    assert cfg.error is None
    assert cfg.workspaces == {"/a": "one", "/b": "three"}
    assert cfg.allow == {"one": {"kube_context": ["ctx1", "ctx2", "ctx3"], "aws_profile": ["p"]}}


def test_bad_rule_field_makes_config_invalid(tmp_path):
    d = _cfg_dir(tmp_path)
    f = _write(d / "guard.toml", '[[rule]]\ncommand = ["x"]\nkind = "k"\nflags = "--x"\n')
    cfg = load_config(d)
    assert cfg.error == str(f)
    assert cfg.rules == default_rules()
    assert cfg.workspaces == {} and cfg.allow == {}


def test_broken_toml_is_invalid(tmp_path):
    d = _cfg_dir(tmp_path)
    f = _write(d / "guard.toml", "[workspaces\n")
    assert load_config(d).error == str(f)


def test_unknown_table_is_invalid(tmp_path):
    d = _cfg_dir(tmp_path)
    f = _write(d / "guard.toml", "[bogus]\na = 1\n")
    assert load_config(d).error == str(f)


def test_group_writable_file_is_untrusted(tmp_path):
    d = _cfg_dir(tmp_path)
    f = _write(d / "guard.toml", '[workspaces]\n"/a" = "one"\n')
    f.chmod(0o664)
    cfg = load_config(d)
    assert cfg.error == str(f)
    assert cfg.workspaces == {}


def test_symlinked_file_is_trusted(tmp_path):
    d = _cfg_dir(tmp_path)
    other = _dir(tmp_path / "elsewhere")
    real = _write(other / "real.toml", '[workspaces]\n"/a" = "one"\n')
    (d / "guard.toml").symlink_to(real)
    cfg = load_config(d)
    assert cfg.error is None
    assert cfg.workspaces == {"/a": "one"}


def test_workspace_resolves_through_symlink(tmp_path):
    real = _dir(tmp_path / "real")
    sub = _dir(real / "sub")
    link = tmp_path / "link"
    link.symlink_to(real)
    assert resolve(str(sub), {str(link): "ws"}) == "ws"
    assert resolve("", {str(link): "ws"}) is None
    assert resolve(str(tmp_path), {str(link): "ws"}) is None


def test_terraform_dir_targets_expand_home(tmp_path):
    d = _cfg_dir(tmp_path)
    _write(d / "guard.toml", '[allow.one]\nterraform_dir = ["~/infra"]\nkube_context = ["~x"]\n')
    cfg = load_config(d)
    assert cfg.allow["one"]["terraform_dir"] == [os.path.expanduser("~/infra")]
    assert cfg.allow["one"]["kube_context"] == ["~x"]


def test_local_targets_unknown_workspace_is_empty(tmp_path):
    d = _cfg_dir(tmp_path)
    _write(d / "guard.toml", '[allow.one]\naws_profile = ["p"]\n')
    src = LocalTargets(load_config(d))
    assert src.lists("one") == ({"aws_profile": ["p"]}, {})
    assert src.lists("nope") == ({}, {})


def test_symlink_to_group_writable_file_is_untrusted(tmp_path):
    d = _cfg_dir(tmp_path)
    other = _dir(tmp_path / "elsewhere")
    real = _write(other / "real.toml", '[workspaces]\n"/a" = "one"\n')
    real.chmod(0o664)
    (d / "guard.toml").symlink_to(real)
    assert load_config(d).error == str(d / "guard.toml")


def test_file_owned_by_someone_else_is_untrusted(tmp_path):
    d = _cfg_dir(tmp_path)
    f = _write(d / "guard.toml", '[workspaces]\n"/a" = "one"\n')
    assert load_config(d, uid=os.getuid()).error is None
    assert load_config(d, uid=os.getuid() + 1).error == str(f)


def test_group_writable_config_dir_is_untrusted(tmp_path):
    d = _cfg_dir(tmp_path)
    f = _write(d / "guard.toml", '[workspaces]\n"/a" = "one"\n')
    d.chmod(0o770)
    try:
        assert load_config(d).error == str(f)
    finally:
        d.chmod(0o700)


def test_group_writable_guard_d_file_is_untrusted(tmp_path):
    d = _cfg_dir(tmp_path)
    gd = _dir(d / "guard.d")
    f = _write(gd / "a.toml", "")
    f.chmod(0o666)
    assert load_config(d).error == str(f)


def test_stat_failure_makes_config_invalid(tmp_path, monkeypatch):
    d = _cfg_dir(tmp_path)
    f = _write(d / "guard.toml", '[workspaces]\n"/a" = "one"\n')
    real_stat = os.stat

    def boom(p, *a, **kw):
        if str(p) == str(f):
            raise PermissionError("denied")
        return real_stat(p, *a, **kw)

    monkeypatch.setattr(os, "stat", boom)
    assert load_config(d).error == str(f)
    assert is_trusted(f, os.getuid()) is False


def test_dangling_symlink_is_invalid(tmp_path):
    d = _cfg_dir(tmp_path)
    f = d / "guard.toml"
    f.symlink_to(tmp_path / "nonexistent.toml")
    cfg = load_config(d)
    assert cfg.error == str(f)
    assert cfg.workspaces == {}


def test_dangling_symlink_in_guard_d_is_invalid(tmp_path):
    d = _cfg_dir(tmp_path)
    gd = _dir(d / "guard.d")
    f = gd / "a.toml"
    f.symlink_to(tmp_path / "nonexistent.toml")
    assert load_config(d).error == str(f)


def test_tilde_user_workspace_key_is_invalid(tmp_path):
    d = _cfg_dir(tmp_path)
    f = _write(d / "guard.toml", '[workspaces]\n"~Documents/Projects/Acme" = "acme"\n')
    cfg = load_config(d)
    assert cfg.error == str(f)
    assert cfg.workspaces == {} and cfg.rules == default_rules()


def test_nul_workspace_key_is_invalid(tmp_path):
    d = _cfg_dir(tmp_path)
    f = _write(d / "guard.toml", '[workspaces]\n"/a\\u0000b" = "one"\n')
    assert load_config(d).error == str(f)


def test_home_workspace_keys_are_valid(tmp_path):
    d = _cfg_dir(tmp_path)
    _write(d / "guard.toml", '[workspaces]\n"~" = "home"\n"~/p" = "p"\n')
    cfg = load_config(d)
    assert cfg.error is None
    assert cfg.workspaces == {os.path.expanduser("~"): "home", os.path.expanduser("~/p"): "p"}


def test_unexpected_error_in_a_file_makes_it_invalid(tmp_path, monkeypatch):
    import treant_guard.config as config_mod

    d = _cfg_dir(tmp_path)
    f = _write(d / "guard.toml", '[workspaces]\n"/a" = "one"\n')

    def boom(data, source):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(config_mod, "parse_rules", boom)
    assert load_config(d).error == str(f)


def test_load_config_never_raises(tmp_path, monkeypatch):
    import treant_guard.config as config_mod

    d = _cfg_dir(tmp_path)

    def boom(config_dir):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(config_mod, "_files", boom)
    cfg = load_config(d)
    assert cfg.error == str(d)
    assert cfg.rules == default_rules() and cfg.workspaces == {} and cfg.allow == {}


def test_local_targets_lists_merges_star_deny(tmp_path):
    d = _cfg_dir(tmp_path)
    _write(
        d / "guard.toml",
        '[deny.one]\nkube_context = ["a", "b"]\n[deny."*"]\nkube_context = ["b", "c"]\n'
        'aws_profile = ["x"]\n[allow.one]\nkube_context = ["ok"]\n',
    )
    cfg = load_config(d)
    assert cfg.error is None
    assert cfg.deny["*"] == {"kube_context": ["b", "c"], "aws_profile": ["x"]}
    allow, deny = LocalTargets(cfg).lists("one")
    assert allow == {"kube_context": ["ok"]}
    assert deny == {"kube_context": ["a", "b", "c"], "aws_profile": ["x"]}
    assert LocalTargets(cfg).lists("other") == (
        {},
        {"kube_context": ["b", "c"], "aws_profile": ["x"]},
    )


def test_star_is_not_valid_in_allow(tmp_path):
    d = _cfg_dir(tmp_path)
    f = _write(d / "guard.toml", '[allow."*"]\nkube_context = ["a"]\n')
    assert load_config(d).error == str(f)


def test_old_targets_table_is_invalid_with_hint(tmp_path):
    d = _cfg_dir(tmp_path)
    f = _write(d / "guard.toml", '[targets.one]\nkube_context = ["a"]\n')
    cfg = load_config(d)
    assert cfg.error == str(f)
    assert cfg.error_detail == "[targets] was renamed to [allow]"
    assert cfg.allow == {} and cfg.deny == {}


@pytest.mark.parametrize("table", ["allow", "deny"])
@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("git_host", "git_remote_host"),
        ("tf_root", "terraform_dir"),
        ("cf_account", "cloudflare_account"),
    ],
)
def test_old_kind_names_are_invalid_with_hint(tmp_path, table, old, new):
    d = _cfg_dir(tmp_path)
    f = _write(d / "guard.toml", f'[{table}.one]\n{old} = ["a"]\n')
    cfg = load_config(d)
    assert cfg.error == str(f)
    assert cfg.error_detail == f"{old} was renamed to {new}"


@pytest.mark.parametrize(
    ("body", "hint"),
    [
        ('fallback = ["a"]', "fallback was renamed to default_from"),
        ('unresolved_if = ["--x"]', "unresolved_if was renamed to unknown_if"),
        ("multiple = true", "multiple was renamed to all_values"),
        ('short_attached = "-i"', "short_attached was renamed to short_flag"),
        ('resolver = "tf_root"', "tf_root was renamed to terraform_dir"),
    ],
)
def test_old_rule_fields_are_invalid(tmp_path, body, hint):
    d = _cfg_dir(tmp_path)
    f = _write(d / "guard.toml", f'[[rule]]\ncommand = ["x"]\nkind = "k"\n{body}\n')
    cfg = load_config(d)
    assert cfg.error == str(f)
    assert cfg.error_detail == hint
    assert cfg.rules == default_rules()


def test_allow_deny_about_load_and_union_across_files(tmp_path):
    d = _cfg_dir(tmp_path)
    _write(
        d / "guard.toml",
        '[allow.one]\nkube_context = ["a", "b"]\n[deny.one]\naws_profile = ["p"]\n'
        '[about]\none = "first"\ntwo = "keep"\n',
    )
    gd = _dir(d / "guard.d")
    _write(
        gd / "10-x.toml",
        '[allow.one]\nkube_context = ["b", "c"]\n[deny.one]\naws_profile = ["p", "q"]\n'
        '[deny."*"]\ngit_remote_host = ["h"]\n[about]\none = "second"\n',
    )
    cfg = load_config(d)
    assert cfg.error is None and cfg.error_detail is None
    assert cfg.allow == {"one": {"kube_context": ["a", "b", "c"]}}
    assert cfg.deny == {"one": {"aws_profile": ["p", "q"]}, "*": {"git_remote_host": ["h"]}}
    assert cfg.about == {"one": "second", "two": "keep"}


def test_about_too_long_is_invalid(tmp_path):
    d = _cfg_dir(tmp_path)
    _write(d / "guard.toml", f'[about]\none = "{"x" * 2000}"\n')
    assert load_config(d).error is None
    f = _write(d / "guard.toml", f'[about]\none = "{"x" * 2001}"\n')
    assert load_config(d).error == str(f)


def test_about_must_be_strings(tmp_path):
    d = _cfg_dir(tmp_path)
    f = _write(d / "guard.toml", "[about]\none = 3\n")
    assert load_config(d).error == str(f)


def test_relative_path_prefix_value_is_invalid(tmp_path):
    for table in ("allow", "deny"):
        d = _cfg_dir(tmp_path / table)
        path = _write(
            d / "guard.toml",
            f'[workspaces]\n"/a" = "one"\n[{table}.one]\nterraform_dir = ["prod"]\n',
        )
        cfg = load_config(d)
        assert cfg.error == str(path)
        assert cfg.error_detail == "terraform_dir values must be absolute paths: prod"


def test_tilde_path_prefix_value_is_valid(tmp_path):
    d = _cfg_dir(tmp_path)
    _write(d / "guard.toml", '[deny.one]\nterraform_dir = ["~/infra"]\n')
    assert load_config(d).error is None


_MYDIR_RULE = (
    '[[rule]]\ncommand = ["mytool"]\nkind = "mydir"\nflags = ["--dir"]\nmatch = "path_prefix"\n'
)


def test_relative_value_before_late_path_rule_is_invalid(tmp_path):
    d = _cfg_dir(tmp_path)
    guard_d = _dir(d / "guard.d")
    a = _write(guard_d / "a.toml", '[workspaces]\n"/a" = "one"\n[deny.one]\nmydir = ["prod"]\n')
    _write(guard_d / "z.toml", _MYDIR_RULE)
    cfg = load_config(d)
    assert cfg.error == str(a)
    assert cfg.error_detail == "mydir values must be absolute paths: prod"


def test_relative_value_after_path_rule_is_invalid(tmp_path):
    d = _cfg_dir(tmp_path)
    guard_d = _dir(d / "guard.d")
    _write(guard_d / "a.toml", _MYDIR_RULE)
    z = _write(guard_d / "z.toml", '[workspaces]\n"/a" = "one"\n[deny.one]\nmydir = ["prod"]\n')
    cfg = load_config(d)
    assert cfg.error == str(z)
    assert cfg.error_detail == "mydir values must be absolute paths: prod"


def test_absolute_values_with_late_path_rule_are_valid(tmp_path):
    d = _cfg_dir(tmp_path)
    guard_d = _dir(d / "guard.d")
    _write(guard_d / "a.toml", '[deny.one]\nmydir = ["/srv/prod", "~/x"]\n')
    _write(guard_d / "z.toml", _MYDIR_RULE)
    cfg = load_config(d)
    assert cfg.error is None
    assert cfg.deny["one"]["mydir"] == ["/srv/prod", os.path.expanduser("~/x")]


def test_user_switch_appends(tmp_path):
    from treant_guard.rules import Switch, default_switches

    d = _cfg_dir(tmp_path)
    _write(d / "guard.toml", '[[switch]]\ncommand = ["kctx"]\nkinds = ["kube_context"]\n')
    cfg = load_config(d)
    assert cfg.error is None
    assert cfg.switches == [*default_switches(), Switch(("kctx",), ("kube_context",))]


def test_bad_switch_makes_config_invalid(tmp_path):
    d = _cfg_dir(tmp_path)
    _write(d / "guard.toml", '[[switch]]\ncommand = ["kctx"]\n')
    cfg = load_config(d)
    assert cfg.error == str(d / "guard.toml")


def test_every_relative_path_value_is_listed(tmp_path):
    d = _cfg_dir(tmp_path)
    path = _write(
        d / "guard.toml",
        '[workspaces]\n"/a" = "one"\n[allow.one]\nterraform_dir = ["prod", "/abs", "dev"]\n'
        '[deny.one]\nterraform_dir = ["stage", "prod"]\n',
    )
    cfg = load_config(d)
    assert cfg.error == str(path)
    assert cfg.error_detail == "terraform_dir values must be absolute paths: prod, dev, stage"


def test_protect_loads_with_star_and_workspace_keys(tmp_path):
    d = _cfg_dir(tmp_path)
    _write(
        d / "guard.toml",
        '[protect."*"]\nkube_context = ["prod", "production"]\n'
        '[protect.one]\naws_profile = ["live"]\n[allow.one]\nkube_context = ["dev"]\n',
    )
    gd = _dir(d / "guard.d")
    _write(gd / "x.toml", '[protect."*"]\nkube_context = ["production", "prd"]\n')
    cfg = load_config(d)
    assert cfg.error is None
    assert cfg.protect == {
        "*": {"kube_context": ["prod", "production", "prd"]},
        "one": {"aws_profile": ["live"]},
    }
    targets = LocalTargets(cfg)
    assert targets.protected("one") == {
        "kube_context": ["prod", "production", "prd"],
        "aws_profile": ["live"],
    }
    assert targets.protected("two") == {"kube_context": ["prod", "production", "prd"]}


def test_protect_path_values_must_be_absolute_and_are_expanded(tmp_path):
    d = _cfg_dir(tmp_path)
    f = _write(d / "guard.toml", '[protect."*"]\nterraform_dir = ["rel/live"]\n')
    cfg = load_config(d)
    assert cfg.error == str(f)
    assert cfg.error_detail == "terraform_dir values must be absolute paths: rel/live"
    _write(d / "guard.toml", '[protect."*"]\nterraform_dir = ["~/live"]\n')
    cfg = load_config(d)
    assert cfg.protect == {"*": {"terraform_dir": [os.path.expanduser("~/live")]}}


def test_protect_bad_shape_is_invalid(tmp_path):
    d = _cfg_dir(tmp_path)
    f = _write(d / "guard.toml", '[protect.one]\nkube_context = "prod"\n')
    assert load_config(d).error == str(f)


def test_owners_lists_other_workspaces_allow(tmp_path):
    d = _cfg_dir(tmp_path)
    _write(
        d / "guard.toml",
        '[allow.one]\nkube_context = ["a"]\n[allow.two]\nkube_context = ["b"]\n'
        '[allow.three]\naws_profile = ["p"]\n',
    )
    others = LocalTargets(load_config(d)).others("one")
    assert others == {"two": {"kube_context": ["b"]}, "three": {"aws_profile": ["p"]}}


def test_mode_defaults_to_protect_and_reads_guard_table(tmp_path):
    d = _cfg_dir(tmp_path)
    assert load_config(d).mode == "protect"
    _write(d / "guard.toml", '[guard]\nmode = "allowlist"\n')
    cfg = load_config(d)
    assert cfg.error is None
    assert cfg.mode == "allowlist"


@pytest.mark.parametrize(
    "body",
    ['[guard]\nmode = "strict"\n', "[guard]\nmode = 1\n", '[guard]\nmode = "protect"\nx = 1\n'],
)
def test_bad_guard_table_is_invalid(tmp_path, body):
    d = _cfg_dir(tmp_path)
    f = _write(d / "guard.toml", body)
    assert load_config(d).error == str(f)


def test_guard_modes_table_sets_a_workspace_mode(tmp_path):
    d = _cfg_dir(tmp_path)
    _write(d / "guard.toml", '[guard]\nmode = "protect"\n[guard.modes]\nstartup = "production"\n')
    cfg = load_config(d)
    assert cfg.error is None
    assert (cfg.mode, cfg.modes) == ("protect", {"startup": "production"})


@pytest.mark.parametrize(
    "body",
    [
        '[guard.modes]\nstartup = "lax"\n',
        "[guard.modes]\nstartup = 1\n",
        '[guard.modes]\n"*" = "production"\n',
        '[guard]\nmodes = "production"\n',
    ],
)
def test_bad_guard_modes_is_invalid(tmp_path, body):
    d = _cfg_dir(tmp_path)
    f = _write(d / "guard.toml", body)
    assert load_config(d).error == str(f)


def test_drop_in_cannot_map_a_folder_into_a_mode_override(tmp_path):
    # a guard.d file must not move a folder into a workspace with its own (laxer) mode
    d = _cfg_dir(tmp_path)
    _write(
        d / "guard.toml",
        '[guard]\nmode = "allowlist"\n[guard.modes]\nstartup = "production"\n'
        '[workspaces]\n"/t" = "startup"\n',
    )
    gd = _dir(d / "guard.d")
    f = _write(gd / "zz.toml", '[workspaces]\n"/c/infra" = "startup"\n')
    cfg = load_config(d)
    assert cfg.error == str(f)
    assert "[guard.modes]" in cfg.error_detail


def test_drop_in_may_map_folders_of_other_workspaces(tmp_path):
    d = _cfg_dir(tmp_path)
    _write(d / "guard.toml", '[guard.modes]\nstartup = "production"\n')
    gd = _dir(d / "guard.d")
    _write(gd / "a.toml", '[workspaces]\n"/c" = "acme"\n')
    assert load_config(d).error is None


def test_guard_table_only_in_guard_toml(tmp_path):
    d = _cfg_dir(tmp_path)
    _write(d / "guard.toml", '[guard]\nmode = "allowlist"\n')
    gd = _dir(d / "guard.d")
    f = _write(gd / "z.toml", '[guard]\nmode = "protect"\n')
    cfg = load_config(d)
    assert cfg.error == str(f)
    assert cfg.error_detail == "[guard] belongs in guard.toml only"


def test_group_writable_guard_d_dir_is_untrusted(tmp_path):
    d = _cfg_dir(tmp_path)
    _write(d / "guard.toml", '[workspaces]\n"/a" = "one"\n')
    gd = _dir(d / "guard.d")
    _write(gd / "a.toml", '[protect."*"]\nkube_context = ["prod"]\n')
    gd.chmod(0o770)
    try:
        assert load_config(d).error == str(gd)
        _write(gd / "a.toml", "")  # an empty guard.d is checked too
        (gd / "a.toml").unlink()
        assert load_config(d).error == str(gd)
    finally:
        gd.chmod(0o700)


def test_guard_d_that_is_not_a_folder_is_invalid(tmp_path):
    """A dangling guard.d link or a guard.d file would drop every deny and protect list."""
    d = _cfg_dir(tmp_path)
    _write(d / "guard.toml", '[workspaces]\n"/a" = "one"\n')
    gd = d / "guard.d"
    gd.symlink_to(tmp_path / "gone")
    cfg = load_config(d)
    assert cfg.error == str(gd)
    assert cfg.workspaces == {}
    gd.unlink()
    _write(gd, '[deny."*"]\nkube_context = ["prod"]\n')
    assert load_config(d).error == str(gd)


def test_star_is_not_a_workspace_name(tmp_path):
    d = _cfg_dir(tmp_path)
    f = _write(d / "guard.toml", '[workspaces]\n"/a" = "*"\n')
    cfg = load_config(d)
    assert cfg.error == str(f)
    assert cfg.error_detail == '"*" is not a workspace name: it means every workspace'


def test_old_kind_name_in_a_user_rule_is_invalid_with_hint(tmp_path):
    d = _cfg_dir(tmp_path)
    f = _write(d / "guard.toml", '[[rule]]\ncommand = ["x"]\nkind = "tf_root"\n')
    cfg = load_config(d)
    assert cfg.error == str(f)
    assert cfg.error_detail == "tf_root was renamed to terraform_dir"


@pytest.mark.parametrize(
    "body",
    [
        "workspaces = 1\n",
        '[workspaces]\n"/a" = 1\n',
        "allow = 1\n",
        "[allow]\none = 1\n",
        '[allow.one]\nkube_context = "x"\n',
        "[allow.one]\nkube_context = [1]\n",
        "deny = []\n",
        '[protect.one]\nkube_context = ["x", 2]\n',
        "about = 1\n",
        "[about]\none = 1\n",
        "guard = 1\n",
        '[guard]\nmode = "strict"\n',
        "[guard]\nother = 1\n",
    ],
)
def test_bad_shapes_are_invalid(tmp_path, body):
    d = _cfg_dir(tmp_path)
    f = _write(d / "guard.toml", body)
    cfg = load_config(d)
    assert cfg.error == str(f)
    assert cfg.workspaces == {} and cfg.allow == {} and cfg.deny == {} and cfg.protect == {}


def test_later_file_rule_overrides_earlier(tmp_path):
    d = _cfg_dir(tmp_path)
    rule = '[[rule]]\ncommand = ["kubectl"]\nkind = "kube_context"\nflags = ["{}"]\n'
    _write(d / "guard.toml", rule.format("--top"))
    gd = _dir(d / "guard.d")
    _write(gd / "10-a.toml", rule.format("--ten"))
    _write(gd / "20-b.toml", rule.format("--twenty"))
    cfg = load_config(d)
    assert cfg.error is None
    (kubectl,) = [r for r in cfg.rules if r.command == ("kubectl",)]
    assert kubectl.flags == ("--twenty",)
    assert kubectl.source == str(gd / "20-b.toml")


def test_tilde_and_home_spellings_of_a_folder_are_one_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = _cfg_dir(tmp_path)
    _write(
        d / "guard.toml",
        f'[deny.one]\nterraform_dir = ["~/infra", "{tmp_path}/infra", "~/other"]\n',
    )
    cfg = load_config(d)
    assert cfg.error is None
    assert cfg.deny == {"one": {"terraform_dir": [f"{tmp_path}/infra", f"{tmp_path}/other"]}}
