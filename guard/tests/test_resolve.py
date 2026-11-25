import os
import subprocess
import time
from dataclasses import replace

import pytest

from treant_guard.resolve import read_config_value, targets_for
from treant_guard.rules import ConfigLookup, Rule, default_rules, default_switches


def _no_run(argv, **kw):
    return subprocess.CompletedProcess(argv, 1, "", "")


def _rule(kind):
    return next(r for r in default_rules() if r.kind == kind)


# --- fallback ---------------------------------------------------------------


def test_fallback_missing_binary_is_unresolved():
    def run(argv, **kw):
        raise FileNotFoundError

    assert targets_for("kubectl get po", cwd="/x", environ={}, rules=default_rules(), run=run) == (
        True,
        [],
        ["kube_context"],
    )


def test_fallback_timeout_nonzero_and_empty_are_unresolved():
    def timeout(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    def empty(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, "  \n", "")

    for run in (timeout, empty, _no_run):
        out = targets_for("kubectl get po", cwd="/x", environ={}, rules=default_rules(), run=run)
        assert out == (True, [], ["kube_context"])


def test_fallback_called_once_per_check():
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, " ctx-a \nsecond\n", "")

    out = targets_for(
        "kubectl get po; kubectl get no", cwd="/x", environ={}, rules=default_rules(), run=run
    )
    assert out == (True, [("kube_context", "ctx-a")], [])
    assert calls == [
        (
            ["kubectl", "config", "current-context"],
            {"capture_output": True, "text": True, "timeout": 1, "cwd": "/x", "env": {}},
        )
    ]
    targets_for("kubectl get po", cwd="/x", environ={}, rules=default_rules(), run=run)
    assert len(calls) == 2


def test_empty_fallback_means_none():
    rules = [Rule(command=("tool",), kind="k", default_from=())]
    assert targets_for("tool x", cwd="/x", environ={}, rules=rules, run=_no_run) == (
        True,
        [],
        ["k"],
    )


# --- flags and env ----------------------------------------------------------


def test_flags_last_wins_and_beat_env():
    environ = {"AWS_PROFILE": "env"}
    single = [replace(_rule("aws_profile"), all_values=False)]
    out = targets_for("aws --profile a s3 ls --profile=b", cwd="/x", environ=environ, rules=single)
    assert out == (True, [("aws_profile", "b")], [])


def test_env_prefix_beats_environ_and_order_of_vars():
    rules = [replace(_rule("aws_profile"), all_values=False)]
    environ = {"AWS_PROFILE": "env", "AWS_DEFAULT_PROFILE": "dflt"}
    assert targets_for("aws s3 ls", cwd="/x", environ=environ, rules=rules)[1] == [
        ("aws_profile", "env")
    ]
    out = targets_for("AWS_DEFAULT_PROFILE=pre aws s3 ls", cwd="/x", environ=environ, rules=rules)
    assert out[1] == [("aws_profile", "pre")]
    only_default = {"AWS_DEFAULT_PROFILE": "dflt"}
    assert targets_for("aws s3 ls", cwd="/x", environ=only_default, rules=rules)[1] == [
        ("aws_profile", "dflt")
    ]
    out = targets_for("AWS_PROFILE= aws s3 ls", cwd="/x", environ=environ, rules=rules)
    assert out[1] == [("aws_profile", "dflt")]


def test_aws_checks_every_profile_variable():
    rules = default_rules()
    both = {"AWS_PROFILE": "a", "AWS_DEFAULT_PROFILE": "b"}
    assert targets_for("aws s3 ls", cwd="/x", environ=both, rules=rules) == (
        True,
        [("aws_profile", "a"), ("aws_profile", "b")],
        [],
    )
    same = {"AWS_PROFILE": "a", "AWS_DEFAULT_PROFILE": "a"}
    assert targets_for("aws s3 ls", cwd="/x", environ=same, rules=rules)[1] == [
        ("aws_profile", "a")
    ]
    out = targets_for("AWS_DEFAULT_PROFILE=pre aws s3 ls", cwd="/x", environ=both, rules=rules)
    assert out[1] == [("aws_profile", "a"), ("aws_profile", "pre")]
    out = targets_for("AWS_PROFILE= aws s3 ls", cwd="/x", environ=both, rules=rules)
    assert out[1] == [("aws_profile", "b")]
    out = targets_for("aws --profile p --profile q s3 ls", cwd="/x", environ=both, rules=rules)
    assert out == (True, [("aws_profile", "p"), ("aws_profile", "q")], [])


def test_multiple_flags_are_not_split():
    out = targets_for(
        "ansible-playbook -i a,b x.yml", cwd="/x", environ={}, rules=[_rule("ansible_inventory")]
    )
    assert out == (True, [("ansible_inventory", "a,b")], [])


# --- subcommands ------------------------------------------------------------


def test_subcommand_skips_global_flag_values():
    def run(argv, **kw):
        out = "origin\thttps://h.example/a.git (push)\n"
        return subprocess.CompletedProcess(argv, 0, out, "")

    rules = [_rule("git_remote_host")]
    for cmd in ("git -c push.x=y push", "git --namespace x push", "git --no-pager push"):
        assert targets_for(cmd, cwd="/r", environ={}, rules=rules, run=run) == (
            True,
            [("git_remote_host", "h.example")],
            [],
        )
    for cmd in ("git -C push status", "git", "git --version"):
        assert targets_for(cmd, cwd="/r", environ={}, rules=rules, run=run) == (False, [], [])


def test_skip_subcommands_need_a_subcommand():
    rules = [_rule("cloudflare_account")]
    for cmd in ("wrangler", "wrangler --version", "wrangler whoami"):
        assert targets_for(cmd, cwd="/x", environ={}, rules=rules) == (False, [], [])


# --- git_push_host ----------------------------------------------------------


def test_git_push_host_strips_userinfo_and_ignores_fetch_lines():
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        out = (
            "origin\thttps://fetch.example/a.git (fetch)\n"
            "origin\thttps://user:pw@Push.Example:8443/a.git (push)\n"
        )
        return subprocess.CompletedProcess(argv, 0, out, "")

    out = targets_for(
        "git push; git push origin", cwd="/repo", environ={}, rules=default_rules(), run=run
    )
    assert out == (True, [("git_remote_host", "push.example")], [])
    assert calls == [
        (
            ["git", "-C", "/repo", "remote", "-v"],
            {"capture_output": True, "text": True, "timeout": 1},
        )
    ]


def test_git_push_redirect_is_not_the_remote():
    def run(argv, **kw):
        out = "origin\thttps://push.example/a.git (push)\n"
        return subprocess.CompletedProcess(argv, 0, out, "")

    for command in ("git push 2>&1 | tail -5", "git push > /tmp/log", "git push -u 2>/dev/null"):
        out = targets_for(command, cwd="/repo", environ={}, rules=default_rules(), run=run)
        assert out == (True, [("git_remote_host", "push.example")], []), command


def test_git_push_host_remote_lookup_failure_is_unresolved():
    def run(argv, **kw):
        raise OSError

    rules = default_rules()
    assert targets_for("git push", cwd="/r", environ={}, rules=rules, run=run) == (
        True,
        [],
        ["git_remote_host"],
    )
    out = targets_for("git push git@h.example:a/b", cwd="/r", environ={}, rules=rules, run=run)
    assert out[1] == [("git_remote_host", "h.example")]


# --- config files -----------------------------------------------------------


def test_config_file_garbage_is_safe(tmp_path):
    (tmp_path / "wrangler.toml").write_bytes(b"\xff\x00" * 70000)
    out = targets_for("wrangler deploy", cwd=str(tmp_path), environ={}, rules=default_rules())
    assert out == (True, [], ["cloudflare_account"])


def test_wrangler_upward_and_env(tmp_path):
    (tmp_path / "wrangler.toml").write_text('account_id = "abc"\n')
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    rules = default_rules()
    assert targets_for("wrangler deploy", cwd=str(sub), environ={}, rules=rules)[1] == [
        ("cloudflare_account", "abc")
    ]
    assert targets_for(
        "wrangler deploy", cwd=str(sub), environ={"CLOUDFLARE_ACCOUNT_ID": "env"}, rules=rules
    )[1] == [("cloudflare_account", "env")]
    assert targets_for("wrangler dev", cwd=str(sub), environ={}, rules=rules) == (False, [], [])


def test_wrangler_nearest_directory_wins(tmp_path):
    (tmp_path / "wrangler.toml").write_text('account_id = "parent"\n')
    sub = tmp_path / "a"
    sub.mkdir()
    (sub / "wrangler.json").write_text('{"account_id": "child"}')
    out = targets_for("wrangler deploy", cwd=str(sub), environ={}, rules=default_rules())
    assert out == (True, [("cloudflare_account", "child")], [])


def test_wrangler_reads_the_nearest_config_only(tmp_path):
    (tmp_path / "wrangler.toml").write_text('account_id = "staging"\n')
    svc = tmp_path / "svc"
    svc.mkdir()
    (svc / "wrangler.toml").write_text('name = "x"\n')
    out = targets_for("wrangler deploy", cwd=str(svc), environ={}, rules=default_rules())
    assert out == (True, [], ["cloudflare_account"])


def test_hostile_jsonc_is_linear(tmp_path):
    (tmp_path / "wrangler.jsonc").write_text('"\\' * 32768)
    start = time.perf_counter()
    out = targets_for("wrangler deploy", cwd=str(tmp_path), environ={}, rules=default_rules())
    assert time.perf_counter() - start < 0.2
    assert out == (True, [], ["cloudflare_account"])  # not JSON: no value, and no hang


def test_wrangler_jsonc(tmp_path):
    (tmp_path / "wrangler.jsonc").write_text('{ // c\n "account_id": "j" /* x */ }')
    out = targets_for("wrangler deploy", cwd=str(tmp_path), environ={}, rules=default_rules())
    assert out[1] == [("cloudflare_account", "j")]


def test_jsonc_keeps_comment_markers_inside_strings(tmp_path):
    (tmp_path / "c.jsonc").write_text('/* head */ {"a": {"b": "x//y/*z*/\\"q"}, // tail\n "n": 1}')
    lookup = ConfigLookup(files=("c.jsonc",), key="a.b")
    assert read_config_value(lookup, str(tmp_path), {}, "/nohome") == (True, 'x//y/*z*/"q')


def test_config_formats_and_dotted_keys(tmp_path):
    (tmp_path / "a.toml").write_text('[s]\nk = "t"\n')
    (tmp_path / "a.json").write_text('{"s": {"k": "j", "n": 3}}')
    (tmp_path / "a.cfg").write_text("[s]\nk = i ; c\n")
    (tmp_path / "noext").write_text("[s]\nk = forced\n")
    cwd = str(tmp_path)

    def read(files, key="s.k", fmt="auto"):
        return read_config_value(ConfigLookup(files=files, key=key, format=fmt), cwd, {}, "/h")

    assert read(("a.toml",)) == (True, "t")
    assert read(("a.json",)) == (True, "j")
    assert read(("./a.cfg",)) == (True, "i")
    assert read(("noext",), fmt="ini") == (True, "forced")
    assert read(("a.json",), key="s.n") == (True, None)
    assert read(("a.json",), key="s.missing") == (True, None)
    assert read(("absent.toml",)) == (False, None)


def test_config_without_first_found_only_continues(tmp_path):
    (tmp_path / "one.toml").write_text("other = 1\n")
    (tmp_path / "two.toml").write_text('k = "two"\n')
    lookup = ConfigLookup(files=("one.toml", "two.toml"), key="k")
    assert read_config_value(lookup, str(tmp_path), {}, "/h") == (True, "two")
    first = replace(lookup, first_found_only=True)
    assert read_config_value(first, str(tmp_path), {}, "/h") == (True, None)


def test_config_reads_at_most_64k(tmp_path):
    (tmp_path / "big.toml").write_text("# " + "x" * 70000 + '\nk = "late"\n')
    lookup = ConfigLookup(files=("big.toml",), key="k")
    assert read_config_value(lookup, str(tmp_path), {}, "/h") == (True, None)


def test_config_split_and_unset_var_entry(tmp_path):
    (tmp_path / "c.toml").write_text('k = " a , ,b "\n')
    rules = [
        Rule(
            command=("tool",),
            kind="k",
            config=ConfigLookup(files=("$UNSET_VAR", "c.toml"), key="k"),
            split=",",
        )
    ]
    out = targets_for("tool", cwd=str(tmp_path), environ={}, rules=rules)
    assert out == (True, [("k", "a"), ("k", "b")], [])


# --- ansible (ported from client/tests/test_guardenv.py) --------------------


def _cfg(path, body):
    path.write_text(body)
    return str(path)


def _ansible(cwd, environ, home, system_cfg=None, command="ansible-playbook site.yml"):
    rule = _rule("ansible_inventory")
    sys_path = system_cfg or str(cwd) + "/no-such-dir/ansible.cfg"
    files = tuple(sys_path if f == "/etc/ansible/ansible.cfg" else f for f in rule.config.files)
    rule = replace(rule, config=replace(rule.config, files=files))
    out = targets_for(command, cwd=str(cwd), environ=environ, rules=[rule], home=str(home))
    assert out[0] is True
    return out[1:]


def test_ansible_inventory_env_wins(tmp_path):
    _cfg(tmp_path / "ansible.cfg", "[defaults]\ninventory = from/cfg\n")
    env = {"ANSIBLE_INVENTORY": "a,b"}
    assert _ansible(tmp_path, env, tmp_path) == (
        [("ansible_inventory", "a"), ("ansible_inventory", "b")],
        [],
    )


def test_ansible_inventory_from_cwd_cfg(tmp_path):
    _cfg(tmp_path / "ansible.cfg", "[defaults]\ninventory = inventory/hosts.yml ; note\n")
    assert _ansible(tmp_path, {}, "/nohome") == ([("ansible_inventory", "inventory/hosts.yml")], [])


def test_ansible_config_env_beats_cwd_and_may_be_relative_or_a_dir(tmp_path):
    _cfg(tmp_path / "ansible.cfg", "[defaults]\ninventory = cwd-inv\n")
    (tmp_path / "conf").mkdir()
    _cfg(tmp_path / "conf" / "ansible.cfg", "[defaults]\ninventory = conf-inv\n")
    _cfg(tmp_path / "other.cfg", "[defaults]\ninventory = other-inv\n")
    assert _ansible(tmp_path, {"ANSIBLE_CONFIG": "other.cfg"}, "/nohome") == (
        [("ansible_inventory", "other-inv")],
        [],
    )
    assert _ansible(tmp_path, {"ANSIBLE_CONFIG": "conf"}, "/nohome") == (
        [("ansible_inventory", "conf-inv")],
        [],
    )


def test_ansible_config_prefix_wins(tmp_path):
    _cfg(tmp_path / "ansible.cfg", "[defaults]\ninventory = cwd-inv\n")
    _cfg(tmp_path / "prod.cfg", "[defaults]\ninventory = prod-inv\n")
    _cfg(tmp_path / "env.cfg", "[defaults]\ninventory = env-inv\n")
    cmd = "ANSIBLE_CONFIG=prod.cfg ansible-playbook s.yml"
    expected = ([("ansible_inventory", "prod-inv")], [])
    assert _ansible(tmp_path, {}, "/nohome", command=cmd) == expected
    assert _ansible(tmp_path, {"ANSIBLE_CONFIG": "env.cfg"}, "/nohome", command=cmd) == expected


def test_ansible_config_env_tilde_uses_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _cfg(home / "my.cfg", "[defaults]\ninventory = tilde-inv\n")
    assert _ansible(tmp_path, {"ANSIBLE_CONFIG": "~/my.cfg"}, home) == (
        [("ansible_inventory", "tilde-inv")],
        [],
    )


