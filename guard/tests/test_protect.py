"""Protect mode: ask only for protected targets and targets another workspace owns."""

import subprocess
from dataclasses import replace

import pytest

from treant_guard.config import GuardConfig, load_config
from treant_guard.engine import check, format_reason
from treant_guard.rules import default_rules


def _no_run(argv, **kw):
    return subprocess.CompletedProcess(argv, 1, "", "")


def _ctx(name):
    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, f"{name}\n", "")

    return run


def _cfg(tmp_path, allow=None, protect=None, deny=None, mode="protect"):
    return GuardConfig(
        rules=default_rules(),
        workspaces={str(tmp_path): "startup"},
        allow=allow or {},
        deny=deny or {},
        about={},
        protect=protect or {},
        mode=mode,
    )


def _check(tmp_path, command, run=_no_run, environ=None, **cfg):
    return check(
        command,
        cwd=str(tmp_path),
        workspace_dir=str(tmp_path),
        environ=environ or {},
        config=_cfg(tmp_path, **cfg),
        run=run,
        home="/home/x",
    )


PROTECT = {"*": {"kube_context": ["prod", "production"], "aws_profile": ["default", "live"]}}
ALLOW = {
    "startup": {"kube_context": ["startup-ops", "pre-production"]},
    "acme": {"kube_context": ["pre-prod", "pre-production"]},
}


def test_unlisted_target_allows(tmp_path):
    d = _check(tmp_path, "kubectl --context scratch delete ns x", allow=ALLOW, protect=PROTECT)
    assert d.action == "allow"
    assert d.reason is None


def test_protected_target_asks_even_when_allowed(tmp_path):
    allow = {"startup": {"kube_context": ["prod"]}}
    d = _check(tmp_path, "kubectl --context prod get po", allow=allow, protect=PROTECT)
    assert d.action == "ask"
    assert d.protected == [("kube_context", "prod")]
    assert d.reason == "Treant guard (startup): protected: kube_context=prod"


def test_workspace_protect_applies_only_there(tmp_path):
    protect = {"acme": {"kube_context": ["ops"]}}
    d = _check(tmp_path, "kubectl --context ops get po", protect=protect)
    assert d.action == "allow"
    protect = {"startup": {"kube_context": ["ops"]}}
    d = _check(tmp_path, "kubectl --context ops get po", protect=protect)
    assert d.action == "ask"


def test_other_workspace_target_asks(tmp_path):
    d = _check(tmp_path, "kubectl get po", run=_ctx("pre-prod"), allow=ALLOW, protect=PROTECT)
    assert d.action == "ask"
    assert d.foreign == [("kube_context", "pre-prod", ("acme",))]
    assert d.reason == "Treant guard (startup): belongs to acme: kube_context=pre-prod"


def test_target_shared_with_own_workspace_allows(tmp_path):
    d = _check(tmp_path, "helm list", run=_ctx("pre-production"), allow=ALLOW, protect=PROTECT)
    assert d.action == "allow"


def test_cannot_tell_allows(tmp_path):
    for command in (
        'kubectl --context "$C" delete ns x',
        "kubectl 'unterminated",
        "$K get po",
        "kubectl get po",  # no current context
    ):
        d = _check(tmp_path, command, allow=ALLOW, protect=PROTECT)
        assert d.action == "allow", command
        assert d.reason is None, command


def test_deny_still_blocks_and_beats_protect(tmp_path):
    deny = {"*": {"kube_context": ["prod"]}}
    d = _check(tmp_path, "kubectl --context prod get po", protect=PROTECT, deny=deny)
    assert d.action == "deny"
    assert d.protected == []


@pytest.mark.parametrize(
    "command",
    [
        "kubectx prod && kubectl delete ns x",
        "kubectl config use-context production; kubectl delete ns x",
        "kubectx prod",
        "kubectx new=prod",
    ],
)
def test_switch_to_protected_context_asks(tmp_path, command):
    d = _check(tmp_path, command, protect=PROTECT)
    assert d.action == "ask", command
    assert d.reason == f"Treant guard (startup): protected: kube_context={d.protected[0][1]}"


