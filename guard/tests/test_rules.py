import pytest

from treant_guard.rules import (
    BUILTIN_RESOLVERS,
    ConfigLookup,
    RuleError,
    default_rules,
    merge_rules,
    parse_rules,
)


def test_default_pack_loads_and_covers_tools():
    rules = default_rules()
    cmds = {c for r in rules for c in r.command}
    assert {
        "kubectl",
        "helm",
        "aws",
        "terraform",
        "terragrunt",
        "git",
        "ansible-playbook",
        "wrangler",
    } <= cmds
    assert {r.kind for r in rules} == {
        "kube_context",
        "aws_profile",
        "git_remote_host",
        "terraform_dir",
        "ansible_inventory",
        "cloudflare_account",
    }
    assert all(r.source == "default.toml" for r in rules)


def test_default_pack_values():
    by_cmd = {r.command[0]: r for r in default_rules()}
    ans = by_cmd["ansible-playbook"]
    assert ans.config == ConfigLookup(
        files=("$ANSIBLE_CONFIG", "./ansible.cfg", "~/.ansible.cfg", "/etc/ansible/ansible.cfg"),
        format="ini",
        key="defaults.inventory",
        first_found_only=True,
    )
    assert ans.all_values and ans.short_flag == "-i" and ans.split == ","
    wr = by_cmd["wrangler"]
    assert wr.skip_subcommands == (
        "dev", "whoami", "login", "logout", "types", "init", "docs", "help", "--version", "-v",
    )  # fmt: skip
    assert wr.config == ConfigLookup(
        files=("wrangler.json", "wrangler.jsonc", "wrangler.toml"),
        format="auto",
        key="account_id",
        first_found_only=True,
        search="upward",
    )
    assert by_cmd["terraform"].match == "path_prefix"
    assert by_cmd["git"].subcommands == ("push",)
    assert by_cmd["kubectl"].default_from == ("kubectl", "config", "current-context")
    assert by_cmd["aws"].all_values and by_cmd["aws"].env == ("AWS_PROFILE", "AWS_DEFAULT_PROFILE")
    for cmd in ("kubectl", "helm"):
        assert by_cmd[cmd].unknown_if == ("--kubeconfig", "$KUBECONFIG")
    assert BUILTIN_RESOLVERS == ("git_push_host", "terraform_dir")


@pytest.mark.parametrize(
    "bad",
    [
        {"command": ["x"], "kind": "k", "flagz": ["-x"]},
        {"command": ["x"], "kind": "k", "flags": "--x"},
        {"command": [], "kind": "k"},
        {"command": ["x"], "kind": ""},
        {"command": ["x"], "kind": "k", "subcommands": ["a"], "skip_subcommands": ["b"]},
        {"command": ["x"], "kind": "k", "resolver": "nope"},
        {"command": ["x"], "kind": "k", "resolver": "terraform_dir", "flags": ["-x"]},
        {"command": ["x"], "kind": "k", "resolver": "terraform_dir", "env": ["A"]},
        {"command": ["x"], "kind": "k", "resolver": "terraform_dir", "default_from": ["a"]},
        {"command": ["x"], "kind": "k", "resolver": "terraform_dir", "config": {"files": ["a"]}},
        {"command": ["x"], "kind": "k", "match": "glob"},
        {"command": ["x"], "kind": "k", "source": "evil"},
        {"command": ["x"], "kind": "k", "config": {"files": []}},
        {"command": ["x"], "kind": "k", "config": {"files": ["a"], "format": "yaml"}},
        {"command": ["x"], "kind": "k", "config": {"files": ["a"], "search": "down"}},
        {"command": ["x"], "kind": "k", "config": {"files": ["a"], "bogus": 1}},
        {"command": ["x"], "kind": "k", "all_values": "yes"},
        {"command": ["x"], "kind": "k", "unknown_if": "--x"},
        {"command": ["x"], "kind": "k", "unknown_if": ["x"]},
        {"command": ["x"], "kind": "k", "unknown_if": ["$"]},
        {"command": ["x"], "kind": "k", "unknown_if": ["$1X"]},
        {"command": ["x"], "kind": "k", "unknown_if": ["$A/b"]},
        {"command": ["x"], "kind": "k", "unknown_if": ["-"]},
        {"command": ["x"], "kind": "k", "resolver": "terraform_dir", "unknown_if": ["-x"]},
    ],
)
def test_invalid_rules_raise(bad):
    with pytest.raises(RuleError, match=r"user\.toml.*rule 0"):
        parse_rules({"rule": [bad]}, "user.toml")


def test_missing_rule_key_is_empty():
    assert parse_rules({}, "x.toml") == []


def test_parse_sets_source_and_tuples():
    (r,) = parse_rules({"rule": [{"command": ["x"], "kind": "k", "flags": ["-a"]}]}, "u.toml")
    assert r.source == "u.toml" and r.flags == ("-a",) and r.command == ("x",)


def test_override_replaces_and_disable_removes():
    base = parse_rules(
        {
            "rule": [
                {"command": ["kubectl"], "kind": "kube_context", "flags": ["--context"]},
                {"command": ["git"], "kind": "git_remote_host", "resolver": "git_push_host"},
            ]
        },
        "pack",
    )
    over = parse_rules(
        {
            "rule": [
                {"command": ["kubectl"], "kind": "kube_context", "flags": ["--ctx"]},
                {"command": ["git"], "kind": "git_remote_host", "disabled": True},
            ]
        },
        "user",
    )
    merged = merge_rules(base, over)
    assert [(r.command, r.flags) for r in merged] == [(("kubectl",), ("--ctx",))]