def test_ansible_inventory_home_then_system(tmp_path):
    home, sys_dir, cwd = tmp_path / "home", tmp_path / "etc", tmp_path / "cwd"
    for d in (home, sys_dir, cwd):
        d.mkdir()
    sys_cfg = _cfg(sys_dir / "ansible.cfg", "[defaults]\ninventory = sys-inv\n")
    assert _ansible(cwd, {}, home, sys_cfg) == ([("ansible_inventory", "sys-inv")], [])
    _cfg(home / ".ansible.cfg", "[defaults]\ninventory = home-inv\n")
    assert _ansible(cwd, {}, home, sys_cfg) == ([("ansible_inventory", "home-inv")], [])


def test_first_config_found_wins_even_without_inventory(tmp_path):
    """Ansible reads one config file only, so a cwd ansible.cfg without inventory means none."""
    home = tmp_path / "home"
    home.mkdir()
    _cfg(home / ".ansible.cfg", "[defaults]\ninventory = home-inv\n")
    _cfg(tmp_path / "ansible.cfg", "[defaults]\nforks = 5\n")
    assert _ansible(tmp_path, {}, home) == ([], ["ansible_inventory"])


def test_ansible_inventory_malformed_cfg_is_none(tmp_path):
    _cfg(tmp_path / "ansible.cfg", "inventory = no section header\n")
    assert _ansible(tmp_path, {}, "/nohome") == ([], ["ansible_inventory"])
    (tmp_path / "ansible.cfg").write_bytes(b"[defaults]\ninventory = \xff\xfe\n")
    targets, unresolved = _ansible(tmp_path, {}, "/nohome")
    assert len(targets) == 1 and unresolved == []


# --- terraform_dir and parse errors -----------------------------------------------


def test_tf_root_chdir_only_for_terraform():
    rules = default_rules()
    assert targets_for("terragrunt -chdir=x plan", cwd="/w", environ={}, rules=rules)[1] == [
        ("terraform_dir", "/w")
    ]
    assert targets_for("terraform -chdir=/abs plan", cwd="/w", environ={}, rules=rules)[1] == [
        ("terraform_dir", "/abs")
    ]


def test_unlexable_command():
    assert targets_for('kubectl "x', cwd="/x", environ={}, rules=default_rules()) == (
        True,
        [],
        ["command"],
    )


# --- switched config / unset variables ---------------------------------------


def _ops(argv, **kw):
    assert argv == ["kubectl", "config", "current-context"]
    return subprocess.CompletedProcess(argv, 0, "ops\n", "")


@pytest.mark.parametrize(
    "command,kind",
    [
        ("KUBECONFIG=~/.kube/config_startup kubectl delete pod x", "kube_context"),
        ("kubectl --kubeconfig ~/.kube/config_prod delete pod x", "kube_context"),
        ("kubectl --kubeconfig=/k delete pod x", "kube_context"),
        ("env KUBECONFIG= kubectl get po", "kube_context"),
        ("env -u KUBECONFIG kubectl get po", "kube_context"),
        ("helm --kubeconfig /k list", "kube_context"),
        ("KUBECONFIG=/k helm list", "kube_context"),
        ("env -u AWS_PROFILE aws s3 ls", "aws_profile"),
        ("AWS_PROFILE= aws s3 ls", "aws_profile"),
    ],
)
def test_switched_config_or_unset_var_is_unresolved(command, kind):
    environ = {"AWS_PROFILE": "ops"}
    out = targets_for(command, cwd="/x", environ=environ, rules=default_rules(), run=_ops)
    assert out == (True, [], [kind])


def test_ambient_values_still_resolve():
    environ = {"AWS_PROFILE": "ops"}
    rules = default_rules()
    assert targets_for("kubectl get po", cwd="/x", environ=environ, rules=rules, run=_ops) == (
        True,
        [("kube_context", "ops")],
        [],
    )
    assert targets_for("aws s3 ls", cwd="/x", environ=environ, rules=rules)[1] == [
        ("aws_profile", "ops")
    ]


def test_flags_win_over_unresolved_if():
    out = targets_for(
        "KUBECONFIG=/k kubectl --kubeconfig x --context ops get po",
        cwd="/x",
        environ={},
        rules=default_rules(),
        run=_ops,
    )
    assert out == (True, [("kube_context", "ops")], [])


def test_empty_prefix_var_stops_environ_for_config_lookup(tmp_path):
    env_cfg = tmp_path / "env.cfg"
    env_cfg.write_text("[defaults]\ninventory = from-env\n")
    (tmp_path / "ansible.cfg").write_text("[defaults]\ninventory = from-cwd\n")
    environ = {"ANSIBLE_CONFIG": str(env_cfg)}
    rules = [_rule("ansible_inventory")]
    for command, want in (
        ("ansible-playbook x.yml", "from-env"),
        ("ANSIBLE_CONFIG= ansible-playbook x.yml", "from-cwd"),
        ("env -u ANSIBLE_CONFIG ansible-playbook x.yml", "from-cwd"),
    ):
        out = targets_for(command, cwd=str(tmp_path), environ=environ, rules=rules, home="/none")
        assert out[1] == [("ansible_inventory", want)], command


# --- same-line state --------------------------------------------------------


def _tf(command, cwd, environ=None, home="/none"):
    """The terraform_dir targets and unknown kinds of a line."""
    out = targets_for(
        command, cwd=str(cwd), environ=environ or {}, rules=default_rules(), run=_ops, home=home
    )
    return out[1], out[2]


def _aws(command, environ, cwd="/x"):
    out = targets_for(command, cwd=cwd, environ=environ, rules=default_rules(), run=_ops)
    return out[1], out[2]


UNKNOWN_TF = ([], ["terraform_dir"])
UNKNOWN_AWS = ([], ["aws_profile"])
UNKNOWN_KUBE = ([], ["kube_context"])


def _cant_tell(out, kind):
    """No target, and ``kind`` among the unknown kinds (a command the guard can't name makes
    every kind unknown)."""
    return out[0] == [] and kind in out[1]


def test_cd_then_terraform_uses_new_dir(tmp_path):
    (tmp_path / "prod" / "stack").mkdir(parents=True)
    (tmp_path / "dev").mkdir()
    prod = str(tmp_path / "prod")
    assert _tf("cd prod && terraform apply", tmp_path) == ([("terraform_dir", prod)], [])
    assert _tf("cd -P prod; terraform apply", tmp_path) == ([("terraform_dir", prod)], [])
    assert _tf(f"cd {prod}\nterraform apply", "/x") == ([("terraform_dir", prod)], [])
    assert _tf("cd dev && cd ../prod && terraform apply", tmp_path) == (
        [("terraform_dir", prod)],
        [],
    )
    assert _tf("cd prod && terraform -chdir=stack plan", tmp_path) == (
        [("terraform_dir", prod + "/stack")],
        [],
    )
    # the state-changing segment itself, and segments before it, use the old folder
    assert _tf("terraform plan; cd prod; terraform plan", tmp_path) == (
        [("terraform_dir", str(tmp_path)), ("terraform_dir", prod)],
        [],
    )


def test_cd_home_and_tilde(tmp_path):
    home = tmp_path / "home"
    (home / "infra").mkdir(parents=True)
    for command, want in (
        ("cd && terraform plan", home),
        ("cd ~ && terraform plan", home),
        ("cd ~/infra && terraform plan", home / "infra"),
    ):
        assert _tf(command, "/x", home=str(home)) == ([("terraform_dir", str(want))], []), command


def test_pushd_like_cd(tmp_path):
    (tmp_path / "prod").mkdir()
    prod = str(tmp_path / "prod")
    assert _tf("pushd prod && terraform plan", tmp_path) == ([("terraform_dir", prod)], [])
    for command in ("pushd && terraform plan", "pushd +1; terraform plan"):
        assert _tf(command, tmp_path) == UNKNOWN_TF, command


def test_cd_dollar_is_unknown(tmp_path):
    (tmp_path / "prod").mkdir()
    for command in (
        "cd $DIR && terraform plan",
        'cd "$DIR" && terraform plan',
        "cd ${DIR}/x && terraform plan",
        "cd `git rev-parse --show-toplevel` && terraform plan",
        "cd $(git rev-parse --show-toplevel) && terraform plan",
        "cd prod* && terraform plan",
    ):
        assert _tf(command, tmp_path) == UNKNOWN_TF, command
    environ = {"AWS_PROFILE": "ops"}
    assert _aws("cd $DIR; aws s3 ls", environ) == UNKNOWN_AWS
    # a flag value still counts
    assert _aws("cd $DIR; aws --profile dev s3 ls", environ) == ([("aws_profile", "dev")], [])


def test_cd_dash_and_popd_are_unknown(tmp_path):
    (tmp_path / "prod").mkdir()
    for command in (
        "cd - && terraform plan",
        "popd && terraform plan",
        "pushd prod; popd; terraform plan",
        "cd prod old && terraform plan",
        "cd ~other && terraform plan",
    ):
        assert _tf(command, tmp_path) == UNKNOWN_TF, command


def test_cd_that_may_not_happen_is_unknown(tmp_path):
    (tmp_path / "prod").mkdir()
    for command in (
        "cd missing; terraform plan",
        "if ! cd prod; then terraform plan; fi",
        "if cd prod; then echo; else terraform plan; fi",
        "if test -d prod; then cd prod; fi; terraform plan",
        "{ cd prod; } | cat; terraform plan",
        "command cd prod; terraform plan",
        "time cd prod; terraform plan",
        "echo | cd prod; terraform plan",
    ):
        assert _tf(command, tmp_path) == UNKNOWN_TF, command
    assert _tf("builtin cd prod; terraform plan", tmp_path) == (
        [("terraform_dir", str(tmp_path / "prod"))],
        [],
    )
    for environ in ({"CDPATH": "/elsewhere"}, {}):
        assert _tf("export CDPATH=/elsewhere; cd prod; terraform plan", tmp_path, environ) == (
            UNKNOWN_TF
        )
    assert _tf("cd prod; terraform plan", tmp_path, {"CDPATH": "/e"}) == UNKNOWN_TF
    assert _tf("cd ./prod; terraform plan", tmp_path, {"CDPATH": "/e"}) == (
        [("terraform_dir", str(tmp_path / "prod"))],
        [],
    )


def test_export_then_aws_uses_value():
    environ = {"AWS_PROFILE": "ops"}
    for command in (
        "export AWS_PROFILE=dev && aws s3 ls",
        "export A=1 AWS_PROFILE=dev; aws s3 ls",
        "export AWS_PROFILE=prod; export AWS_PROFILE=dev; aws s3 ls",
    ):
        assert _aws(command, environ) == ([("aws_profile", "dev")], []), command
    # the command's own prefix wins over the line's
    assert _aws("export AWS_PROFILE=dev; AWS_PROFILE=x aws s3 ls", environ) == (
        [("aws_profile", "x")],
        [],
    )
    # the exporting segment itself sees the old value
    assert _aws("aws s3 ls; export AWS_PROFILE=dev", environ) == ([("aws_profile", "ops")], [])
    # `export A` without a value changes nothing
    assert _aws("export AWS_PROFILE; aws s3 ls", environ) == ([("aws_profile", "ops")], [])


def test_unset_then_aws_reads_nothing():
    environ = {"AWS_PROFILE": "ops"}
    for command in ("unset AWS_PROFILE; aws s3 ls", "unset -v X AWS_PROFILE && aws s3 ls"):
        assert _aws(command, environ) == UNKNOWN_AWS, command
    assert _aws("export -n AWS_PROFILE; aws s3 ls", environ) == UNKNOWN_AWS


def test_lone_assignment_needs_export():
    # not in the hook environment: a plain shell variable, aws does not see it
    assert _aws("AWS_PROFILE=dev; aws s3 ls", {}) == UNKNOWN_AWS
    assert _aws("AWS_PROFILE=dev; aws s3 ls", {"AWS_DEFAULT_PROFILE": "ops"}) == (
        [("aws_profile", "ops")],
        [],
    )
    # in the hook environment: already exported, the new value counts
    assert _aws("AWS_PROFILE=dev; aws s3 ls", {"AWS_PROFILE": "ops"}) == (
        [("aws_profile", "dev")],
        [],
    )
    # exported later on the line, or after allexport
    for command in (
        "AWS_PROFILE=dev; export AWS_PROFILE; aws s3 ls",
        "export AWS_PROFILE=x; AWS_PROFILE=dev; aws s3 ls",
        "set -a; AWS_PROFILE=dev; aws s3 ls",
        "set -o allexport; AWS_PROFILE=dev; aws s3 ls",
    ):
        assert _aws(command, {}) == ([("aws_profile", "dev")], []), command
    assert _aws("set -a; set +a; AWS_PROFILE=dev; aws s3 ls", {}) == UNKNOWN_AWS
    assert _aws("unset AWS_PROFILE; AWS_PROFILE=dev; aws s3 ls", {"AWS_PROFILE": "o"}) == (
        UNKNOWN_AWS
    )


def test_declare_x_is_export():
    """`local` is not here: its names are unknown (test_local_makes_its_names_unknown).
    Nor are `readonly` and `declare -rx` (test_readonly_names_are_unknown_...)."""
    for command in (
        "declare -x AWS_PROFILE=dev; aws s3 ls",
        "typeset -x AWS_PROFILE=dev; aws s3 ls",
    ):
        assert _aws(command, {}) == ([("aws_profile", "dev")], []), command
    command = "declare AWS_PROFILE=dev; aws s3 ls"
    assert _aws(command, {}) == UNKNOWN_AWS, command
    assert _aws(command, {"AWS_PROFILE": "ops"}) == ([("aws_profile", "dev")], []), command
    assert _aws("declare +x AWS_PROFILE; aws s3 ls", {"AWS_PROFILE": "ops"}) == UNKNOWN_AWS


def test_dollar_value_is_unknown(tmp_path):
    environ = {"AWS_PROFILE": "ops"}
    for command in (
        "export AWS_PROFILE=$(cat f); aws s3 ls",
        "export AWS_PROFILE=`cat f`; aws s3 ls",
        "export AWS_PROFILE=$X && aws s3 ls",
        "AWS_PROFILE=${X}; aws s3 ls",
        "declare -x AWS_PROFILE=$X; aws s3 ls",
    ):
        assert _aws(command, environ) == UNKNOWN_AWS, command
    assert _aws("export AWS_PROFILE=$X; aws --profile dev s3 ls", environ) == (
        [("aws_profile", "dev")],
        [],
    )
    assert _aws("export AWS_PROFILE=$X; AWS_PROFILE=dev aws s3 ls", environ) == (
        [("aws_profile", "dev")],
        [],
    )
    # a $VAR config-file entry
    _cfg(tmp_path / "ansible.cfg", "[defaults]\ninventory = from-cwd\n")
    out = targets_for(
        "export ANSIBLE_CONFIG=$X; ansible-playbook site.yml",
        cwd=str(tmp_path),
        environ={},
        rules=default_rules(),
        home="/none",
    )
    assert out == (True, [], ["ansible_inventory"])
    # a known value is read like the command's own prefix
    (tmp_path / "other.cfg").write_text("[defaults]\ninventory = from-other\n")
    out = targets_for(
        f"export ANSIBLE_CONFIG={tmp_path}/other.cfg; ansible-playbook site.yml",
        cwd=str(tmp_path),
        environ={},
        rules=default_rules(),
        home="/none",
    )
    assert out == (True, [("ansible_inventory", "from-other")], [])