def test_switch_to_other_workspace_context_asks(tmp_path):
    d = _check(tmp_path, "kubectx pre-prod && kubectl get po", allow=ALLOW, protect=PROTECT)
    assert d.action == "ask"
    assert d.foreign == [("kube_context", "pre-prod", ("acme",))]


def test_switch_to_denied_context_denies(tmp_path):
    deny = {"*": {"kube_context": ["prod"]}}
    d = _check(tmp_path, "kubectx prod; kubectl delete ns x", deny=deny)
    assert d.action == "deny"


def test_switch_to_own_or_unlisted_context_allows(tmp_path):
    for command in (
        "kubectx startup-ops && kubectl delete ns x",
        "kubectx scratch && kubectl delete ns x",
        "git remote add up git@github.com:me/x.git && git push up",
    ):
        d = _check(tmp_path, command, allow=ALLOW, protect=PROTECT)
        assert d.action == "allow", command


def test_allowlist_mode_ignores_switch_words(tmp_path):
    allow = {"startup": {"kube_context": ["ops"]}}
    d = _check(tmp_path, "kubectx scratch", allow=allow, mode="allowlist")
    assert d.action == "allow"


def test_allowlist_mode_keeps_old_behaviour_and_honours_protect(tmp_path):
    allow = {"startup": {"kube_context": ["ops", "prod"]}}
    d = _check(tmp_path, "kubectl --context other get po", allow=allow, mode="allowlist")
    assert d.action == "ask"
    assert d.not_allowed == [("kube_context", "other")]
    d = _check(tmp_path, "kubectl get po", allow=allow, mode="allowlist")
    assert d.action == "ask"
    assert d.unknown == ["kube_context"]
    d = _check(tmp_path, "kubectl 'x", allow=allow, mode="allowlist")
    assert d.action == "ask"
    d = _check(
        tmp_path, "kubectl --context prod get po", allow=allow, protect=PROTECT, mode="allowlist"
    )
    assert d.action == "ask"
    assert d.protected == [("kube_context", "prod")]


def test_protected_path_matches_by_prefix_and_real_path(tmp_path):
    live = tmp_path / "live"
    (live / "stack").mkdir(parents=True)
    (tmp_path / "link").symlink_to("live")
    protect = {"*": {"terraform_dir": [str(live)]}}
    d = _check(tmp_path, f"terraform -chdir={tmp_path}/link/stack apply", protect=protect)
    assert d.action == "ask"
    d = _check(tmp_path, f"terraform -chdir={tmp_path}/other apply", protect=protect)
    assert d.action == "allow"


def test_other_workspace_folder_asks(tmp_path):
    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    mine.mkdir()
    theirs.mkdir()
    allow = {
        "startup": {"terraform_dir": [str(mine)]},
        "acme": {"terraform_dir": [str(theirs)]},
    }
    d = _check(tmp_path, f"terraform -chdir={theirs} plan", allow=allow)
    assert d.action == "ask"
    assert d.foreign == [("terraform_dir", str(theirs), ("acme",))]
    assert _check(tmp_path, f"terraform -chdir={mine} plan", allow=allow).action == "allow"


def test_invalid_config_still_asks(tmp_path):
    cfg = GuardConfig(default_rules(), {}, {}, {}, {}, error="/c/guard.toml")
    d = check("kubectl get po", cwd="/a", workspace_dir="/a", environ={}, config=cfg)
    assert d.action == "ask"


def test_loaded_config_defaults_to_protect(tmp_path):
    conf = tmp_path / "conf"
    conf.mkdir(mode=0o700)
    conf.chmod(0o700)
    f = conf / "guard.toml"
    f.write_text(
        f'[workspaces]\n"{tmp_path}" = "startup"\n[protect."*"]\nkube_context = ["prod"]\n'
    )
    f.chmod(0o600)
    cfg = load_config(conf)
    assert cfg.error is None and cfg.mode == "protect"
    kw = {"cwd": str(tmp_path), "workspace_dir": str(tmp_path), "environ": {}, "config": cfg}
    assert check("kubectl --context dev get po", run=_no_run, **kw).action == "allow"
    assert check("kubectl --context prod get po", run=_no_run, **kw).action == "ask"