def test_merge_order_and_append():
    def mk(cmd, kind, **kw):
        return {"command": cmd, "kind": kind, **kw}

    base = parse_rules({"rule": [mk(["a"], "k"), mk(["b"], "k"), mk(["c"], "k")]}, "p")
    over = parse_rules(
        {"rule": [mk(["new"], "k"), mk(["b"], "k", flags=["-b"]), mk(["zz"], "k", disabled=True)]},
        "u",
    )
    merged = merge_rules(base, over)
    assert [r.command[0] for r in merged] == ["a", "b", "c", "new"]
    assert merged[1].flags == ("-b",)
    assert merge_rules(base, []) == base


def test_unresolved_if_parses():
    (rule,) = parse_rules(
        {"rule": [{"command": ["x"], "kind": "k", "unknown_if": ["--cfg", "-c", "$X_1"]}]},
        "u.toml",
    )
    assert rule.unknown_if == ("--cfg", "-c", "$X_1")


GIT_REMOTE = ("add", "set-url", "rename", "remove", "rm")


def test_default_switches():
    from treant_guard.rules import Switch, default_switches

    kube = ("kube_context",)
    assert default_switches() == [
        Switch(("kubectx",), kube),
        Switch(("kubectl",), kube, ("config", "use-context")),
        Switch(("kubectl",), kube, ("config", "use")),
        Switch(("kubectl",), kube, ("config", "set-context")),
        Switch(("kubectl",), kube, ("config", "set")),
        Switch(("kubectl",), kube, ("config", "rename-context")),
        Switch(("kubectl",), kube, ("config", "unset")),
        Switch(("aws",), ("aws_profile",), ("configure",)),
        *(Switch(("git",), ("git_remote_host",), ("remote", sub)) for sub in GIT_REMOTE),
        Switch(("git",), ("git_remote_host",), ("config",)),
    ]


@pytest.mark.parametrize(
    "bad",
    [
        {"command": ["x"]},
        {"command": ["x"], "kinds": []},
        {"command": "x", "kinds": ["k"]},
        {"command": [], "kinds": ["k"]},
        {"command": ["x"], "kinds": ["k"], "args": "a"},
        {"command": ["x"], "kinds": ["k"], "bogus": 1},
        "x",
    ],
)
def test_bad_switch_is_invalid(bad):
    from treant_guard.rules import parse_switch

    with pytest.raises(ValueError):
        parse_switch(bad)


@pytest.mark.parametrize(
    "bad,message",
    [
        ({"command": ["x", ""], "kinds": ["k"]}, "switch: command must be a non-empty list"),
        ({"command": ["x"], "kinds": ["k", ""]}, "switch: kinds must be a non-empty list"),
        ({"command": ["x"], "kinds": ["k"], "args": ["a", ""]}, "switch: args must not hold"),
    ],
)
def test_switch_empty_strings_are_invalid(bad, message):
    from treant_guard.rules import parse_switch

    with pytest.raises(ValueError, match=message):
        parse_switch(bad)


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ({"command": ["x", ""], "kind": "k"}, "command must not hold an empty string"),
        ({"command": ["x"], "kind": "k", "flags": [""]}, "flags must not hold an empty string"),
        ({"command": ["x"], "kind": "k", "short_flag": ""}, "short_flag must be"),
        ({"command": ["x"], "kind": "k", "short_flag": "--x"}, "short_flag must be"),
        ({"command": ["x"], "kind": "k", "subcommands": []}, "subcommands must be a non-empty"),
        ({"command": ["x"], "kind": "k", "subcommands": [""]}, "subcommands must not hold"),
        ({"command": ["x"], "kind": "k", "skip_subcommands": [""]}, "skip_subcommands must not"),
        ({"command": ["x"], "kind": "k", "env": [""]}, "env must not hold"),
        ({"command": ["x"], "kind": "k", "default_from": []}, "default_from must be a non-empty"),
        ({"command": ["x"], "kind": "k", "default_from": [""]}, "default_from must not hold"),
        ({"command": ["x"], "kind": "k", "global_flags_with_value": [""]}, "global_flags_with"),
        ({"command": ["x"], "kind": "k", "config": {"files": [""]}}, "config.files must not"),
        ({"command": ["x"], "kind": "k", "split": ""}, "split must not be empty"),
    ],
)
def test_empty_entries_are_invalid(bad, message):
    """An empty word never matches, so a rule holding one would silently check nothing."""
    with pytest.raises(RuleError, match=message):
        parse_rules({"rule": [bad]}, "user.toml")


def test_duplicate_override_last_wins():
    over = parse_rules(
        {
            "rule": [
                {"command": ["kubectl"], "kind": "kube_context", "flags": ["--one"]},
                {"command": ["kubectl"], "kind": "kube_context", "flags": ["--two"]},
            ]
        },
        "user",
    )
    merged = merge_rules(default_rules(), over)
    (kubectl,) = [r for r in merged if r.command == ("kubectl",)]
    assert kubectl.flags == ("--two",)
    assert [r.command for r in merged] == [r.command for r in default_rules()]