def test_export_kubeconfig_makes_kubectl_unknown():
    rules = default_rules()
    for command, environ in (
        ("export KUBECONFIG=/k; kubectl get po", {}),
        ("export KUBECONFIG=$X; kubectl get po", {}),
        ("KUBECONFIG=/k; kubectl get po", {"KUBECONFIG": "/a"}),
        ("unset KUBECONFIG; helm list", {"KUBECONFIG": "/a"}),
    ):
        out = targets_for(command, cwd="/x", environ=environ, rules=rules, run=_ops)
        assert out == (True, [], ["kube_context"]), command
    out = targets_for("KUBECONFIG=/k; kubectl get po", cwd="/x", environ={}, rules=rules, run=_ops)
    assert out == (True, [("kube_context", "ops")], [])


def test_or_after_state_change_is_unknown(tmp_path):
    (tmp_path / "prod").mkdir()
    for command in (
        "cd prod || true; terraform plan",
        "cd prod || terraform plan",
        "cd prod && echo || terraform plan",
    ):
        assert _tf(command, tmp_path) == UNKNOWN_TF, command
    environ = {"AWS_PROFILE": "ops"}
    assert _aws("export AWS_PROFILE=dev || exit; aws s3 ls", environ) == UNKNOWN_AWS
    assert _aws("cd /tmp || aws --profile dev s3 ls", environ) == ([("aws_profile", "dev")], [])
    # no state change: `||` changes nothing
    assert _aws("kubectl get po || aws s3 ls", environ) == (
        [("kube_context", "ops"), ("aws_profile", "ops")],
        [],
    )


def test_subshell_state_does_not_carry(tmp_path):
    """A change inside ( … ), before | or & is not followed; what comes after can't tell."""
    (tmp_path / "prod").mkdir()
    for command in (
        "(cd prod && terraform plan); terraform plan",
        "cd prod | cat; terraform plan",
        "cd prod & terraform plan",
        "(cd prod) && terraform plan",
        "( cd prod; cd prod ) || terraform plan",
        "echo $(cd prod); terraform plan",
    ):
        assert _tf(command, tmp_path) == UNKNOWN_TF, command
    environ = {"AWS_PROFILE": "ops"}
    for command in (
        "export AWS_PROFILE=dev | cat; aws s3 ls",
        "(export AWS_PROFILE=dev); aws s3 ls",
        "export AWS_PROFILE=dev & aws s3 ls",
    ):
        assert _aws(command, environ) == UNKNOWN_AWS, command
    # groups without a state change read as usual
    assert _tf("(terraform plan) | cat; terraform plan", tmp_path) == (
        [("terraform_dir", str(tmp_path))],
        [],
    )


@pytest.mark.parametrize(
    "prefix",
    [
        "source x",
        ". ./x",
        "eval x",
        "alias kubectl=echo",
        "f() { echo; }",
        "f () { echo; }",
        "function f { echo; }",
        "function f() { echo; }",
        "exec 3>f",
        "exec",
        "command eval x",
        "(source x)",
    ],
)
def test_source_makes_flags_untrusted(prefix):
    # an alias that names a guarded program also makes every other kind unknown
    every = prefix.startswith("alias")
    command = f"{prefix}; kubectl --context ops get ns"
    out = targets_for(command, cwd="/x", environ={}, rules=default_rules(), run=_ops)
    assert out[:2] == (True, []) and out[2][0] == "kube_context"
    assert len(out[2]) == (6 if every else 1)
    # segments before it are read as usual
    command = f"kubectl --context ops get ns; {prefix}"
    out = targets_for(command, cwd="/x", environ={}, rules=default_rules(), run=_ops)
    assert out[:2] == (True, [("kube_context", "ops")])
    assert len(out[2]) == (6 if every else 0)


def test_loop_with_state_change_is_unknown(tmp_path):
    (tmp_path / "a").mkdir()
    for command in (
        "for d in a b; do cd $d; terraform plan; done",
        "for d in a; do cd a; terraform -chdir=/abs plan; done",
        "terraform -chdir=/abs plan; while read x; do export A=1; done",
        "until false; do (cd a); done; terraform -chdir=/abs plan",
        "for f in x; do source $f; done; terraform -chdir=/abs plan",
    ):
        assert _tf(command, tmp_path) == UNKNOWN_TF, command
    # a loop without a state change is read as usual (no `$x` in kubectl's words)
    out = targets_for(
        "for x in 1 2; do kubectl --context ops get po; done",
        cwd="/x",
        environ={},
        rules=default_rules(),
    )
    assert out == (True, [("kube_context", "ops")], [])


def test_keywords_expose_command():
    rules = default_rules()
    for command in (
        "if true; then kubectl --context prod get ns; fi",
        "if ! kubectl --context prod get ns; then echo; fi",
        "while true; do kubectl --context prod get ns; done",
        "{ kubectl --context prod get ns; }",
        "if false; then echo; elif true; then kubectl --context prod get ns; fi",
    ):
        out = targets_for(command, cwd="/x", environ={}, rules=rules, run=_ops)
        assert out == (True, [("kube_context", "prod")], []), command


def test_case_line_is_unknown():
    command = "case $x in a) kubectl --context ops get ns;; esac"
    out = targets_for(command, cwd="/x", environ={}, rules=default_rules(), run=_ops)
    assert out == (True, [], ["kube_context"])


def test_env_i_ignores_hook_env():
    environ = {"AWS_PROFILE": "ops"}
    for command in (
        "env -i aws s3 ls",
        "env - aws s3 ls",
        "env --ignore-environment aws s3 ls",
        "export AWS_PROFILE=dev; env -i aws s3 ls",
    ):
        assert _aws(command, environ) == UNKNOWN_AWS, command
    assert _aws("env -i AWS_PROFILE=dev aws s3 ls", environ) == ([("aws_profile", "dev")], [])
    # the default command reads the hook's environment, not the cleared one
    out = targets_for(
        "env -i kubectl get po", cwd="/x", environ={}, rules=default_rules(), run=_ops
    )
    assert out == (True, [], ["kube_context"])
    # only for that command
    assert _aws("env -i true; aws s3 ls", environ) == ([("aws_profile", "ops")], [])


def _remotes_by_dir(calls):
    def run(argv, **kw):
        calls.append(argv)
        host = {"/repo": "a.example", "/repo/sub": "b.example", "/other": "c.example"}[argv[2]]
        return subprocess.CompletedProcess(argv, 0, f"origin\tgit@{host}:x/y.git (push)\n", "")

    return run


def test_git_dash_c_reads_that_repo(tmp_path):
    (tmp_path / "sub").mkdir()
    calls = []
    run = _remotes_by_dir(calls)
    rules = default_rules()
    out = targets_for(
        "git push; git -C sub push; git -C /other push; git -C /other push",
        cwd="/repo",
        environ={},
        rules=rules,
        run=run,
    )
    assert out == (
        True,
        [
            ("git_remote_host", "a.example"),
            ("git_remote_host", "b.example"),
            ("git_remote_host", "c.example"),
        ],
        [],
    )
    assert [c[2] for c in calls] == ["/repo", "/repo/sub", "/other"]
    out = targets_for("git -C $D push", cwd="/repo", environ={}, rules=rules, run=run)
    assert out == (True, [], ["git_remote_host"])
    # after cd, the remotes of the new folder
    calls.clear()

    def run2(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "origin\tgit@d.example:x.git (push)\n", "")

    out = targets_for("cd sub && git push", cwd=str(tmp_path), environ={}, rules=rules, run=run2)
    assert out == (True, [("git_remote_host", "d.example")], [])
    assert calls == [["git", "-C", str(tmp_path / "sub"), "remote", "-v"]]


def test_git_dir_flag_is_unknown():
    calls = []
    run = _remotes_by_dir(calls)
    for command in (
        "git --git-dir=/x/.git push",
        "git --git-dir /x/.git push",
        "git --work-tree=/x push origin",
        "git --work-tree /x push",
    ):
        out = targets_for(command, cwd="/repo", environ={}, rules=default_rules(), run=run)
        assert out == (True, [], ["git_remote_host"]), command


def test_no_state_change_behaves_as_before(tmp_path):
    environ = {"AWS_PROFILE": "ops"}
    rules = default_rules()
    for command in (
        "kubectl get po && aws s3 ls | grep x; terraform plan",
        "echo a || kubectl --context dev get po & aws --profile p s3 ls",
        "(kubectl get po) ; terraform -chdir=sub plan",
        "X=1 aws s3 ls; kubectl get po",
    ):
        whole = targets_for(command, cwd=str(tmp_path), environ=environ, rules=rules, run=_ops)
        merged = (False, [], [])
        for part in command.replace("&&", ";").replace("||", ";").replace("&", ";").split(";"):
            for piece in part.replace("|", ";").replace("(", "").replace(")", "").split(";"):
                if not piece.strip():
                    continue
                m, t, u = targets_for(
                    piece, cwd=str(tmp_path), environ=environ, rules=rules, run=_ops
                )
                merged = (
                    merged[0] or m,
                    merged[1] + [x for x in t if x not in merged[1]],
                    merged[2] + [x for x in u if x not in merged[2]],
                )
        assert whole == merged, command


def test_switches_keyword_is_accepted():
    out = targets_for(
        "kubectl --context a get po", cwd="/x", environ={}, rules=default_rules(), switches=()
    )
    assert out == (True, [("kube_context", "a")], [])


def test_heredoc_body_is_not_followed(tmp_path):
    (tmp_path / "dev").mkdir()
    command = "cat > s.sh <<'EOF'\ncd dev\nEOF\nterraform apply"
    assert _tf(command, tmp_path) == UNKNOWN_TF
    # a here-document without state changes in it reads as usual
    calls = []
    command = "git commit -m \"$(cat <<'EOF'\nfix: x\nEOF\n)\" && git push"
    out = targets_for(
        command, cwd="/repo", environ={}, rules=default_rules(), run=_remotes_by_dir(calls)
    )
    assert out == (True, [("git_remote_host", "a.example")], [])


def test_and_or_list_makes_its_changes_unknown(tmp_path):
    (tmp_path / "dev").mkdir()
    dev = str(tmp_path / "dev")
    environ = {"AWS_PROFILE": "ops"}
    # later members of the same `&&` list see the change exactly
    assert _tf("cd dev && terraform apply", tmp_path) == ([("terraform_dir", dev)], [])
    assert _tf("true && cd dev && terraform apply", tmp_path) == ([("terraform_dir", dev)], [])
    # a single-member list stays exact
    assert _tf("cd dev; terraform apply", tmp_path) == ([("terraform_dir", dev)], [])
    # after the list ends, a change it may not have made is unknown
    for command in (
        "false && cd dev; terraform apply",
        "true || cd dev; terraform apply",
        "git diff --quiet && cd dev\nterraform apply",
        "cd dev && true; terraform apply",
    ):
        assert _tf(command, tmp_path) == UNKNOWN_TF, command
    for command in (
        '[ -z "$AWS_PROFILE" ] && export AWS_PROFILE=dev; aws s3 ls',
        "false && unset AWS_PROFILE; aws s3 ls",
        "false && AWS_PROFILE=dev; export AWS_PROFILE; aws s3 ls",
        "false && cd dev; aws s3 ls",
    ):
        assert _aws(command, environ) == UNKNOWN_AWS, command
    # a variable made unknown leaves the folder readable
    assert _tf("false && export AWS_PROFILE=dev; terraform apply", tmp_path) == (
        [("terraform_dir", str(tmp_path))],
        [],
    )
    # a change in a group, or followed by `||` anywhere later, is not followed
    assert _tf("(false && cd dev); terraform apply", tmp_path) == UNKNOWN_TF
    assert _tf("cd dev; true || echo; terraform apply", tmp_path) == UNKNOWN_TF
    # an `||` before any change changes nothing
    assert _tf("true || echo; cd dev; terraform apply", tmp_path) == (
        [("terraform_dir", dev)],
        [],
    )


def test_background_list_does_not_carry(tmp_path):
    """Segments after `&` can't tell; the backgrounded list itself reads exactly."""
    (tmp_path / "dev").mkdir()
    dev = str(tmp_path / "dev")
    assert _tf("cd dev && terraform plan & terraform apply", tmp_path) == (
        [("terraform_dir", dev)],
        ["terraform_dir"],
    )
    assert _aws(
        "export AWS_PROFILE=dev && aws s3 ls & aws s3 rm s3://b/k", {"AWS_PROFILE": "o"}
    ) == ([("aws_profile", "dev")], ["aws_profile"])


def test_comments_are_not_followed(tmp_path):
    """A possible comment makes every rule of the line can't tell."""
    (tmp_path / "dev").mkdir()
    environ = {"AWS_PROFILE": "prod"}
    both = {"AWS_PROFILE": "prod", "AWS_DEFAULT_PROFILE": "dev"}
    for command, env in (
        ("true # ; export AWS_PROFILE=dev\naws s3 rm s3://b/k", environ),
        ("unset X # keep AWS_PROFILE\naws s3 ls", both),
        ("export AWS_PROFILE=prod  # was AWS_PROFILE=dev\naws s3 ls", {}),
        ("aws s3 ls  # add --profile dev", environ),
        ("aws --profile dev s3 ls #", environ),
    ):
        assert _aws(command, env) == UNKNOWN_AWS, command
    assert _tf("true # ; cd dev\nterraform apply", tmp_path) == UNKNOWN_TF


def test_glued_heredoc_marker(tmp_path):
    (tmp_path / "dev").mkdir()
    assert _tf("cat>s.sh<<EOF\ncd dev\nEOF\nterraform apply", tmp_path) == UNKNOWN_TF
    # a here-string is not a here-document
    assert _tf("cat<<<x\ncd dev\nterraform apply", tmp_path) == (
        [("terraform_dir", str(tmp_path / "dev"))],
        [],
    )


def test_or_anywhere_in_the_list_is_unknown(tmp_path):
    """A change in an and-or list that has `||` before it is not followed."""
    (tmp_path / "dev").mkdir()
    for command in (
        "true || cd dev && terraform apply",
        "false || cd dev && terraform apply",
        "a && b || cd dev && terraform apply",
    ):
        assert _tf(command, tmp_path) == UNKNOWN_TF, command
    assert _aws("true || export AWS_PROFILE=dev && aws s3 ls", {"AWS_PROFILE": "p"}) == (
        UNKNOWN_AWS
    )


def test_change_inside_a_group_is_unknown(tmp_path):
    """A change inside { … }, an if/loop body or after a keyword is not followed."""
    (tmp_path / "dev").mkdir()
    for command in (
        "{ cd dev; } & terraform apply",
        "if true; then cd dev; fi & terraform apply",
        "{ cd dev; }; terraform apply",
        "{ cd dev && terraform apply; }",
        "if true; then cd dev; fi; terraform apply",
        "if true; then echo; cd dev; terraform apply; fi",
        "! cd dev; terraform apply",
    ):
        assert _tf(command, tmp_path) == UNKNOWN_TF, command
    assert _aws("{ export AWS_PROFILE=dev; } & aws s3 rm s3://b/k", {"AWS_PROFILE": "p"}) == (
        UNKNOWN_AWS
    )


def test_text_that_may_hide_commands_is_untrusted(tmp_path):
    """A possible comment, a backtick or `$'` in the raw text: every rule can't tell."""
    (tmp_path / "prod").mkdir()
    for command in (
        "echo a\\ #b; cd prod\nterraform apply",
        "echo a\\;#b; cd prod\nterraform apply",
        "echo ${X:-a #}; cd prod\nterraform apply",
        "echo $'a\\'; cd prod\nterraform apply",
        "terraform -chdir=/abs apply #",
    ):
        assert _tf(command, tmp_path) == UNKNOWN_TF, command
    # a comment inside a substitution body: every kind can't tell
    assert _cant_tell(_tf("echo `true #`; cd prod\nterraform apply", tmp_path), "terraform_dir")
    # with a quote the lexer can't balance, the line does not parse at all
    out = targets_for(
        "echo $'a\\' #'; cd prod\nterraform apply", cwd="/x", environ={}, rules=default_rules()
    )
    assert out == (True, [], ["command"])
    environ = {"AWS_PROFILE": "dev"}
    for command in (
        "echo x\\ #; export AWS_PROFILE=prod\naws s3 rm s3://b/k",
        "echo x\\&#; export AWS_PROFILE=prod\naws s3 rm s3://b/k",
        "echo ${X:-a #}; export AWS_PROFILE=prod\naws s3 rm s3://b/k",
        "echo ` true; export AWS_PROFILE=prod ` ; aws s3 ls",
        "aws --profile $'dev' s3 ls",
    ):
        assert _aws(command, environ) == UNKNOWN_AWS, command
    # `#` inside a word is not a comment
    for command in ("echo a#b; aws s3 ls", "echo ${#X}; aws s3 ls"):
        assert _aws(command, environ) == ([("aws_profile", "dev")], []), command