def test_format_reason_protect_parts():
    assert format_reason(
        "w",
        [],
        [],
        [],
        protected=[("k", "p")],
        foreign=[("k", "a", ("x",)), ("j", "b", ("x", "y")), ("k", "c", ("x",))],
    ) == ("Treant guard (w): protected: k=p; belongs to x: k=a, k=c; belongs to x, y: j=b")


# --- review fixes ------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git remote add up git@git.prod.example:r.git && git push up main",
        "git remote set-url origin https://git.prod.example/r.git; git push origin",
        "git config remote.origin.url git@git.prod.example:r.git && git push origin",
        "git remote add up ssh://me@GIT.PROD.EXAMPLE:22/r.git",
    ],
)
def test_switch_url_words_match_their_host(tmp_path, command):
    d = _check(tmp_path, command, protect={"*": {"git_remote_host": ["git.prod.example"]}})
    assert d.action == "ask", command
    assert d.protected == [("git_remote_host", "git.prod.example")]
    d = _check(tmp_path, command, deny={"*": {"git_remote_host": ["git.prod.example"]}})
    assert d.action == "deny", command


def test_fallback_prefilter_covers_switches(tmp_path):
    cfg = GuardConfig(default_rules(), {}, {}, {}, {}, error="/c/guard.toml")
    d = check("kubectx prod", cwd="/a", workspace_dir="/a", environ={}, config=cfg)
    assert d.action == "ask"


def test_allowlist_mode_checks_switch_words_for_deny_and_protect_only(tmp_path):
    allow = {"startup": {"kube_context": ["ops"]}}
    kw = {"allow": allow, "mode": "allowlist"}
    d = _check(tmp_path, "kubectx prod", protect=PROTECT, **kw)
    assert d.action == "ask"
    assert d.protected == [("kube_context", "prod")]
    d = _check(
        tmp_path, "kubectx evil; kubectl get po", deny={"*": {"kube_context": ["evil"]}}, **kw
    )
    assert d.action == "deny"
    # an unlisted word is no finding: allowlist asks only for what the command then reads
    assert _check(tmp_path, "kubectx scratch", **kw).action == "allow"
    assert _check(tmp_path, "git remote add up git@h:r.git", **kw).action == "allow"


def test_switch_option_with_equals_keeps_its_value(tmp_path):
    from treant_guard.rules import Switch, default_switches

    cfg = _cfg(tmp_path, protect=PROTECT)
    cfg = GuardConfig(
        cfg.rules,
        cfg.workspaces,
        {},
        {},
        {},
        switches=[*default_switches(), Switch(("kw",), ("kube_context",))],
        protect=PROTECT,
    )
    for command in ("kw --ctx prod", "kw --ctx=prod"):
        d = check(command, cwd=str(tmp_path), workspace_dir=str(tmp_path), environ={}, config=cfg)
        assert d.action == "ask", command


def test_blank_flag_value_does_not_hide_the_default(tmp_path):
    """``kubectl --context= ...`` acts on the current context, so a protected or denied one
    still asks or denies."""
    d = _check(tmp_path, "kubectl --context '' delete po x", run=_ctx("prod"), protect=PROTECT)
    assert d.action == "ask"
    assert d.protected == [("kube_context", "prod")]
    d = _check(
        tmp_path, "kubectl --context= delete po x", run=_ctx("prod"),
        deny={"*": {"kube_context": ["prod"]}},
    )  # fmt: skip
    assert d.action == "deny"
    d = _check(
        tmp_path, "aws --profile '' s3 rm s3://b/k", environ={"AWS_PROFILE": "live"},
        protect=PROTECT,
    )  # fmt: skip
    assert d.action == "ask"
    # allowlist mode still asks even when the default is allowed
    allow = {"startup": {"kube_context": ["dev"]}}
    d = _check(tmp_path, "kubectl --context '' get po", run=_ctx("dev"), allow=allow,
               mode="allowlist")  # fmt: skip
    assert d.action == "ask"
    assert d.unknown == ["kube_context"]