def test_assignment_with_redirect_is_lone(tmp_path):
    """A segment of only assignments and redirects is a lone assignment."""
    (tmp_path / "dev").mkdir()
    for command in (
        "AWS_PROFILE=prod 2>/dev/null; aws s3 ls",
        "AWS_PROFILE=prod > /dev/null; aws s3 ls",
    ):
        assert _aws(command, {"AWS_PROFILE": "dev"}) == ([("aws_profile", "prod")], []), command
    assert _tf("pushd dev > /dev/null && terraform apply", tmp_path) == (
        [("terraform_dir", str(tmp_path / "dev"))],
        [],
    )


def test_blur_is_by_event_not_value(tmp_path):
    """A non-plain change blurs what was changed, even when it returns to the start."""
    prod, dev = tmp_path / "prod", tmp_path / "dev"
    prod.mkdir()
    dev.mkdir()
    for command in (
        f"cd {prod}; false && cd {dev}; terraform apply",
        f"cd {prod}; cd {dev} | cat; terraform apply",
        f"cd {prod}; cd {dev} & terraform apply",
        f"cd {prod}; if false; then cd {dev}; fi; terraform apply",
        f"cd {prod}; false && {{ cd {dev}; }}; terraform apply",
        f"cd {prod}; if true; then :; else cd {dev}; fi; terraform apply",
        f"cd {prod}\nfalse && cd {dev}\nterraform apply",
        f"cd {prod} && false && cd {dev}; terraform apply",
        f"cd {prod}; {{ cd {dev}; }} | cat; terraform apply",
        f"cd {prod}; cd {dev} 2>/dev/null & terraform apply",
        f"cd {prod}; false && cd {dev} || true; terraform apply",
    ):
        assert _tf(command, dev) == UNKNOWN_TF, command
    environ = {"AWS_DEFAULT_PROFILE": "dev"}
    for command in (
        "set -a; false && set +a; AWS_PROFILE=prod; aws s3 rm s3://b/k",
        "set -a; set +a | cat; AWS_PROFILE=prod; aws s3 rm s3://b/k",
        # a later exact value does not make a blurred variable readable again
        "false && export AWS_PROFILE=x; export AWS_PROFILE=dev; aws s3 ls",
    ):
        assert _aws(command, environ) == UNKNOWN_AWS, command


def test_escaped_backslash_before_newline_is_not_a_continuation(tmp_path):
    (tmp_path / "prod").mkdir()
    for command in (
        "echo a\\\\\ncd prod\nterraform apply",
        "true \\\\\ncd prod && terraform apply",
    ):
        assert _tf(command, tmp_path) == ([("terraform_dir", str(tmp_path / "prod"))], [])
    assert _aws("echo a\\\\\nexport AWS_PROFILE=prod\naws s3 ls", {"AWS_PROFILE": "dev"}) == (
        [("aws_profile", "prod")],
        [],
    )
    # a real continuation still joins the lines
    assert _tf("cd \\\nprod && terraform apply", tmp_path) == (
        [("terraform_dir", str(tmp_path / "prod"))],
        [],
    )


def test_quoted_redirect_lookalikes_make_the_assignment_unknown():
    """Assignments + redirects count as lone only without quotes or backslashes."""
    environ = {"AWS_PROFILE": "prod"}
    for command in (
        "AWS_PROFILE=dev '>' x; aws s3 rm s3://b/k",
        "AWS_PROFILE=dev '>x'; aws s3 rm s3://b/k",
        "AWS_PROFILE=dev \\>x; aws s3 rm s3://b/k",
    ):
        assert _aws(command, environ) == UNKNOWN_AWS, command
    assert _aws("AWS_PROFILE=dev >/dev/null; aws s3 ls", environ) == ([("aws_profile", "dev")], [])


def test_words_that_hide_commands_are_untrusted(tmp_path):
    """`${…}` with separators, a computed or globbed command name, a group after a
    wrapper: every later rule can't tell."""
    (tmp_path / "dev").mkdir()
    for command in (
        "echo ${X:-a; cd dev; true }\nterraform apply",
        "echo ${X:-${Y} ; cd dev}\nterraform apply",
        "cd${IFS}dev; terraform apply",
        "c? dev; terraform apply",
        "time { cd dev; }; terraform apply",
        "command { cd dev; }; terraform apply",
        "true; echo >#x; cd dev\nterraform apply",
    ):
        assert _cant_tell(_tf(command, tmp_path), "terraform_dir"), command
    environ = {"AWS_PROFILE": "dev"}
    for command in (
        "echo ${X:-a; export AWS_PROFILE=prod }\naws s3 rm s3://b/k",
        "time { export AWS_PROFILE=prod; }; aws s3 rm s3://b/k",
    ):
        assert _cant_tell(_aws(command, environ), "aws_profile"), command
    # plain expansions and the `[` test command stay readable
    for command in ("echo ${HOME}/x; aws s3 ls", "[ -n x ] && aws s3 ls", "[[ -n x ]]; aws s3 ls"):
        assert _aws(command, environ) == ([("aws_profile", "dev")], []), command


# --- brace expansion, zsh forms, computed words --------------------------------

ADP = {"AWS_DEFAULT_PROFILE": "dev"}


def test_brace_expansion_command_is_untrusted(tmp_path):
    """A command word with `{…,…}` or `{…..…}` may expand to any command."""
    (tmp_path / "prod").mkdir()
    for command in (
        "{cd,prod}; terraform apply",
        "{a..c}; terraform apply",
        "command {cd,prod}; terraform apply",
    ):
        assert _cant_tell(_tf(command, tmp_path), "terraform_dir"), command
    out = _aws("{export,AWS_PROFILE=prod}; aws s3 ls", {"AWS_PROFILE": "dev"})
    assert _cant_tell(out, "aws_profile")
    # line-uncertain: flags do not count either
    assert _cant_tell(_aws("{cd,prod}; aws --profile dev s3 ls", {}), "aws_profile")
    # braces in the arguments of an ordinary command change nothing
    assert _aws("echo {a,b}; aws s3 ls", {"AWS_PROFILE": "dev"}) == ([("aws_profile", "dev")], [])


def test_zsh_chdir_and_precommand_modifiers(tmp_path):
    """`chdir` is zsh's `cd` (bash has none, so it can't be told where it goes);
    `noglob`, `nocorrect` and `-` are wrappers."""
    (tmp_path / "prod").mkdir()
    for command in (
        "chdir prod; terraform apply",
        "chdir $D; terraform apply",
        "noglob cd prod; terraform apply",
        "nocorrect cd prod; terraform apply",
        "- cd prod; terraform apply",
        "noglob chdir prod; terraform apply",
    ):
        assert _tf(command, tmp_path) == UNKNOWN_TF, command
    out = targets_for(
        "noglob kubectl --context prod get po", cwd="/x", environ={}, rules=default_rules()
    )
    assert out == (True, [("kube_context", "prod")], [])


def test_zsh_repeat_and_foreach_are_loops(tmp_path):
    (tmp_path / "prod").mkdir()
    for command in (
        "repeat 1 cd prod; terraform apply",
        "foreach x (a); cd prod; end; terraform apply",
    ):
        assert _tf(command, tmp_path) == UNKNOWN_TF, command
    # the command after `repeat N` is still seen
    out = targets_for(
        "repeat 2 kubectl --context dev get po", cwd="/x", environ={}, rules=default_rules()
    )
    assert out == (True, [("kube_context", "dev")], [])


def test_zsh_option_commands_are_untrusted(tmp_path):
    """`setopt`/`unsetopt`/`emulate` (and bash `shopt`, `enable`, zsh `disable`) can
    change how every later word is read."""
    (tmp_path / "prod").mkdir()
    for command in (
        "setopt allexport; AWS_PROFILE=prod; aws s3 ls",
        "unsetopt noallexport; AWS_PROFILE=prod; aws s3 ls",
        "setopt autocd; aws --profile dev s3 ls",
        "shopt -s expand_aliases; aws --profile dev s3 ls",
    ):
        assert _aws(command, ADP) == UNKNOWN_AWS, command
    for command in (
        "emulate sh -c 'cd prod'; terraform apply",
        "enable -n cd; cd prod; terraform apply",
        "disable cd; cd prod; terraform apply",
    ):
        assert _tf(command, tmp_path) == UNKNOWN_TF, command


def test_zsh_autocd_makes_the_folder_unknown(tmp_path):
    """A lone word naming a folder changes into it (zsh `AUTO_CD`)."""
    (tmp_path / "prod").mkdir()
    (tmp_path / "ls").mkdir()  # on PATH: runs the command
    here = ([("terraform_dir", str(tmp_path))], [])
    for command in (
        "prod; terraform apply",
        "prod >/dev/null && terraform apply",
        "../x && terraform apply",
        f"{tmp_path}/prod; terraform apply",
        ".; terraform apply",
        "..; terraform apply",
        "~; terraform apply",
        "~/infra; terraform apply",
    ):
        assert _tf(command, tmp_path) == UNKNOWN_TF, command
    for command in (
        "ls; terraform apply",
        "prod x; terraform apply",  # arguments: no AUTO_CD
        "missing; terraform apply",
    ):
        assert _tf(command, tmp_path, {"PATH": "/bin:/usr/bin"}) == here, command
    # with CDPATH set, any lone word that is not a command may be a folder elsewhere
    assert _tf("missing; terraform apply", tmp_path, {"CDPATH": "/e"}) == UNKNOWN_TF


def test_computed_words_in_state_commands_make_env_unknown():
    """`$`, a backtick, a glob or braces in a state command's words (other than an
    assignment's value): the environment can't be read for the rest of the line."""
    dev = {"AWS_PROFILE": "dev"}
    for command, environ in (
        ("V=AWS_PROFILE=prod; export $V; aws s3 ls", dev),
        ("O=a; set -$O; AWS_PROFILE=prod; aws s3 ls", ADP),
        ("X=-x; declare $X AWS_PROFILE=prod; aws s3 ls", ADP),
        ("V=AWS_PROFILE; unset $V; aws s3 ls", {**dev, "AWS_DEFAULT_PROFILE": "prod"}),
        ("export {AWS_PROFILE,X}=prod; aws s3 ls", dev),
        ("export AWS_PROFILE={x,prod}; aws s3 ls", dev),
        ("export AWS_PROF*; aws s3 ls", dev),
        ("typeset `echo A`=1; aws s3 ls", dev),
        ("local $V; aws s3 ls", dev),
        ("readonly A?=1; aws s3 ls", dev),
    ):
        assert _aws(command, environ) == UNKNOWN_AWS, command
    # a flag still counts
    assert _aws("export $V; aws --profile dev s3 ls", {}) == ([("aws_profile", "dev")], [])
    # `$` in an assignment's value only makes that variable unknown (no splitting, no glob)
    assert _aws("export PATH=$PATH:/x; aws s3 ls", dev) == ([("aws_profile", "dev")], [])


def test_assigning_expansion_makes_the_variable_unknown():
    """`${NAME=…}`, `${NAME:=…}` (zsh `${NAME::=…}`) assign NAME while expanding."""
    for command in (
        ": ${AWS_PROFILE:=prod}; export AWS_PROFILE; aws s3 ls",
        'echo "${AWS_PROFILE=prod}"; aws s3 ls',
        "echo '${AWS_PROFILE::=prod}'; aws s3 ls",
        "aws s3 ls ${AWS_PROFILE:=prod}",
        ": ${!REF=prod}; aws s3 ls",
        ": ${(P)REF=prod}; aws s3 ls",
    ):
        assert _aws(command, ADP) == UNKNOWN_AWS, command
    assert _aws("echo ${AWS_PROFILE:-x}; aws s3 ls", ADP) == ([("aws_profile", "dev")], [])


def test_trap_is_untrusted(tmp_path):
    """A trap runs its text later (`DEBUG` before each command, `ERR` on failure)."""
    (tmp_path / "prod").mkdir()
    for command in (
        "trap 'cd prod' DEBUG; terraform apply",
        "trap 'cd prod' ERR; false; terraform apply",
    ):
        assert _tf(command, tmp_path) == UNKNOWN_TF, command
    assert _aws("trap '' INT; aws --profile dev s3 ls", {}) == UNKNOWN_AWS


def test_append_assignment_is_unknown():
    """`NAME+=…` appends to a value the guard may not know."""
    for command in (
        "export AWS_PROFILE+=prod; aws s3 ls",
        "declare -x AWS_PROFILE+=prod; aws s3 ls",
        "AWS_PROFILE+=prod aws s3 ls",
        "env AWS_PROFILE+=prod aws s3 ls",
        "AWS_PROFILE+=prod; export AWS_PROFILE; aws s3 ls",
    ):
        assert _aws(command, ADP) == UNKNOWN_AWS, command
    assert _aws("AWS_PROFILE+=x; aws s3 ls", {"AWS_PROFILE": "dev"}) == UNKNOWN_AWS


def test_cd_search_path_and_home_make_the_folder_unknown(tmp_path):
    """A `CDPATH`/`HOME` prefix on `cd`, zsh's `cdpath` (a `HOME` set earlier on the line is
    followed: see `test_cd_home_uses_the_followed_home`)."""
    (tmp_path / "dev").mkdir()
    (tmp_path / "other" / "dev").mkdir(parents=True)
    for command in (
        f"CDPATH={tmp_path}/other cd dev && terraform apply",
        f"HOME={tmp_path}/other cd && terraform apply",
        f"cdpath=({tmp_path}/other); cd dev; terraform apply",
        f"cdpath={tmp_path}/other; cd dev; terraform apply",
    ):
        assert _tf(command, tmp_path, {"HOME": "/h"}) == UNKNOWN_TF, command


def test_local_makes_its_names_unknown():
    """Bash `local` outside a function fails; zsh `local` sets the variable."""
    for command in ("local AWS_PROFILE=dev; aws s3 ls", "local AWS_PROFILE; aws s3 ls"):
        assert _aws(command, {"AWS_PROFILE": "prod"}) == UNKNOWN_AWS, command


def test_declare_flags_that_change_the_value_are_unknown():
    """Only `-x` and `+x` are followed; any other flag (`-r`, `-g`, `-n`, `-a`,
    `-i`, `-u`, zsh `-T`, ...) makes the named variables unknown."""
    environ = {"AWS_PROFILE": "prod", "AWS_DEFAULT_PROFILE": "dev"}
    for command in (
        "declare -n AWS_PROFILE; aws s3 ls",
        "typeset -u AWS_PROFILE=dev; aws s3 ls",
        "declare -a AWS_PROFILE=dev; aws s3 ls",
        "declare -ix AWS_PROFILE=dev; aws s3 ls",
        "typeset -T AWS_PROFILE aws_profile; aws s3 ls",
        "export -n AWS_PROFILE; aws s3 ls",
        "declare -r AWS_PROFILE=dev; aws s3 ls",
        "typeset -g AWS_PROFILE=dev; aws s3 ls",
    ):
        assert _aws(command, environ) == UNKNOWN_AWS, command
    # `+x NAME=value` keeps the value as a shell variable; a later `export` exports it
    assert _aws("declare +x AWS_PROFILE=prod; export AWS_PROFILE; aws s3 ls", {}) == (
        [("aws_profile", "prod")],
        [],
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root can enter any folder")
def test_cd_into_a_folder_it_cannot_enter_is_unknown(tmp_path):
    """`cd` fails on a folder without search permission."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        assert _tf("cd locked; terraform apply", tmp_path) == UNKNOWN_TF
    finally:
        locked.chmod(0o755)


def test_set_flags_that_change_how_words_are_read_are_untrusted():
    """bash `set -k` puts later `NAME=value` arguments in the command's environment; other
    unlisted letters or options are not followed. zsh spellings of allexport are not followed
    either (bash rejects them): a plain `A=b` after them is unknown."""
    for command in (
        "set -k; aws s3 ls AWS_PROFILE=prod",
        "set -o keyword; aws s3 ls AWS_PROFILE=prod",
        "set -J; aws --profile dev s3 ls",
    ):
        assert _aws(command, ADP) == UNKNOWN_AWS, command
    for command in (
        "set -o all_export; AWS_PROFILE=prod; aws s3 ls",
        "set +o NO_ALL_EXPORT; AWS_PROFILE=prod; aws s3 ls",
    ):
        assert _aws(command, {}) == UNKNOWN_AWS, command
    for command in ("set -euo pipefail; aws s3 ls", "set -e -x; aws s3 ls", "set +e; aws s3 ls"):
        assert _aws(command, ADP) == ([("aws_profile", "dev")], []), command


# --- splitting declarations, set -o spellings, read-only names, test tails ------


def test_declaration_not_written_bare_splits_dollar_values():
    """Bash 3.2 splits `$X` in an assignment argument unless the declaration command
    is a bare word, with no prefix assignment and no `builtin`/wrapper, and the argument
    starts with a plain `NAME=`. `unset` and `set` always split."""
    for command in (
        "\\export A=$X; aws s3 ls",
        '"export" A=$X; aws s3 ls',
        'ex""port A=$X; aws s3 ls',
        "'declare' -x A=$X; aws s3 ls",
        "builtin export A=$X; aws s3 ls",
        "builtin declare -x A=$X; aws s3 ls",
        "command export A=$X; aws s3 ls",
        "B=1 export A=$X; aws s3 ls",
        'export "A"=$X; aws s3 ls',
        "export A\\=$X; aws s3 ls",
        "unset A=$X; aws s3 ls",
    ):
        assert _aws(command, ADP) == UNKNOWN_AWS, command
    # `set $O` may be `set -k`: flags no longer count either
    assert _aws("set $O; aws --profile dev s3 ls", ADP) == UNKNOWN_AWS
    # a bare declaration does not split its assignment values: only A is unknown
    for command in (
        "export A=$X; aws s3 ls",
        "export B=1 A=$X; aws s3 ls",
        'export A="$X"; aws s3 ls',
        "declare -x A=$X; aws s3 ls",
        "if true; then export A=$X; fi; aws --profile dev s3 ls",
    ):
        assert _aws(command, ADP) == ([("aws_profile", "dev")], []), command
    assert _aws("builtin export AWS_PROFILE=dev; aws s3 ls", {}) == ([("aws_profile", "dev")], [])


def test_set_follows_only_the_exact_allexport_spellings():
    """Bash rejects zsh spellings (`noallexport`, `all_export`, `ALLEXPORT`) and zsh
    takes them, so after any `-o`/`+o` name but `allexport` or a no-op one (`pipefail`, ...)
    a plain `A=b` may or may not be exported."""
    for command in (
        "set -a; set -o noallexport; AWS_PROFILE=prod; aws s3 ls",
        "set -a; set +o all_export; AWS_PROFILE=prod; aws s3 ls",
        "set -a; set +o ALLEXPORT; AWS_PROFILE=prod; aws s3 ls",
        "set -o all_export; AWS_PROFILE=prod; aws s3 ls",
        "set +o NO_ALL_EXPORT; AWS_PROFILE=prod; aws s3 ls",
    ):
        assert _aws(command, ADP) == UNKNOWN_AWS, command
    assert _aws("set -o allexport; AWS_PROFILE=dev; aws s3 ls", {}) == (
        [("aws_profile", "dev")],
        [],
    )
    assert _aws("set -a; set +o allexport; AWS_PROFILE=prod; aws s3 ls", ADP) == (
        [("aws_profile", "dev")],
        [],
    )
    for command in ("set -euo pipefail; aws s3 ls", "set +a; AWS_PROFILE=prod; aws s3 ls"):
        assert _aws(command, ADP) == ([("aws_profile", "dev")], []), command
    assert _aws("set -euo pipefail; export AWS_PROFILE=dev; aws s3 ls", {"AWS_PROFILE": "p"}) == (
        [("aws_profile", "dev")],
        [],
    )


def test_readonly_names_are_unknown_for_the_rest_of_the_line():
    """A later write to a read-only variable fails (bash and zsh), and a prefix
    assignment fails while the command still runs with the old value."""
    environ = {"AWS_PROFILE": "prod", "AWS_DEFAULT_PROFILE": "dev"}
    for command in (
        "readonly AWS_PROFILE; export AWS_PROFILE=dev; aws s3 ls",
        "declare -r AWS_PROFILE; export AWS_PROFILE=dev; aws s3 ls",
        "typeset -r AWS_PROFILE; AWS_PROFILE=dev; aws s3 ls",
        "readonly AWS_PROFILE; AWS_PROFILE=dev aws s3 ls",
        "readonly AWS_PROFILE; unset AWS_PROFILE; aws s3 ls",
        "readonly AWS_PROFILE=dev; aws s3 ls",
        "declare -rx AWS_PROFILE=dev; aws s3 ls",
    ):
        assert _aws(command, environ) == UNKNOWN_AWS, command
    assert _aws("readonly X; aws s3 ls", {"AWS_PROFILE": "prod"}) == ([("aws_profile", "prod")], [])


def test_declare_global_and_readonly_export_are_unknown():
    """Bash 3.2 rejects `declare -g` and `readonly -x` (no change); zsh takes them."""
    for command in (
        "declare -gx AWS_PROFILE=dev; aws s3 ls",
        "typeset -gx AWS_PROFILE=dev; aws s3 ls",
        "declare -g AWS_PROFILE=dev; aws s3 ls",
        "readonly -x AWS_PROFILE=dev; aws s3 ls",
    ):
        assert _aws(command, {"AWS_PROFILE": "prod"}) == UNKNOWN_AWS, command


def test_zsh_short_forms_after_a_test_are_untrusted():
    """Zsh runs `cmd` in `if [[ x ]] cmd`, `while [[ x ]] { cmd }`, `if (( x )) cmd`;
    words straight after a `)` are not read as a command of their own."""
    for command in (
        "while [[ 1 ]] { aws --profile dev s3 ls; break }",
        "if [[ 1 ]] aws --profile dev s3 ls",
        "if [[ ( 1 ) ]] aws --profile dev s3 ls",
        "if (( 1 )) aws --profile dev s3 ls",
        "if [ 1 ] { aws --profile dev s3 ls }",
        "if test 1 { aws --profile dev s3 ls }",
        "X=$(date) aws --profile dev s3 ls",
    ):
        assert _aws(command, ADP) == UNKNOWN_AWS, command
    for command in (
        "[[ -n x ]] && aws --profile dev s3 ls",
        "[ -f x ] && aws --profile dev s3 ls",
        "test -n x; aws --profile dev s3 ls",
        "(cd /tmp) >/dev/null; aws --profile dev s3 ls",
        "echo $(date); aws --profile dev s3 ls",
    ):
        assert _aws(command, ADP) == ([("aws_profile", "dev")], []), command


def test_computed_words_in_a_matched_command_are_unknown():
    """A word that starts with `$`, a backtick or a brace expansion, or holds an
    unquoted `$` or brace expansion, may become a flag: the rule can't tell, flags
    included."""
    for command in (
        "aws {--profile,dev} s3 ls",
        "aws --profile dev s3 ls --{profile,x}=dev",
        "aws --profile dev s3 ls $X",
        'aws --profile dev s3 ls "$X"',
        "aws --profile dev s3 ls ''\"$X\"",
        'aws --profile dev s3 ls "--profile="$X',
        "aws --profile dev s3 ls a$X",
        "aws --profile dev s3 cp $(ls) s3://b/",
        'aws --profile dev s3 cp "$(ls)" s3://b/',
        "aws --profile dev s3 cp ${X} s3://b/",
    ):
        assert _aws(command, ADP) == UNKNOWN_AWS, command
    for command in (
        "aws --profile dev s3 ls 's3://$b'",
        'aws --profile dev s3 ls "s3://$B/k"',
        "aws --profile dev s3 ls \\$X",
        "echo $X; aws --profile dev s3 ls",
        "aws --profile dev s3 ls {} x,y",
    ):
        assert _aws(command, ADP) == ([("aws_profile", "dev")], []), command


# --- [[switch]] ---------------------------------------------------------------


def _sw(command, environ=None):
    out = targets_for(
        command,
        cwd="/x",
        environ=environ or {},
        rules=default_rules(),
        switches=tuple(default_switches()),
        run=_ops,
    )
    return out[1], out[2]


def test_kubectx_then_kubectl_is_unknown():
    assert _sw("kubectx prod && kubectl get po") == UNKNOWN_KUBE
    assert _sw("kubectx prod; kubectl get po") == UNKNOWN_KUBE


def test_use_context_then_kubectl_is_unknown():
    for sub in ("use-context", "use", "set-context", "set", "rename-context"):
        # the switch itself reads the current context; the later kubectl can't tell
        assert _sw(f"kubectl config {sub} prod && kubectl get po") == (
            [("kube_context", "ops")],
            ["kube_context"],
        )
    assert _sw("kubectl --context ops config use-context prod; helm ls") == (
        [("kube_context", "ops")],
        ["kube_context"],
    )


def test_non_switch_subcommand_is_not_a_switch():
    assert _sw("kubectl config view && kubectl --context ops get ns") == (
        [("kube_context", "ops")],
        [],
    )
    assert _sw("kubectl config view && kubectl get ns")[1] == []


def test_switch_taints_across_pipe():
    assert _sw("kubectx prod | kubectl get po") == UNKNOWN_KUBE
    assert _sw("kubectx prod & kubectl get po") == UNKNOWN_KUBE
    assert _sw("kubectx prod || kubectl get po") == UNKNOWN_KUBE
    assert _sw("(kubectx prod); kubectl get po") == UNKNOWN_KUBE
    assert _sw("(kubectx prod; kubectl get po)") == UNKNOWN_KUBE


def test_switch_keeps_flag_value():
    assert _sw("kubectx prod && kubectl --context ops get ns") == (
        [("kube_context", "ops")],
        [],
    )


def test_switch_only_affects_later_segments_and_its_kinds():
    assert _sw("kubectl get po && kubectx prod")[1] == []
    targets, unknown = _sw("kubectx prod && aws --profile p s3 ls")
    assert ("aws_profile", "p") in targets and unknown == []


def test_git_remote_set_url_then_push_is_unknown():
    assert _sw("git remote set-url origin x && git push") == ([], ["git_remote_host"])
    assert _sw("git config remote.origin.url x; git push") == ([], ["git_remote_host"])
    assert _sw("git -C d remote add o x; git push") == ([], ["git_remote_host"])


def test_aws_configure_then_aws_is_unknown():
    assert _sw("aws configure set region x && aws s3 ls") == ([], ["aws_profile"])
    out = _sw("aws configure && aws --profile p s3 ls", {"AWS_PROFILE": "q"})
    assert out == ([("aws_profile", "q"), ("aws_profile", "p")], [])


def test_switch_matches_wrapped_and_path_programs():
    assert _sw("command /usr/bin/kubectx prod && kubectl get po") == UNKNOWN_KUBE
    assert _sw("env A=b kubectx prod && kubectl get po") == UNKNOWN_KUBE


def test_switch_computed_args_ask():
    assert _sw("kubectl config $S prod && kubectl get po") == UNKNOWN_KUBE


# --- loop variables, physical folders, hidden programs -------------------------


def test_loop_variable_is_unknown_for_the_rest_of_the_line():
    """`for NAME in …`, `select NAME`, zsh `foreach NAME (…)` and `for NAME (…)` assign NAME
    (exported when it already was)."""
    rm = "aws s3 rm s3://b/k"
    for command in (
        f"for AWS_PROFILE in prod; do {rm}; done",
        f"for AWS_PROFILE in prod; do :; done; {rm}",
        f"export AWS_PROFILE=dev; for AWS_PROFILE in prod; do :; done; {rm}",
        f"select AWS_PROFILE in prod; do {rm}; break; done",
        f"foreach AWS_PROFILE (prod) {rm}; end",
        f"for AWS_PROFILE (prod) {rm}",
        f"for X AWS_PROFILE in a prod; do :; done; {rm}",
        f"if true; then for AWS_PROFILE in prod; do :; done; fi; {rm}",
    ):
        assert _aws(command, {"AWS_PROFILE": "dev"}) == UNKNOWN_AWS, command
    out = targets_for(
        "for KUBECONFIG in ~/.kube/prod; do :; done; kubectl delete ns x",
        cwd="/x",
        environ={"KUBECONFIG": "/k/dev"},
        rules=default_rules(),
        run=_ops,
    )
    assert out == (True, [], ["kube_context"])
    # another loop variable leaves the rule alone
    for command in (
        "for x in a b; do aws s3 ls; done",
        "for x in a b; do :; done; aws s3 ls",
        "select x in a; do aws s3 ls; break; done",
    ):
        assert _aws(command, {"AWS_PROFILE": "dev"}) == ([("aws_profile", "dev")], []), command


SWITCHES = tuple(default_switches())


def _remote(argv, **kw):
    if argv[:1] == ["git"] and argv[-2:] == ["remote", "-v"]:
        out = "origin\tgit@github.com:a/b.git (fetch)\norigin\tgit@github.com:a/b.git (push)\n"
        return subprocess.CompletedProcess(argv, 0, out, "")
    return _ops(argv, **kw)


def _sw_git(command):
    out = targets_for(
        command,
        cwd="/x",
        environ={},
        rules=default_rules(),
        switches=tuple(default_switches()),
        run=_remote,
    )
    return out[1], out[2]


def test_switch_args_are_the_leading_words():
    """`args` are the first non-option words after the program (global options and their
    values skipped); the same words later on, or another subcommand, are no switch."""
    pushed = ([("git_remote_host", "github.com")], [])
    for command in (
        "git commit -m \"$(cat <<'EOF'\nfix: x\n\nbody\nEOF\n)\" && git push",
        'git commit -m "$MSG" && git push',
        "git add config && git commit -m x && git push",
        "git remote -v && git push",
        "git remote && git push",
        'git commit -m "remote add" && git push',
        "git log --grep remote add && git push",
        "git -C . status && git push",
    ):
        assert _sw_git(command) == pushed, command
    for command in (
        "git remote set-url origin x && git push",
        "git remote add o x && git push",
        "git remote rename o p; git push",
        "git remote remove o; git push",
        "git remote rm o; git push",
        "git remote -v add o x; git push",
        "git -c a.b=c remote add o x; git push",
        "git --git-dir=.git remote set-url origin x; git push",
        "git --namespace n remote add o x; git push",
        "git $S set-url origin x && git push",
        "git config remote.origin.url x && git push",
        "git config --get remote.origin.url && git push",
    ):
        assert _sw_git(command) == ([], ["git_remote_host"]), command
    for command in (
        "kubectl -n x config use-context prod && kubectl get po",
        "kubectl --context=x config use prod && kubectl get po",
        "kubectl config unset current-context; kubectl get po",
    ):
        assert _sw(command)[1] == ["kube_context"], command
    assert _sw("kubectl get cm config && kubectl get po") == ([("kube_context", "ops")], [])


def _tree(tmp_path):
    """prod/sub, dev, and dev/link -> prod/sub."""
    (tmp_path / "prod" / "sub").mkdir(parents=True)
    (tmp_path / "dev").mkdir()
    os.symlink(tmp_path / "prod" / "sub", tmp_path / "dev" / "link")
    return os.path.realpath(tmp_path / "prod")


def test_physical_folders_resolve_dot_dot_after_a_symlink(tmp_path):
    """`cd -P`, `terraform -chdir` and `git -C` go where the kernel goes: `..` after a symlink
    is the link target's parent. A plain `cd` stays logical, as in bash and zsh."""
    prod = _tree(tmp_path)
    for command in (
        "cd -P dev/link/.. && terraform apply",
        "cd -P dev/link && cd .. && terraform apply",
        "cd -LP dev/link/..; terraform apply",
        "terraform -chdir=dev/link/.. apply",
        "cd dev/link && terraform -chdir=.. apply",
    ):
        assert _tf(command, tmp_path) == ([("terraform_dir", prod)], []), command
    dev = str(tmp_path / "dev")
    assert _tf("cd dev/link/.. && terraform apply", tmp_path) == ([("terraform_dir", dev)], [])
    # bash: the last of -L/-P wins; zsh: -P wins
    assert _tf("cd -PL dev/link/..; terraform apply", tmp_path) == UNKNOWN_TF
    # an explicit -L is logical, like a plain cd
    assert _tf("cd -L dev/link/.. && terraform apply", tmp_path) == ([("terraform_dir", dev)], [])
    # the same folder keeps the spelling it was given
    assert _tf("terraform -chdir=dev/../dev apply", tmp_path) == ([("terraform_dir", dev)], [])


def test_git_dash_c_dot_dot_after_a_symlink(tmp_path):
    prod = _tree(tmp_path)
    seen = []

    def run(argv, **kw):
        seen.append(argv[2])
        return subprocess.CompletedProcess(argv, 0, "", "")

    targets_for(
        "git -C dev/link/.. push", cwd=str(tmp_path), environ={}, rules=default_rules(), run=run
    )
    assert seen == [prod]


def test_prefix_assignment_on_a_declaration_is_unknown_afterwards():
    """bash: `A=x export A` exports x (the prefix persists); zsh does not. Either way the
    names are unknown for the rest of the line."""
    dev = {"AWS_PROFILE": "dev"}
    for command in (
        "AWS_PROFILE=prod export AWS_PROFILE; aws s3 ls",
        "AWS_PROFILE=prod declare -x AWS_PROFILE; aws s3 ls",
        "AWS_PROFILE=prod typeset -x AWS_PROFILE; aws s3 ls",
        "AWS_PROFILE=prod export X=1; aws s3 ls",
        "AWS_PROFILE=prod readonly X; aws s3 ls",
        "AWS_PROFILE=prod local X; aws s3 ls",
        "AWS_PROFILE=prod unset X; aws s3 ls",
        "AWS_PROFILE=prod set -e; aws s3 ls",
    ):
        assert _aws(command, dev) == UNKNOWN_AWS, command
    assert _aws("AWS_PROFILE=prod true; aws s3 ls", dev) == ([("aws_profile", "dev")], [])


def test_computed_command_name_matches_every_rule():
    every = sorted({r.kind for r in default_rules()})
    for command in (
        "$K delete ns x",
        "${K} --context prod delete ns x",
        "{kubectl,} --context prod delete ns x",
        "env $K delete ns x",
        "/usr/bin/kube* --context prod delete ns x",
    ):
        matched, targets, unknown = targets_for(
            command, cwd="/x", environ={}, rules=default_rules(), run=_ops
        )
        assert matched and targets == [] and sorted(unknown) == every, command


def test_zsh_equals_command_word_is_the_program():
    assert _aws("=aws s3 rm s3://b/k --profile prod", {}) == ([("aws_profile", "prod")], [])
    assert _sw("=kubectx prod && kubectl get po") == UNKNOWN_KUBE


def test_leading_redirects_do_not_hide_the_command():
    for command in (
        ">/dev/null aws --profile prod s3 rm s3://b/k",
        "> /dev/null aws --profile prod s3 rm s3://b/k",
        "2>&1 aws --profile prod s3 rm s3://b/k",
        "A=1 2>/dev/null aws --profile prod s3 rm s3://b/k",
        "<in >out aws --profile prod s3 rm s3://b/k",
        "env >x aws --profile prod s3 rm s3://b/k",
    ):
        assert _aws(command, {}) == ([("aws_profile", "prod")], []), command


def test_clobber_redirect_is_not_a_pipe():
    for command in (
        "aws s3 ls >| out --profile prod",
        "aws s3 ls >|out --profile prod",
    ):
        assert _aws(command, {}) == ([("aws_profile", "prod")], []), command


def test_more_wrappers_expose_the_program():
    for command in (
        "nice -n 5 aws --profile prod s3 ls",
        "nice aws --profile prod s3 ls",
        "nice --adjustment 5 aws --profile prod s3 ls",
        "stdbuf -o L aws --profile prod s3 ls",
        "stdbuf -oL -e 0 aws --profile prod s3 ls",
        "caffeinate -i -t 60 aws --profile prod s3 ls",
        "coproc aws --profile prod s3 ls",
        "gtimeout 5 aws --profile prod s3 ls",
        "timeout --foreground 5 aws --profile prod s3 ls",
        "time -o out aws --profile prod s3 ls",
        "doas -u root aws --profile prod s3 ls",
        "watch -n 2 aws --profile prod s3 ls",
        "nohup nice -n 1 aws --profile prod s3 ls",
        "arch -arm64 aws --profile prod s3 ls",
        "arch -arch x86_64 aws --profile prod s3 ls",
        "setsid aws --profile prod s3 ls",
        "setsid -f aws --profile prod s3 ls",
    ):
        assert _aws(command, {}) == ([("aws_profile", "prod")], []), command


def test_env_split_string_and_path_hide_the_program():
    every = sorted({r.kind for r in default_rules()})
    for command in (
        "env -S 'kubectl --context prod delete ns x'",
        "env --split-string='kubectl --context prod delete ns x'",
        "env -P /opt/bin kubectl --context prod delete ns x",
        "env -iS 'kubectl --context prod delete ns x'",
    ):
        out = targets_for(command, cwd="/x", environ={}, rules=default_rules(), run=_ops)
        assert out[1] == [] and sorted(out[2]) == every, command


def test_exec_c_clears_the_environment():
    assert _aws("exec -c aws s3 ls", {"AWS_PROFILE": "dev"}) == UNKNOWN_AWS


def test_chdir_wrappers_make_the_folder_unknown(tmp_path):
    (tmp_path / "prod").mkdir()
    for command in (
        "env -C prod terraform apply",
        "env --chdir=prod terraform apply",
        "env -iC prod terraform apply",
        "sudo -D prod terraform apply",
        "sudo --chdir=prod terraform apply",
        "sudo -R / terraform apply",
    ):
        assert _tf(command, tmp_path) == UNKNOWN_TF, command
    assert _tf("env -u X terraform apply", tmp_path) == ([("terraform_dir", str(tmp_path))], [])


def test_shell_dash_c_with_a_guarded_command_cannot_tell():
    every = sorted({r.kind for r in default_rules()})
    for command in (
        "bash -c 'kubectl --context prod delete ns x'",
        "sh -c 'aws s3 rm s3://b/k'",
        "zsh -lc 'git push'",
        "/bin/bash -ec 'cd /x; terraform apply'",
        "sudo bash -c 'kubectx prod'",
        'bash -c "$CMD"',
        "echo 'kubectl --context prod delete ns x' | bash",
        "bash -s < x <<< 'aws s3 ls'",
    ):
        out = targets_for(
            command, cwd="/x", environ={}, rules=default_rules(), switches=SWITCHES, run=_ops
        )
        assert out[1] == [] and sorted(out[2]) == every, command
    for command in ("bash -c 'echo hi'; aws --profile p s3 ls", "bash x.sh; aws --profile p s3 ls"):
        assert _aws(command, {}) == ([("aws_profile", "p")], []), command


def test_git_environment_and_repo_cannot_tell():
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_CONFIG",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_PARAMETERS",
    ):
        environ = {name: "x"}
        out = targets_for("git push", cwd="/x", environ=environ, rules=default_rules(), run=_remote)
        assert out == (True, [], ["git_remote_host"]), name
        for command in (f"{name}=x git push", f"export {name}=x; git push"):
            out = targets_for(command, cwd="/x", environ={}, rules=default_rules(), run=_remote)
            assert out == (True, [], ["git_remote_host"]), command
    for command in (
        "git push --repo=prod",
        "git push --repo prod",
        "git -c remote.origin.pushurl=git@prod:x push",
        "git -c url.git@prod:.insteadOf=git@github.com: push",
        "git -c branch.main.pushRemote=prod push",
        "git -c include.path=/x push",
        "git -c $K=v push",
        "git --config-env=remote.origin.url=X push",
    ):
        out = targets_for(command, cwd="/x", environ={}, rules=default_rules(), run=_remote)
        assert out == (True, [], ["git_remote_host"]), command
    out = targets_for("git push", cwd="/x", environ={}, rules=default_rules(), run=_remote)
    assert out == (True, [("git_remote_host", "github.com")], [])