def _remote(url):
    def run(argv, **kw):
        if argv[:2] == ["git", "-C"]:
            return subprocess.CompletedProcess(argv, 0, f"origin\t{url} (push)\n", "")
        return subprocess.CompletedProcess(argv, 1, "", "")

    return run


@pytest.mark.parametrize("mode", ["protect", "allowlist"])
def test_host_entries_match_whatever_their_case(tmp_path, mode):
    """Hosts are read in lower case, so a listed ``GitHub.com`` must still deny, protect and
    mark another workspace's host."""
    run = _remote("https://github.com/o/r.git")
    d = _check(tmp_path, "git push", run=run, mode=mode,
               deny={"*": {"git_remote_host": ["GitHub.com"]}})  # fmt: skip
    assert d.action == "deny"
    d = _check(tmp_path, "git push", run=run, mode=mode,
               protect={"startup": {"git_remote_host": ["GITHUB.COM"]}})  # fmt: skip
    assert d.protected == [("git_remote_host", "github.com")]
    d = _check(tmp_path, "git remote add up https://h.example/r.git", mode=mode,
               deny={"*": {"git_remote_host": ["H.Example"]}})  # fmt: skip
    assert d.action == "deny"


def test_other_workspace_host_matches_whatever_its_case(tmp_path):
    run = _remote("git@github.com:o/r.git")
    d = _check(tmp_path, "git push", run=run,
               allow={"acme": {"git_remote_host": ["GitHub.com"]}})  # fmt: skip
    assert d.action == "ask"
    assert d.foreign == [("git_remote_host", "github.com", ("acme",))]


def test_symlink_inside_own_folder_into_another_workspace_asks(tmp_path):
    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    mine.mkdir()
    theirs.mkdir()
    (mine / "link").symlink_to(theirs)
    allow = {
        "startup": {"terraform_dir": [str(mine)]},
        "acme": {"terraform_dir": [str(theirs)]},
    }
    d = _check(tmp_path, f"terraform -chdir={mine}/link apply", allow=allow)
    assert d.action == "ask"
    assert d.foreign == [("terraform_dir", f"{mine}/link", ("acme",))]


def test_relative_value_of_a_user_folder_rule_is_read_from_the_command_folder(tmp_path):
    """``mytool --dir prod`` in a folder holding a denied ``prod`` acts on it."""
    d = tmp_path / "treant"
    d.mkdir(mode=0o700)
    work = tmp_path / "work"
    (work / "prod").mkdir(parents=True)
    (work / "dev").mkdir()
    f = d / "guard.toml"
    f.write_text(
        f'[workspaces]\n"{work}" = "startup"\n'
        f'[deny.startup]\nmydir = ["{work}/prod"]\n'
        f'[protect."*"]\nmydir = ["{work}/live"]\n'
        '[[rule]]\ncommand = ["mytool"]\nkind = "mydir"\nflags = ["--dir"]\n'
        'match = "path_prefix"\n'
    )
    f.chmod(0o600)
    cfg = load_config(d)
    assert cfg.error is None

    def run(command, cwd=work):
        return check(command, cwd=str(cwd), workspace_dir=str(work), environ={}, config=cfg)

    assert run("mytool --dir prod run").action == "deny"
    assert run("mytool --dir ../prod run", cwd=work / "dev").action == "deny"
    assert run("cd dev && mytool --dir ../prod run").action == "deny"
    assert run("mytool --dir live/x run").action == "ask"
    assert run("mytool --dir dev run").action == "allow"
    # allowlist mode keeps asking for a relative value, even inside an allowed folder
    strict = replace(cfg, mode="allowlist", allow={"startup": {"mydir": [str(work)]}})
    d = check("mytool --dir dev run", cwd=str(work), workspace_dir=str(work), environ={},
              config=strict)  # fmt: skip
    assert d.action == "ask"
    assert d.not_allowed == [("mydir", "dev")]
    d = check(f"mytool --dir {work}/dev run", cwd=str(work), workspace_dir=str(work), environ={},
              config=strict)  # fmt: skip
    assert d.action == "allow"