def test_zsh_brace_bodies_in_a_segment_are_untrusted(tmp_path):
    """zsh `if { list } { list }`, `while { list } { list }`: a `{` word after the command
    starts a body the guard does not follow."""
    (tmp_path / "prod").mkdir()
    rm = "aws s3 rm s3://b/k"
    for command in (
        f"if {{ true }} {{ export AWS_PROFILE=prod }}; {rm}",
        "while { true } { aws s3 rm s3://b/k --profile dev; break }",
        "while { true } { aws s3 rm s3://b/k --profile dev; break; }",
        f"if true {{ export AWS_PROFILE=prod }}; {rm}",
        "coproc N { aws s3 rm s3://b/k --profile dev; }",
    ):
        assert _aws(command, ADP) == UNKNOWN_AWS, command
    assert _tf("if { true } { cd prod }; terraform apply", tmp_path) == UNKNOWN_TF
    # `{}` and quoted braces are plain words
    for command in ("echo {} '{'; aws --profile dev s3 ls", "aws --profile dev s3 ls {}"):
        assert _aws(command, ADP) == ([("aws_profile", "dev")], []), command


def test_guarded_command_in_a_hidden_substitution_cannot_tell():
    every = sorted({r.kind for r in default_rules()})
    for command in (
        'echo "$(kubectl --context prod delete ns x)"',
        'X="$(kubectl --context prod delete ns x)"',
        "echo `kubectl --context prod delete ns x`",
        'echo "`aws s3 rm s3://b/k`"',
        'echo "$(echo "$(git push)")"',
        'echo "$(kubectx prod)"',
        'echo "$(sudo terraform apply -auto-approve)"',
    ):
        out = targets_for(
            command, cwd="/x", environ={}, rules=default_rules(), switches=SWITCHES, run=_remote
        )
        assert out[1] == [] and sorted(out[2]) == every, command
    # a body that runs no guarded command, or a git command other than push
    for command in (
        'git log --oneline "$(git merge-base HEAD main)"..HEAD && git push',
        "git commit -m \"$(cat <<'EOF'\nfix: x\n\nbody\nEOF\n)\" && git push",
        'git tag "v$(date +%s)" && git push',
    ):
        out = targets_for(
            command,
            cwd="/x",
            environ={},
            rules=default_rules(),
            switches=tuple(default_switches()),
            run=_remote,
        )
        assert out == (True, [("git_remote_host", "github.com")], []), command


def test_builtins_that_assign_make_their_names_unknown():
    """`printf -v NAME`, zsh `print -v NAME`, `read NAME…`, `getopts … NAME` assign NAME."""
    for command in (
        "printf -v AWS_PROFILE prod; aws s3 ls",
        "printf -vAWS_PROFILE %s prod; aws s3 ls",
        "print -v AWS_PROFILE prod; aws s3 ls",
        "read AWS_PROFILE < f; aws s3 ls",
        "read -r X AWS_PROFILE < f; aws s3 ls",
        "getopts ab AWS_PROFILE; aws s3 ls",
    ):
        assert _aws(command, {"AWS_PROFILE": "dev"}) == UNKNOWN_AWS, command
    # `while read x` over a list keeps other names readable
    out = _aws("ls | while read x; do aws s3 ls; done", {"AWS_PROFILE": "dev"})
    assert out == ([("aws_profile", "dev")], [])


# --- minors (2026-11-02) --------------------------------------------------------

# `set` options and letters that change neither how variables are assigned or exported nor
# how words are read, accepted by bash 3.2 and zsh (zsh rejects errtrace, functrace, history,
# monitor, `-b` and `-m`, and a non-interactive zsh stops at the failing `set`).
_NOOP_OPTIONS = (
    "pipefail", "errexit", "nounset", "xtrace", "verbose", "noclobber", "errtrace",
    "functrace", "notify", "monitor", "ignoreeof", "emacs", "vi", "history", "hashall",
)  # fmt: skip
_NOOP_LETTERS = "euxvCEbmh"


def test_set_noop_options_keep_allexport_known():
    dev = ([("aws_profile", "dev")], [])
    for option in _NOOP_OPTIONS:
        for sign in "-+":
            command = f"set {sign}o {option}; AWS_PROFILE=prod; aws s3 ls"
            assert _aws(command, ADP) == dev, command
    for letter in _NOOP_LETTERS:
        for sign in "-+":
            command = f"set {sign}{letter}; AWS_PROFILE=prod; aws s3 ls"
            assert _aws(command, ADP) == dev, command
    assert _aws("set -euo pipefail; AWS_PROFILE=prod; aws s3 ls", ADP) == dev
    assert _aws("set -euo pipefail; AWS_PROFILE=dev; aws s3 ls", {"AWS_PROFILE": "p"}) == dev
    # zsh spells pipefail PIPE_FAIL; bash rejects it and changes nothing
    assert _aws("set -o PIPE_FAIL; AWS_PROFILE=prod; aws s3 ls", ADP) == dev
    # allexport stays on through a no-op option
    assert _aws("set -a; set -o pipefail; AWS_PROFILE=dev; aws s3 ls", {}) == dev
    assert _aws("set -a; set -euo pipefail; AWS_PROFILE=dev; aws s3 ls", {}) == dev


def test_set_options_that_may_change_reading_stay_unknown():
    """`physical`/`-P` (bash: cd is physical; zsh `-P`: RC_EXPAND_PARAM) and `-T` (zsh:
    CDABLE_VARS) are not no-ops; nor is a name only one shell accepts in a `set` that also
    changes allexport (bash stops at the bad name, zsh does not)."""
    for command in (
        "set -o physical; AWS_PROFILE=prod; aws s3 ls",
        "set -P; AWS_PROFILE=prod; aws s3 ls",
        "set -T; AWS_PROFILE=prod; aws s3 ls",
        "set -o noexec; AWS_PROFILE=prod; aws s3 ls",
        "set -o nopipefail; AWS_PROFILE=prod; aws s3 ls",
        "set -o PIPE_FAIL -a; AWS_PROFILE=prod; aws s3 ls",
        "set -o errtrace -a; AWS_PROFILE=prod; aws s3 ls",
        "set -ba; AWS_PROFILE=prod; aws s3 ls",
        "set -a; set -o history +a; AWS_PROFILE=prod; aws s3 ls",
    ):
        assert _aws(command, ADP) == UNKNOWN_AWS, command
    # allexport stays off whichever way the shell reads it; both shells accept every word
    assert _aws("set -o history +a; AWS_PROFILE=prod; aws s3 ls", ADP) == (
        [("aws_profile", "dev")],
        [],
    )
    assert _aws("set -o pipefail -a; AWS_PROFILE=dev; aws s3 ls", {}) == (
        [("aws_profile", "dev")],
        [],
    )


def test_redirect_inside_wrapper_options_does_not_hide_the_program():
    for command in (
        "timeout >/dev/null 5 kubectl --context prod get po",
        "timeout 2>&1 5 kubectl --context prod get po",
        "timeout -s KILL >/dev/null 5 kubectl --context prod get po",
        "nice >/dev/null -n 5 kubectl --context prod get po",
        "nice -n 5 > /dev/null kubectl --context prod get po",
        "sudo -u x >/dev/null kubectl --context prod get po",
        "sudo >/dev/null -u x kubectl --context prod get po",
        "env >/dev/null -u X kubectl --context prod get po",
    ):
        out = targets_for(command, cwd="/x", environ={}, rules=default_rules(), run=_ops)
        assert out == (True, [("kube_context", "prod")], []), command


def _line(command, environ=None, cwd="/x"):
    """(targets, unknown kinds) of a line with the shipped rules and switches."""
    out = targets_for(
        command,
        cwd=cwd,
        environ=environ or {},
        rules=default_rules(),
        switches=tuple(default_switches()),
        run=_remote,
        home="/home/x",
    )
    return out[1], out[2]


_EVERY = (
    [],
    [
        "kube_context", "aws_profile", "terraform_dir", "git_remote_host",
        "ansible_inventory", "cloudflare_account",
    ],
)  # fmt: skip
_GH = ([("git_remote_host", "github.com")], [])


def test_text_runners_naming_a_guarded_program_match_every_rule():
    """`eval`, `trap`, `alias`, zsh `emulate … -c` run their text later: when it names a
    guarded program or is computed, every kind can't tell, at the top level and inside a
    substitution body."""
    for command in (
        'eval "kubectl --context prod delete ns x"',
        "eval kubectl --context prod delete ns x",
        'eval "$CMD"',
        "trap 'kubectl --context prod delete ns x' EXIT",
        "alias k='kubectl --context prod'",
        "emulate sh -c 'kubectl --context prod delete ns x'",
        'X="$(eval "kubectl --context prod delete ns x")"',
        'X="$(eval "$CMD")"',
        "X=`eval kubectl --context prod delete ns x`",
        'X="$(command eval "$CMD")"',
    ):
        assert _line(command) == _EVERY, command
    # an `eval` of plain text inside a body runs nothing guarded
    assert _line('X="$(eval echo hi)"; aws --profile dev s3 ls') == ([("aws_profile", "dev")], [])


def test_heredoc_in_substitution_whose_end_the_shells_disagree_on():
    """bash 3.2 ends `$(` at the first unbalanced `)`, even in a here-document body (the
    rest is text in double quotes, commands outside them); zsh reads the body as data."""
    for command in (
        "X=\"$(cat <<'EOF'\n1) one\nEOF\n)\"; aws --profile dev s3 ls",
        'X="$(cat <<EOF\na) b\nEOF\n)"; aws --profile dev s3 ls',
        "X=$(cat <<EOF\na) b\nEOF\n); aws --profile dev s3 ls",
        'X="$(cat <<EOF\nno end\n)"; aws --profile dev s3 ls',
        'X="$(case $Y in a) kubectl --context prod delete ns x;; esac)"; aws s3 ls',
    ):
        out = _line(command, {"AWS_PROFILE": "dev"})
        assert out == _EVERY, command
    for command in (
        (
            "git commit -m \"$(cat <<'EOF'\nfeat(guard): it's fine (#12)\n\nbody (a) b\nEOF\n)\" "
            "&& git push"
        ),
        "git commit -m \"$(cat <<'EOF'\nfix: x\n\nbody\nEOF\n)\" && git push",
        "git commit -m \"$(cat <<-'EOF'\n\tfix: x (y)\n\tEOF\n)\" && git push",
        "git commit -m \"$(cat <<'EOF'\nfix: the edge case\nEOF\n)\" && git push",
    ):
        assert _line(command) == _GH, command


def test_shells_with_long_command_options_are_read_like_dash_c():
    for command in (
        'fish --command "kubectl --context prod delete ns x"',
        'fish --command="kubectl --context prod delete ns x"',
        'fish -c "kubectl --context prod delete ns x"',
        "fish -C 'kubectl --context prod delete ns x' script.fish",
        "fish --init-command='kubectl get po' script.fish",
        'bash --rcfile x -c "kubectl --context prod delete ns x"',
        "echo kubectl delete ns x | bash --rcfile x",
        "echo kubectl delete ns x | bash --init-file x",
        "echo kubectl delete ns x | fish",
        'nu -c "kubectl --context prod delete ns x"',
        'nu --commands "kubectl --context prod delete ns x"',
        'pwsh -Command "kubectl --context prod delete ns x"',
        'pwsh -c "kubectl --context prod delete ns x"',
        "pwsh -EncodedCommand AAAA; kubectl --context dev get po",
        "pwsh -ec AAAA; kubectl --context dev get po",
        'xonsh -c "kubectl --context prod delete ns x"',
    ):
        assert _line(command) == _EVERY, command
    for command in (
        "bash -c 'echo hi'; kubectl --context dev get po",
        "fish -c 'echo hi'; kubectl --context dev get po",
        "bash --rcfile x script.sh; kubectl --context dev get po",
    ):
        assert _line(command) == ([("kube_context", "dev")], []), command


def test_hash_inside_quotes_is_no_comment():
    for command in (
        'git commit -m "fix #12" && git push',
        'git commit -m "(#12)" && git push',
        "git commit -m 'fix #12' && git push",
        'git commit -m "feat: x (#12)" && git push',
        "git commit -m fix#12 && git push",
    ):
        assert _line(command) == _GH, command
    assert _line('git commit -m "$(echo #x)" && git push') == _EVERY
    # an unquoted comment, or a quote structure the guard does not read exactly, still asks
    for command in (
        "git push # done",
        'git commit -m "a" #"x" && git push',
        'git commit -m "a\\" #x" && git push',
        "git commit -m \"$(cat <<'EOF'\nx #1\nEOF\n)\" #c\ngit push",
    ):
        assert _line(command) == ([], ["git_remote_host"]), command


def test_cd_home_uses_the_followed_home(tmp_path):
    """`cd` alone and `cd ~/…` go to the `HOME` set earlier on the line (bash and zsh read the
    shell variable); an unknown or unset `HOME` can't tell."""
    other = tmp_path / "other"
    (other / "dev").mkdir(parents=True)
    for environ in ({"HOME": "/h"}, {}):
        for command, folder in (
            (f"HOME={other}; cd; terraform apply", other),
            (f"export HOME={other}; cd ~/dev; terraform apply", other / "dev"),
            (f"export HOME={other} && cd ~ && terraform apply", other),
            (f"declare -x HOME={other}\ncd\nterraform apply", other),
        ):
            assert _tf(command, tmp_path, environ) == ([("terraform_dir", str(folder))], []), (
                command
            )
    for command in (
        "HOME=$X; cd; terraform apply",
        "export HOME=$X; cd ~/dev; terraform apply",
        "unset HOME; cd; terraform apply",
        "read HOME; cd; terraform apply",
        f"true || HOME={other}; cd; terraform apply",
        f"HOME={other}; cd ~x; terraform apply",
    ):
        assert _tf(command, tmp_path, {"HOME": "/h"}) == UNKNOWN_TF, command
    # other kinds may read files under the home: still can't tell
    out = targets_for(
        f"HOME={other}; kubectl get po",
        cwd=str(tmp_path),
        environ={"HOME": "/h"},
        rules=default_rules(),
        run=_ops,
    )
    assert out == (True, [], ["kube_context"])


def _recording(calls, context=lambda kw: "ops"):
    def run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, context(kw) + "\n", "")

    return run


def test_default_from_runs_in_the_followed_folder_and_environment(tmp_path):
    (tmp_path / "sub").mkdir()
    environ = {"FOO": "hook", "GONE": "x"}
    for command, cwd, env in (
        ("kubectl get po", tmp_path, environ),
        ("cd sub && kubectl get po", tmp_path / "sub", environ),
        ("export BAR=1; kubectl get po", tmp_path, {**environ, "BAR": "1"}),
        ("FOO=new; kubectl get po", tmp_path, {**environ, "FOO": "new"}),
        ("unset GONE && kubectl get po", tmp_path, {"FOO": "hook"}),
        ("BAR=2 kubectl get po", tmp_path, {**environ, "BAR": "2"}),
        ("env -u GONE kubectl get po", tmp_path, {"FOO": "hook"}),
        ("set -a; BAR=3; kubectl get po", tmp_path, {**environ, "BAR": "3"}),
        ("BAR=4; kubectl get po", tmp_path, environ),  # not exported
    ):
        calls = []
        out = targets_for(
            command,
            cwd=str(tmp_path),
            environ=environ,
            rules=default_rules(),
            run=_recording(calls),
        )
        assert out == (True, [("kube_context", "ops")], []), command
        assert [c[0] for c in calls] == [["kubectl", "config", "current-context"]], command
        assert calls[0][1]["cwd"] == str(cwd), command
        assert calls[0][1]["env"] == env, command


def test_default_from_with_an_exported_unknown_variable_cant_tell(tmp_path):
    environ = {"FOO": "hook"}
    for command in (
        "export BAR=$(date); kubectl get po",
        "read FOO; kubectl get po",
        "for FOO in a b; do :; done; kubectl get po",
        "set -a; read BAR; kubectl get po",
        "true || export BAR=1; kubectl get po",
    ):
        calls = []
        out = targets_for(
            command,
            cwd=str(tmp_path),
            environ=environ,
            rules=default_rules(),
            run=_recording(calls),
        )
        assert out == (True, [], ["kube_context"]), command
    # a variable that is not exported is not passed on: the default still counts
    for command in ("read X; kubectl get po", "for f in a b; do kubectl get po; done"):
        out = targets_for(
            command, cwd=str(tmp_path), environ=environ, rules=default_rules(), run=_recording([])
        )
        assert out == (True, [("kube_context", "ops")], []), command


def test_fallback_cache_is_per_folder_and_environment(tmp_path):
    (tmp_path / "a").mkdir()
    calls = []
    out = targets_for(
        "kubectl get po; cd a; kubectl get po; kubectl get no; export X=1; kubectl get po",
        cwd=str(tmp_path),
        environ={},
        rules=default_rules(),
        run=_recording(calls, lambda kw: os.path.basename(kw["cwd"]) + kw["env"].get("X", "")),
    )
    assert out == (
        True,
        [
            ("kube_context", os.path.basename(tmp_path)),
            ("kube_context", "a"),
            ("kube_context", "a1"),
        ],
        [],
    )
    assert len(calls) == 3


def test_mapfile_and_readarray_make_their_names_unknown():
    for command in (
        "mapfile AWS_PROFILE < f; aws s3 ls",
        "readarray -t AWS_PROFILE < f; aws s3 ls",
        "mapfile -n 1 -d , AWS_PROFILE < f; aws s3 ls",
        "mapfile -tu 3 AWS_PROFILE; aws s3 ls",
    ):
        assert _aws(command, {"AWS_PROFILE": "dev"}) == UNKNOWN_AWS, command
    # no name: bash fills MAPFILE
    assert _aws("mapfile < f; aws s3 ls", {"AWS_PROFILE": "dev"}) == ([("aws_profile", "dev")], [])
    # `-C` evaluates its callback like `eval`
    for command in (
        "mapfile -C 'kubectl --context prod delete ns' -c 1 a < f",
        'readarray -C "$CB" a < f; aws s3 ls',
        "mapfile -n 1 -C 'kubectl get po' a < f",
    ):
        assert _line(command, {"AWS_PROFILE": "dev"}) == _EVERY, command


# --- review round 1 (2026-11-02) ----------------------------------------------------


def test_fake_or_partly_quoted_heredoc_markers_do_not_hide_a_comment():
    """`<<'X'` inside quotes is no here-document; `<<'E'OF` ends at `EOF`; a here-document
    without its end line strips nothing. The `#` then counts as a comment: every rule the line
    matches can't tell."""
    for command in (
        "echo \"<<'X'\"\nkubectl delete pod p # --context dev",
        "echo '<<\"X\"'\nkubectl delete pod p # --context dev",
        "cat <<'E'OF\ndata\nEOF\nkubectl delete pod p # --context dev",
        "cat <<'EOF'\nx\nkubectl delete pod p # --context dev",
        "cat <<\"E\"'O'F\ndata\nEOF\nkubectl delete pod p # --context dev",
    ):
        assert _line(command) == ([], ["kube_context"]), command
    # a real quoted here-document body is still data
    assert _line("cat <<'E'OF\n# x\nEOF\nkubectl --context dev get po") == (
        [("kube_context", "dev")],
        [],
    )
    assert _line("git commit -m \"$(cat <<'EOF'\nfix: x (#12)\nEOF\n)\" && git push") == _GH


def test_fake_or_partly_quoted_heredoc_markers_in_a_substitution_body():
    for command in (
        'X="$(echo "<<\'Q\'"\nkubectl --context prod delete ns x)"; echo',
        "X=\"$(cat <<'Q'Z\nhi\nQZ\nkubectl --context prod delete ns x)\"; echo",
        "X=\"$(cat <<'Q'\nhi\nkubectl --context prod delete ns x)\"; echo",
    ):
        assert _line(command) == _EVERY, command


def test_redirect_between_wrapper_option_and_value():
    for command in (
        "nice -n >/dev/null 5 kubectl --context prod get po",
        "sudo -u >/dev/null root kubectl --context prod get po",
        "timeout -s >/dev/null KILL 5 kubectl --context prod get po",
        "env -u >/dev/null A kubectl --context prod get po",
        "env --unset >/dev/null A kubectl --context prod get po",
        "nice -n 2>&1 5 kubectl --context prod get po",
    ):
        assert _line(command) == ([("kube_context", "prod")], []), command


def test_xargs_program_is_matched_but_cant_tell():
    """xargs appends words from stdin, a flag among them: the program's rules can't tell,
    flags included."""
    for command in (
        "echo x | xargs kubectl --context prod delete ns",
        "echo x | xargs -n1 kubectl --context dev delete ns",
        "echo x | xargs -0 -P 4 -L 1 kubectl --context dev get po",
        "echo x | xargs >/dev/null -n1 kubectl --context dev delete ns",
        "echo x | xargs -d , -E end -s 100 kubectl get po",
    ):
        assert _line(command) == ([], ["kube_context"]), command
    # a replace string: every kind can't tell (ruling 2026-11-02)
    assert _line("echo x | xargs -I {} kubectl --context dev delete ns {}") == _EVERY
    # xargs of another program matches nothing
    assert _line("ls | xargs grep x; kubectl --context dev get po") == (
        [("kube_context", "dev")],
        [],
    )
    # stdin may finish a switch: a later kubectl can't tell
    out = _line("echo prod | xargs kubectl config use-context; kubectl --context dev get po")
    assert ("kube_context", "dev") in out[0] and out[1] == ["kube_context"]
    assert _line("echo dev | xargs kubectl config; kubectl get po")[1] == ["kube_context"]


def test_parallel_and_find_exec_naming_a_guarded_program_match_every_rule():
    for command in (
        "parallel kubectl --context prod delete ns ::: a b",
        "parallel 'kubectl delete ns {}' ::: a b",
        "ls | parallel -j 2 kubectl delete ns",
        r"find . -name '*.yaml' -exec kubectl --context prod apply -f {} \;",
        "find . -execdir kubectl apply -f {} +",
        r"find . -ok kubectl delete -f {} \;",
        r'find . -exec sh -c "$X" \;',
    ):
        assert _line(command) == _EVERY, command
    # grep never runs its arguments: a `{}` for it is safe (allow-list)
    assert _line("find . -name x -exec grep y {} +; kubectl --context dev get po") == (
        [("kube_context", "dev")],
        [],
    )
    assert _line("find . -name x -exec rm x \\; ; kubectl --context dev get po") == (
        [("kube_context", "dev")],
        [],
    )


def test_cd_through_a_missing_folder_fails(tmp_path):
    """bash and zsh check each folder before `..`: `cd dev/x/..` fails when `dev/x` is
    missing, and the command runs where it was."""
    (tmp_path / "dev").mkdir()
    for command in ("cd dev/x/..; terraform apply", "HOME=$PWD; cd ~/dev/x/..; terraform apply"):
        assert _tf(command, tmp_path) == UNKNOWN_TF, command
    (tmp_path / "dev" / "x").mkdir()
    assert _tf("cd dev/x/..; terraform apply", tmp_path) == (
        [("terraform_dir", str(tmp_path / "dev"))],
        [],
    )


# --- review round 2 (2026-11-02) ----------------------------------------------------

_C = "kubectl delete pod p # --context dev"
_P = "kubectl --context prod delete ns x"


def test_here_string_is_no_here_document():
    """`<<<` is a here-string: no body follows, so a later comment is a comment."""
    for command in (f"cat <<<'X'\n{_C}\nX", f'cat <<<"X"\n{_C}\nX', f"cat <<< 'X'\n{_C}\nX"):
        assert _line(command) == ([], ["kube_context"]), command
    for command in (
        f"X=\"$(cat <<<'Q'\n{_P}\nQ\n)\"; echo",
        f"X=\"$(# <<'Q'\n{_P}\nQ\n)\"; echo",
        'X="$(echo a # x\n)"; echo kubectl',
    ):
        assert _line(command) == _EVERY, command


def test_arithmetic_shift_is_no_here_document():
    # the comment scan does not skip arithmetic (ruling 2026-11-02: an accepted over-ask)
    gh = 'echo $((1<<2)) && git commit -m "fix #12" && git push'
    assert _line(gh) == ([], ["git_remote_host"])
    # a `$( … )` in double quotes keeps the raw `#` rule
    assert _line('X="$(echo $((1<<2)))"; git commit -m "fix #12" && git push')[0] == []


def test_commands_from_stdin_under_xargs_parallel_and_find():
    for command in (
        f"echo '{_P}' | xargs -0 sh -c",
        f"echo '{_P}' | xargs -I{{}} sh -c '{{}}'",
        f"echo '{_P}' | xargs bash",
        f"echo '{_P}' | parallel",
        f"echo '{_P}' | parallel -j 2",
        f"echo '{_P}' | parallel --jobs 2 -k",
        r"find /usr/local/bin -name kubectl -exec {} --context prod delete ns x \;",
        r"find . -exec $CMD \;",
        r"find . -name '*.sh' -exec sh -c '{}' \;",
        r"find . -exec bash {} \;",
    ):
        assert _line(command) == _EVERY, command
    # explicit, readable command words still allow; a bare xargs runs echo
    for command in (
        "echo x | xargs; kubectl --context dev get po",
        "ls | parallel gzip ::: a b; kubectl --context dev get po",
    ):
        assert _line(command) == ([("kube_context", "dev")], []), command


def test_xargs_long_options_it_does_not_know_hide_the_program():
    for command in (
        f"echo n | xargs --max-a 1 {_P}",
        f"echo n | xargs --delim , {_P}",
        "echo n | xargs --bogus kubectl get po",
        "echo n | xargs --max-a=1 kubectl get po",  # abbreviated, also with `=`
    ):
        assert _line(command) == _EVERY, command
    # exact long options are read
    for command in (
        "echo n | xargs --max-args 1 kubectl get po",
        "echo n | xargs --max-args=1 kubectl get po",
        "echo n | xargs --null --no-run-if-empty kubectl get po",
    ):
        assert _line(command) == ([], ["kube_context"]), command


# --- review round 3 (2026-11-02) ----------------------------------------------------


def test_subshell_substitution_is_no_arithmetic():
    """`$((cmd …) )` is a substitution of a subshell, and `'$(('` is text: a `#` in them is a
    comment as usual."""
    for command in (
        f"echo '$((' ; {_C} '))'",
        f"echo '$(('\n{_C} '))'",
        f'X=$(({_C}\n) ); echo "[$X]"',
        f"echo $(({_C}\n) )",
    ):
        assert _line(command)[0] == [] and "kube_context" in _line(command)[1], command
    # a real arithmetic shift is no here-document
    assert _line("echo $((1<<2)) && kubectl --context dev get po") == (
        [("kube_context", "dev")],
        [],
    )


_B = "/usr/local/bin"


def test_runners_through_wrappers_and_placeholders():
    """Under xargs, parallel and every find action, wrappers are unwrapped first: nothing
    left, a placeholder, a shell or a `$` as the program asks for every kind."""
    for command in (
        "echo kubectl delete ns x | xargs env",
        "echo kubectl delete ns x | xargs nice",
        "echo kubectl delete ns x | xargs command",
        "echo kubectl delete ns x | xargs sudo",
        "echo kubectl delete ns x | xargs timeout 5",
        f"echo {_B}/kubectl | xargs -I{{}} {{}} --context prod delete ns x",
        f"echo {_B}/kubectl | xargs -I% nice % --context prod delete ns x",
        f"echo {_B}/kubectl | xargs -J% nice % --context prod delete ns x",
        f"echo {_B}/kubectl | xargs -i nice {{}} --context prod delete ns x",
        f"echo {_B}/kubectl | xargs --replace=@ nice @ --context prod delete ns x",
        rf"find {_B} -name kubectl -exec nice {{}} --context prod delete ns x \;",
        rf"find {_B} -name kubectl -exec env {{}} --context prod delete ns x \;",
        rf"find {_B} -name kubectl -exec timeout 5 {{}} --context prod delete ns x \;",
        rf"find {_B} -name kubectl -ok nice {{}} --context prod delete ns x \;",
        rf"find {_B} -name kubectl -exec true \; -exec nice {{}} --context prod delete ns x \;",
        r"find . -exec env sh -c 'x' \;",
        r"find . -exec nice \;",
        f"ls {_B}/kube* | parallel nice {{}} --context prod delete ns x",
        f"ls {_B}/kube* | parallel env {{}} --context prod delete ns x",
        "echo kubectl delete ns x | parallel nice",
        "echo x | parallel -- sh -c {}",
        "echo kubectl | parallel -X nice {}",
        "echo kubectl | parallel nice {1} get po",
    ):
        assert _line(command) == _EVERY, command
    for command in (
        r"find . -exec true \; -exec nice grep y \; ; kubectl --context dev get po",
        "ls | xargs grep foo; kubectl --context dev get po",
        "ls | xargs nice grep foo; kubectl --context dev get po",
        "ls | parallel nice gzip; kubectl --context dev get po",
    ):
        assert _line(command) == ([("kube_context", "dev")], []), command


# --- review round 4 (2026-11-02) ----------------------------------------------------


def test_placeholder_anywhere_under_a_runner_asks():
    """A placeholder in any command word of a find action, xargs or parallel asks for every
    kind, behind any wrapper, listed or not (accepted over-ask: `-exec kubectl apply -f {}`)."""
    for command in (
        f"find {_B} -name kubectl -exec ionice2 {{}} --context prod delete ns x \\;",
        f"find {_B} -name kubectl -exec mywrap -x {{}} --context prod delete ns x \\;",
        "find . -exec kubectl apply -f {} \\;",
        f"echo {_B}/kubectl | xargs -I% mywrap % --context prod delete ns x",
        f"echo {_B}/kubectl | xargs -I% mywrap -n 5 x% --context prod delete ns x",
        f"ls {_B}/kube* | parallel mywrap {{}} --context prod delete ns x",
        f"ls {_B}/kube* | parallel mywrap {{.}} --context prod delete ns x",
    ):
        assert _line(command) == _EVERY, command


def test_scheduling_wrappers_expose_the_program():
    """util-linux `ionice [-c C] [-n N] [-t] cmd`, `chrt [policy] PRIO cmd`, `taskset [-c]
    MASK cmd`, and `cpulimit -l N cmd`."""
    for command in (
        "ionice -c 2 -n 7 kubectl --context prod get po",
        "ionice -c2 -t kubectl --context prod get po",
        "ionice --class 3 kubectl --context prod get po",
        "chrt 10 kubectl --context prod get po",
        "chrt -f 10 kubectl --context prod get po",
        "chrt --rr -R 5 kubectl --context prod get po",
        "chrt -d -T 1000 -P 2000 -D 2000 0 kubectl --context prod get po",
        "taskset 0x3 kubectl --context prod get po",
        "taskset -c 0,2 kubectl --context prod get po",
        "taskset --cpu-list 1-3 kubectl --context prod get po",
        "cpulimit -l 50 kubectl --context prod get po",
        "cpulimit --limit=50 -- kubectl --context prod get po",
        "cpulimit -l 50 -s SIGTERM -k -- kubectl --context prod get po",
    ):
        assert _line(command) == ([("kube_context", "prod")], []), command


# --- placeholder allow-list (ruling 2026-11-02) --------------------------------------


def test_placeholder_for_a_program_that_never_runs_its_arguments():
    """After the listed wrappers, a placeholder asks unless the program never runs its
    arguments (echo, cp, rm, grep, ...); a guarded program still asks (the value may be a
    flag), and so does any program that can run code (awk, sed, git, ...)."""
    dev = ([("kube_context", "dev")], [])
    for command in (
        "kubectl get pods -o name | xargs -I{} echo {}",
        "kubectl get pods -o name | xargs -I{} rm {}",
        "ls | xargs -I{} cp {} /tmp; kubectl --context dev get po",
        "ls | xargs -I% nice cp % /tmp; kubectl --context dev get po",
        "find . -name x -exec grep y {} +; kubectl --context dev get po",
        "find . -exec chmod 600 {} \\; ; kubectl --context dev get po",
        "ls | parallel gzip {} ; kubectl --context dev get po",
    ):
        out = _line(command)
        assert out[1] == [] and out[0] in (dev[0], [("kube_context", "ops")]), command
    for command in (
        f"find {_B} -name kubectl -exec ionice {{}} --context prod delete ns x \\;",
        f"find {_B} -name kubectl -exec ionice2 {{}} --context prod delete ns x \\;",
        "echo p | xargs -I{} kubectl delete pod {}",
        "echo p | xargs -I{} awk {}",
        "echo p | xargs -I{} sed -e {} f; kubectl get po",
        "echo p | xargs -I{} git {}; kubectl get po",
        "find . -exec kubectl apply -f {} \\;",
        "ls | parallel tar xf {} ; kubectl get po",
    ):
        assert _line(command) == _EVERY, command


# --- final review of the minors (2026-11-02) ------------------------------------------


def test_placeholder_in_a_wrapper_option_is_no_data():
    """BSD `xargs -J` puts every stdin word where the placeholder is: inside a wrapper's
    options it can supply the program, so the allow-list does not apply there."""
    for command in (
        "echo 5 kubectl --context prod delete ns x | xargs -J% nice -n % cat",
        "echo X kubectl --context prod delete ns x | xargs -J% env -u % cat",
        "echo r kubectl --context prod delete ns x | xargs -J% sudo -u % cat",
        "echo 'x; kubectl --context prod delete ns x' | parallel echo {=uq=}",
        "echo 1 | parallel nice -n {=uq=} cat; kubectl get po",
    ):
        assert _line(command) == _EVERY, command


def test_xargs_and_parallel_placeholder_spellings():
    for command in (
        f"echo {_B}/kubectl | xargs --repl=% nice % --context prod delete ns x",
        f"echo {_B}/kubectl | xargs --re={{}} {{}} --context prod delete ns x",
        f"echo {_B}/kubectl | xargs -ri nice {{}} --context prod delete ns x",
        f"echo {_B}/kubectl | xargs -0i nice {{}} --context prod delete ns x",
        f"echo {_B}/kubectl | parallel -I XX nice XX --context prod delete ns x",
        f"echo {_B}/kubectl | parallel -IXX XX --context prod delete ns x",
        f"echo {_B}/kubectl | parallel -U XX nice XX --context prod delete ns x",
        f"echo {_B}/kubectl | parallel --replace XX XX --context prod delete ns x",
    ):
        assert _line(command) == _EVERY, command


def test_chrt_priority_is_optional_for_policies_without_one():
    for command in (
        "chrt -o kubectl --context prod get po",
        "chrt -b kubectl --context prod get po",
        "chrt -i 0 kubectl --context prod get po",
    ):
        assert _line(command) == ([("kube_context", "prod")], []), command


def test_quoted_text_in_arithmetic_is_no_arithmetic():
    """bash reads quotes inside `$((…))` (zsh does not): `'))'` there can end a subshell
    substitution instead, so a region with quotes, backslashes or newlines is no arithmetic."""
    command = "echo $((echo '))<<\\Q '\\'\nkubectl delete pod p # --context dev\n) )\nQ"
    out = _line(command)
    assert out[0] == [] and "kube_context" in out[1], command


# --- switch words ------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "words"),
    [
        ("kubectx prod && kubectl get po", [("kube_context", "prod")]),
        ("kubectl config use-context production", [("kube_context", "production")]),
        # an option not known to take a value: its next word is kept too (over-asks only)
        (
            "kubectl --kubeconfig k config use-context live",
            [("kube_context", "k"), ("kube_context", "live")],
        ),
        ("git -C repo remote add up h", [("git_remote_host", "up"), ("git_remote_host", "h")]),
        ("kubectx new=prod", [("kube_context", "new"), ("kube_context", "prod")]),
        ("aws configure --profile acme", [("aws_profile", "acme")]),
        ("kubectx -", []),
        ('kubectx "$C"', []),
        ("kubectx", []),
        ("echo prod | xargs kubectx", []),
    ],
)
def test_switch_words_are_collected(command, words):
    found: list = []
    targets_for(
        command,
        cwd="/x",
        environ={},
        rules=default_rules(),
        switches=tuple(default_switches()),
        run=_no_run,
        switch_words=found,
    )
    assert found == words


def test_blank_flag_value_reads_the_default_and_stays_unresolved():
    """``--context ""`` makes kubectl use its current context: read it, and keep the kind
    "can't tell" so allowlist mode still asks."""

    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, "prod\n", "")

    rules = default_rules()
    for command in ("kubectl --context '' get po", "kubectl --context= get po",
                    "kubectl --context dev --context ' ' get po"):  # fmt: skip
        out = targets_for(command, cwd="/x", environ={}, rules=rules, run=run)
        assert out == (True, [("kube_context", "prod")], ["kube_context"]), command
    environ = {"AWS_PROFILE": "live"}
    out = targets_for("aws --profile '' s3 ls", cwd="/x", environ=environ, rules=rules)
    assert out == (True, [("aws_profile", "live")], ["aws_profile"])
    out = targets_for("aws --profile p --profile= s3 ls", cwd="/x", environ=environ, rules=rules)
    assert out == (True, [("aws_profile", "p"), ("aws_profile", "live")], ["aws_profile"])
    environ = {"ANSIBLE_INVENTORY": "prod"}
    out = targets_for("ansible-playbook -i '' x.yml", cwd="/x", environ=environ, rules=rules)
    assert out == (True, [("ansible_inventory", "prod")], ["ansible_inventory"])
