import os
import subprocess
import time

import pytest

from treant_guard.config import GuardConfig, load_config
from treant_guard.engine import Decision, check, format_reason, prefilter
from treant_guard.rules import default_rules


def _no_run(argv, **kw):
    return subprocess.CompletedProcess(argv, 1, "", "")


def _cfg(tmp_path, allow, error=None, deny=None, rules=None):
    return GuardConfig(
        rules=rules or default_rules(),
        workspaces={str(tmp_path): "acme"},
        allow=allow,
        deny=deny or {},
        about={},
        error=error,
        mode="allowlist",  # these tests pin the allow-list behaviour; see test_protect.py
    )


def _check(tmp_path, command, targets, environ=None, run=_no_run, deny=None, rules=None, **kw):
    return check(
        command,
        cwd=str(tmp_path),
        workspace_dir=str(tmp_path),
        environ=environ or {},
        config=_cfg(tmp_path, {"acme": targets}, deny=deny, rules=rules),
        run=run,
        home="/home/x",
        **kw,
    )


def test_listed_targets_allow(tmp_path):
    d = _check(tmp_path, "kubectl --context dev get po", {"kube_context": ["dev"]})
    assert d == Decision("allow", "acme", [], [], [], True, None)


def test_unlisted_and_unresolved_ask_with_exact_reason(tmp_path):
    d = _check(
        tmp_path,
        "kubectl --context prod get po && aws s3 ls",
        {"kube_context": ["dev"]},
    )
    assert d.action == "ask"
    assert d.not_allowed == [("kube_context", "prod")]
    assert d.unknown == ["aws_profile"]
    assert d.reason == (
        "Treant guard (acme): not allowed in acme: kube_context=prod; "
        "can't tell which aws_profile this uses"
    )


def test_tf_root_prefix_listed(tmp_path):
    root = tmp_path / "infra"
    (root / "stack").mkdir(parents=True)
    d = _check(tmp_path, f"terraform -chdir={root}/stack plan", {"terraform_dir": [str(root)]})
    assert d.action == "allow"
    d = _check(tmp_path, f"terraform -chdir={tmp_path}/other plan", {"terraform_dir": [str(root)]})
    assert d.action == "ask"
    assert d.not_allowed[0][0] == "terraform_dir"


def test_no_workspace_allows(tmp_path):
    d = check(
        "kubectl --context prod get po",
        cwd="/elsewhere",
        workspace_dir="/elsewhere",
        environ={},
        config=_cfg(tmp_path, {}),
        run=_no_run,
    )
    assert d.action == "allow"
    assert d.workspace is None


def test_empty_targets_ask(tmp_path):
    d = _check(tmp_path, "kubectl --context prod get po", {})
    assert d.action == "ask"
    assert d.reason == "Treant guard (acme): not allowed in acme: kube_context=prod"


def test_unparsable_asks(tmp_path):
    d = _check(tmp_path, "kubectl 'unterminated", {"kube_context": ["dev"]})
    assert d.action == "ask"
    assert d.reason == "Treant guard (acme): could not parse the command"


def test_invalid_config_asks_for_pack_commands_anywhere(tmp_path):
    cfg = GuardConfig(default_rules(), {}, {}, {}, {}, error="/c/guard.toml", mode="allowlist")
    kw = {
        "cwd": "/anywhere",
        "workspace_dir": "/anywhere",
        "environ": {},
        "config": cfg,
        "run": _no_run,
    }
    d = check("kubectl get po", **kw)
    assert d.action == "ask"
    assert d.reason == "Treant guard: guard config invalid: /c/guard.toml"
    assert check("ls", **kw).action == "allow"


def test_internal_error_asks(tmp_path):
    class Boom:
        def lists(self, workspace):
            raise RuntimeError("x")

    d = _check(tmp_path, "kubectl --context dev get po", {}, source=Boom())
    assert d.action == "ask"
    assert d.reason == "Treant guard: guard error"


def test_mentions_are_allowed(tmp_path):
    for cmd in ("echo kubectl", 'git commit -m "push"', "cat wrangler.toml"):
        d = _check(tmp_path, cmd, {})
        assert d.action == "allow", cmd
        assert d.matched is False, cmd


def test_prefilter():
    pf = prefilter(default_rules())
    assert pf("kubectl get po")
    assert pf("cd x && /usr/bin/kubectl get po")
    assert not pf("cat wrangler.toml")
    assert not pf("my-kubectl-wrapper")
    assert not pf("ls")


def test_rules_in_cwd_are_ignored(tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    (cwd / "guard.toml").write_text(
        '[[rule]]\ncommand = ["kubectl"]\nkind = "kube_context"\ndisabled = true\n'
    )
    conf = tmp_path / "conf"
    conf.mkdir()
    conf.chmod(0o700)
    cfg = load_config(conf)
    cfg = GuardConfig(cfg.rules, {str(cwd): "ws"}, {}, {}, {}, cfg.error, mode="allowlist")
    d = check(
        "kubectl --context x get po",
        cwd=str(cwd),
        workspace_dir=str(cwd),
        environ={},
        config=cfg,
        run=_no_run,
    )
    assert d.action == "ask"


def test_check_no_fallback_under_20ms(tmp_path):
    cfg = GuardConfig(
        default_rules(),
        {str(tmp_path): "ws"},
        {"ws": {"kube_context": ["ops"]}},
        {},
        {},
        mode="allowlist",
    )

    def batch():
        start = time.perf_counter()
        for _ in range(20):
            d = check(
                "kubectl --context ops get po",
                cwd=str(tmp_path),
                workspace_dir=str(tmp_path),
                environ={},
                config=cfg,
                run=_no_run,
            )
        assert d.action == "allow"
        return (time.perf_counter() - start) / 20

    # the best of five batches: a busy machine slows some batches, not the code
    assert min(batch() for _ in range(5)) < 0.02


def test_format_reason():
    assert format_reason("w", [], [], []) is None
    assert format_reason("w", [], [("a", "1"), ("b", "2")], ["x", "y"]) == (
        "Treant guard (w): not allowed in w: a=1, b=2; can't tell which x, y this uses"
    )
    assert format_reason("w", [("k", "v", False), ("j", "u", True)], [], []) == (
        "Treant guard (w): denied in w: k=v; denied everywhere: j=u"
    )


def test_switched_kubeconfig_or_unset_profile_asks(tmp_path):
    def ops(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, "ops\n", "")

    targets = {"kube_context": ["ops"], "aws_profile": ["ops"]}
    for command, kind in (
        ("KUBECONFIG=~/.kube/config_startup kubectl delete pod x", "kube_context"),
        ("kubectl --kubeconfig ~/.kube/config_prod delete pod x", "kube_context"),
        ("env -u AWS_PROFILE aws s3 ls", "aws_profile"),
        ("AWS_PROFILE= aws s3 ls", "aws_profile"),
    ):
        d = _check(tmp_path, command, targets, environ={"AWS_PROFILE": "ops"}, run=ops)
        assert d.action == "ask", command
        assert d.reason == f"Treant guard (acme): can't tell which {kind} this uses", command
    d = _check(tmp_path, "kubectl delete pod x", targets, run=ops)
    assert d.action == "allow"


def test_deny_in_workspace(tmp_path):
    d = _check(
        tmp_path,
        "kubectl --context prod get po",
        {"kube_context": ["prod"]},
        deny={"acme": {"kube_context": ["prod"]}},
    )
    assert d.action == "deny"
    assert d.denied == [("kube_context", "prod", False)]
    assert d.not_allowed == []
    assert d.reason == "Treant guard (acme): denied in acme: kube_context=prod"


def test_star_deny_reads_everywhere(tmp_path):
    d = _check(
        tmp_path,
        "kubectl --context prod get po",
        {},
        deny={"*": {"kube_context": ["prod"]}},
    )
    assert d.action == "deny"
    assert d.denied == [("kube_context", "prod", True)]
    assert d.reason == "Treant guard (acme): denied everywhere: kube_context=prod"
    d = _check(
        tmp_path,
        "kubectl --context prod get po",
        {},
        deny={"*": {"kube_context": ["prod"]}, "acme": {"kube_context": ["prod"]}},
    )
    assert d.denied == [("kube_context", "prod", False)]


def test_deny_beats_allow_across_files(tmp_path):
    (tmp_path / "guard.toml").write_text(
        f'[workspaces]\n"{tmp_path}" = "acme"\n[allow.acme]\nkube_context = ["prod"]\n'
    )
    (tmp_path / "guard.toml").chmod(0o600)
    guard_d = tmp_path / "guard.d"
    guard_d.mkdir(mode=0o700)
    guard_d.chmod(0o700)
    (guard_d / "acme.toml").write_text('[deny.acme]\nkube_context = ["prod"]\n')
    (guard_d / "acme.toml").chmod(0o600)
    cfg = load_config(tmp_path)
    assert cfg.error is None, cfg.error_detail
    d = check(
        "kubectl --context prod get po",
        cwd=str(tmp_path),
        workspace_dir=str(tmp_path),
        environ={},
        config=cfg,
        run=_no_run,
    )
    assert d.action == "deny"
    assert d.denied == [("kube_context", "prod", False)]


def test_star_deny_ignores_unmapped_folder(tmp_path):
    d = check(
        "kubectl --context prod get po",
        cwd="/elsewhere",
        workspace_dir="/elsewhere",
        environ={},
        config=_cfg(tmp_path, {}, deny={"*": {"kube_context": ["prod"]}}),
        run=_no_run,
    )
    assert d.action == "allow"
    assert d.workspace is None


def test_unknown_is_never_denied(tmp_path):
    d = _check(
        tmp_path,
        "kubectl get po",
        {},
        deny={"*": {"kube_context": ["prod"]}},
    )
    assert d.action == "ask"
    assert d.denied == []
    assert d.unknown == ["kube_context"]


def test_terraform_dir_deny_by_prefix(tmp_path):
    root = tmp_path / "infra"
    (root / "stack").mkdir(parents=True)
    d = _check(
        tmp_path,
        f"terraform -chdir={root}/stack plan",
        {"terraform_dir": [str(tmp_path)]},
        deny={"acme": {"terraform_dir": [str(root)]}},
    )
    assert d.action == "deny"
    assert d.denied == [("terraform_dir", f"{root}/stack", False)]
    d = _check(
        tmp_path,
        f"terraform -chdir={tmp_path}/other plan",
        {"terraform_dir": [str(tmp_path)]},
        deny={"acme": {"terraform_dir": [str(root)]}},
    )
    assert d.action == "allow"


def test_deny_follows_rule_match(tmp_path):
    """A user [[rule]] from the config makes its kind a folder kind that deny matches by
    prefix."""
    d = tmp_path / "treant"
    d.mkdir(mode=0o700)
    root = tmp_path / "proj"
    (d / "guard.toml").write_text(
        f'[workspaces]\n"{tmp_path}" = "acme"\n[guard]\nmode = "allowlist"\n'
        f'[allow.acme]\nworkdir = ["{tmp_path}"]\n[deny.acme]\nworkdir = ["{root}"]\n'
        '[[rule]]\ncommand = ["mytool"]\nkind = "workdir"\nflags = ["--dir"]\n'
        'match = "path_prefix"\n'
    )
    (d / "guard.toml").chmod(0o600)
    cfg = load_config(d)
    assert cfg.error is None

    def run(command):
        return check(command, cwd=str(tmp_path), workspace_dir=str(tmp_path), environ={},
                     config=cfg, run=_no_run)  # fmt: skip

    res = run(f"mytool --dir {root}/sub run")
    assert res.action == "deny"
    assert res.denied == [("workdir", f"{root}/sub", False)]
    assert run(f"mytool --dir {tmp_path}/other run").action == "allow"


def test_mixed_deny_and_ask_reason_order(tmp_path):
    d = _check(
        tmp_path,
        "kubectl --context prod get po && aws --profile x s3 ls && git push",
        {},
        deny={"acme": {"kube_context": ["prod"]}},
        run=_no_run,
    )
    assert d.action == "deny"
    assert d.reason == (
        "Treant guard (acme): denied in acme: kube_context=prod; "
        "not allowed in acme: aws_profile=x; can't tell which git_remote_host this uses"
    )


def test_deny_follows_symlink_into_denied_folder(tmp_path):
    work = tmp_path / "work"
    (tmp_path / "prod").mkdir()
    work.mkdir()
    deny = {"acme": {"terraform_dir": [str(tmp_path / "prod")]}}
    allow = {"terraform_dir": [str(work)]}
    (work / "link").symlink_to("../prod")
    d = _check(tmp_path, f"terraform -chdir={work}/link plan", allow, deny=deny)
    assert d.action == "deny"
    d = check(
        "terraform plan",
        cwd=str(work / "link"),
        workspace_dir=str(tmp_path),
        environ={},
        config=_cfg(tmp_path, {"acme": allow}, deny={"acme": deny["acme"]}),
        run=_no_run,
        home="/home/x",
    )
    assert d.action == "deny"


def test_deny_entry_that_is_a_symlink_matches_its_target(tmp_path):
    (tmp_path / "prod").mkdir()
    (tmp_path / "alias").symlink_to("prod")
    d = _check(
        tmp_path,
        f"terraform -chdir={tmp_path}/prod plan",
        {"terraform_dir": [str(tmp_path)]},
        deny={"acme": {"terraform_dir": [str(tmp_path / "alias")]}},
    )
    assert d.action == "deny"


def test_late_path_rule_config_never_allows(tmp_path):
    d = tmp_path / "treant"
    guard_d = d / "guard.d"
    guard_d.mkdir(parents=True)
    d.chmod(0o700)
    guard_d.chmod(0o700)
    files = {
        "a.toml": f'[workspaces]\n"{tmp_path}" = "acme"\n[deny.acme]\nmydir = ["prod"]\n',
        "z.toml": (
            '[[rule]]\ncommand = ["mytool"]\nkind = "mydir"\nflags = ["--dir"]\n'
            'match = "path_prefix"\n'
        ),
    }
    for name, text in files.items():
        (guard_d / name).write_text(text)
        (guard_d / name).chmod(0o600)
    cfg = load_config(d)
    assert cfg.error == str(guard_d / "a.toml")
    assert cfg.error_detail == "mydir values must be absolute paths: prod"
    res = check(
        "kubectl get po",
        cwd=str(tmp_path),
        workspace_dir=str(tmp_path),
        environ={},
        config=cfg,
        run=_no_run,
    )
    assert res.action != "allow"


# --- same-line state ---------------------------------------------------------


def test_state_in_subshell_pipe_or_background_does_not_carry(tmp_path):
    dev, prod = tmp_path / "dev", tmp_path / "prod"
    dev.mkdir()
    prod.mkdir()
    targets = {"terraform_dir": [str(dev)]}
    # not followed, so the rest of the line can't tell
    for command in (
        f"(cd {prod}); cd {dev} && terraform apply",
        f"cd {dev}; cd {prod} | cat; terraform apply",
        f"cd {dev}; cd {prod} & terraform apply",
    ):
        d = _check(tmp_path, command, targets)
        assert d.action == "ask", command
        assert d.unknown == ["terraform_dir"], command
    d = _check(tmp_path, f"cd {dev}; cd {prod}; terraform apply", targets)
    assert d.action == "ask"
    assert d.not_allowed == [("terraform_dir", str(prod))]


def test_unreadable_value_asks_even_under_allowed_prefix(tmp_path):
    (tmp_path / "infra").mkdir()
    targets = {"terraform_dir": [str(tmp_path)], "aws_profile": ["dev"]}
    for command, kind in (
        (f'cd "{tmp_path}/$X" && terraform plan', "terraform_dir"),
        (f"cd {tmp_path}/`echo infra` && terraform plan", "terraform_dir"),
        ("export AWS_PROFILE=$(echo dev); aws s3 ls", "aws_profile"),
    ):
        d = _check(tmp_path, command, targets, environ={"AWS_PROFILE": "dev"})
        assert d.action == "ask", command
        assert d.reason == f"Treant guard (acme): can't tell which {kind} this uses", command


def test_source_line_asks_despite_allowed_flag(tmp_path):
    targets = {"kube_context": ["ops"]}
    assert _check(tmp_path, "kubectl --context ops get ns", targets).action == "allow"
    for prefix in ("source x", ". x", "eval x", "f() { :; }", "alias k=kubectl"):
        d = _check(tmp_path, f"{prefix}; kubectl --context ops get ns", targets)
        assert d.action == "ask", prefix
        # an alias naming a guarded program: every kind can't tell
        assert d.unknown[0] == "kube_context", prefix
        assert len(d.unknown) == (6 if prefix.startswith("alias") else 1), prefix


def test_lone_assignment_follows_export_state(tmp_path):
    targets = {"aws_profile": ["dev"]}
    # not exported: aws still reads AWS_DEFAULT_PROFILE only
    d = _check(tmp_path, "AWS_PROFILE=prod; aws s3 ls", targets, {"AWS_DEFAULT_PROFILE": "dev"})
    assert d.action == "allow"
    # already in the hook environment: the new value counts
    d = _check(tmp_path, "AWS_PROFILE=prod; aws s3 ls", targets, {"AWS_PROFILE": "dev"})
    assert d.action == "ask"
    assert d.not_allowed == [("aws_profile", "prod")]


def test_or_loop_and_case_ask(tmp_path):
    (tmp_path / "dev").mkdir()
    targets = {"terraform_dir": [str(tmp_path)], "kube_context": ["ops"]}
    for command in (
        "cd dev || terraform apply",
        "for d in dev; do cd $d && terraform apply; done",
        "case x in x) kubectl --context ops delete ns a;; esac",
    ):
        assert _check(tmp_path, command, targets).action == "ask", command


def _check_in(tmp_path, cwd, command, targets, environ):
    return check(
        command,
        cwd=str(cwd),
        workspace_dir=str(tmp_path),
        environ=environ,
        config=_cfg(tmp_path, {"acme": targets}),
        run=_no_run,
        home="/home/x",
    )


def test_review_probes_ask(tmp_path):
    dev, prod = tmp_path / "dev", tmp_path / "prod"
    dev.mkdir()
    prod.mkdir()
    targets = {"terraform_dir": [str(dev)], "aws_profile": ["dev"]}
    hook = {"AWS_PROFILE": "prod"}
    for command, environ in (
        # a change that may not run
        ('[ -z "$AWS_PROFILE" ] && export AWS_PROFILE=dev; aws s3 ls', hook),
        ("false && export AWS_PROFILE=dev; aws s3 ls", hook),
        (f"false && cd {dev}; terraform apply", {}),
        (f"true || cd {dev}; terraform apply", {}),
        (f"git diff --quiet && cd {dev}\nterraform apply", {}),
        # `&` backgrounds the whole list
        (f"cd {dev} && terraform plan & terraform apply", {}),
        ("export AWS_PROFILE=dev && aws s3 ls & aws s3 rm s3://b/k", hook),
        # comments
        ("true # ; export AWS_PROFILE=dev\naws s3 rm s3://b/k", hook),
        (f"true # ; cd {dev}\nterraform apply", {}),
        ("unset X # keep AWS_PROFILE\naws s3 ls", {**hook, "AWS_DEFAULT_PROFILE": "dev"}),
        ("export AWS_PROFILE=prod  # was AWS_PROFILE=dev\naws s3 ls", {}),
        ("aws s3 ls  # add --profile dev", hook),
        # a here-document marker glued to the command
        (f"cat>s.sh<<EOF\ncd {dev}\nEOF\nterraform apply", {}),
    ):
        d = _check_in(tmp_path, prod, command, targets, environ)
        assert d.action == "ask", command
    # the exact forms still allow
    for command, environ in (
        (f"cd {dev} && terraform apply", {}),
        (f"cd {dev}; terraform apply", {}),
        ("export AWS_PROFILE=dev && aws s3 ls", hook),
    ):
        assert _check_in(tmp_path, prod, command, targets, environ).action == "allow", command


def test_review_round2_probes_ask(tmp_path):
    dev, prod = tmp_path / "dev", tmp_path / "prod"
    dev.mkdir()
    prod.mkdir()
    tf = {"terraform_dir": [str(dev)]}
    aws = {"aws_profile": ["dev"]}
    p, dv = {"AWS_PROFILE": "prod"}, {"AWS_PROFILE": "dev"}
    for command, targets, environ, cwd in (
        # `||` before the change in the same list
        (f"true || cd {dev} && terraform apply", tf, {}, prod),
        (f"false || cd {dev} && terraform apply", tf, {}, prod),
        (f"a && b || cd {dev} && terraform apply", tf, {}, prod),
        ("true || export AWS_PROFILE=dev && aws s3 ls", aws, p, tmp_path),
        # groups run with `&`
        (f"{{ cd {dev}; }} & terraform apply", tf, {}, prod),
        ("{ export AWS_PROFILE=dev; } & aws s3 rm s3://b/k", aws, p, tmp_path),
        (f"if true; then cd {dev}; fi & terraform apply", tf, {}, prod),
        # text bash runs that a comment rule would drop
        (f"echo a\\ #b; cd {prod}\nterraform apply", tf, {}, dev),
        (f"echo a\\;#b; cd {prod}\nterraform apply", tf, {}, dev),
        ("echo x\\ #; export AWS_PROFILE=prod\naws s3 rm s3://b/k", aws, dv, tmp_path),
        ("echo x\\&#; export AWS_PROFILE=prod\naws s3 rm s3://b/k", aws, dv, tmp_path),
        (f"echo ${{X:-a #}}; cd {prod}\nterraform apply", tf, {}, dev),
        (f"echo `true #`; cd {prod}\nterraform apply", tf, {}, dev),
        (f"echo $'a\\' #'; cd {prod}\nterraform apply", tf, {}, dev),
        # backtick body, assignment with a redirect
        ("echo ` true; export AWS_PROFILE=dev ` ; aws s3 ls", aws, p, tmp_path),
        ("AWS_PROFILE=prod 2>/dev/null; aws s3 ls", aws, dv, tmp_path),
    ):
        d = _check_in(tmp_path, cwd, command, targets, environ)
        assert d.action == "ask", command
    # plain chains stay exact
    for command, targets, environ in (
        (f"cd {dev} && terraform apply", tf, {}),
        (f"cd {dev}; terraform apply", tf, {}),
        (f"cd {dev}\nterraform apply", tf, {}),
        ("export AWS_PROFILE=dev; aws s3 ls", aws, p),
        ("export AWS_PROFILE=dev && aws s3 ls", aws, p),
        ("export AWS_PROFILE=dev\naws s3 ls", aws, p),
    ):
        d = _check_in(tmp_path, prod, command, targets, environ)
        assert d.action == "allow", command


def test_review_round3_probes_ask(tmp_path):
    dev, prod = tmp_path / "dev", tmp_path / "prod"
    dev.mkdir()
    prod.mkdir()
    tf = {"terraform_dir": [str(dev)]}
    aws = {"aws_profile": ["dev"]}
    p, dv = {"AWS_PROFILE": "prod"}, {"AWS_PROFILE": "dev"}
    adp = {"AWS_DEFAULT_PROFILE": "dev"}
    for command, targets, environ, cwd in (
        # C1: blur by event
        (f"cd {prod}; false && cd {dev}; terraform apply", tf, {}, dev),
        (f"cd {prod}; cd {dev} | cat; terraform apply", tf, {}, dev),
        (f"cd {prod}; cd {dev} & terraform apply", tf, {}, dev),
        (f"cd {prod}; if false; then cd {dev}; fi; terraform apply", tf, {}, dev),
        (f"cd {prod}; false && {{ cd {dev}; }}; terraform apply", tf, {}, dev),
        (f"cd {prod}; if true; then :; else cd {dev}; fi; terraform apply", tf, {}, dev),
        (f"cd {prod}\nfalse && cd {dev}\nterraform apply", tf, {}, dev),
        (f"cd {prod} && false && cd {dev}; terraform apply", tf, {}, dev),
        ("set -a; false && set +a; AWS_PROFILE=prod; aws s3 rm s3://b/k", aws, adp, tmp_path),
        ("set -a; set +a | cat; AWS_PROFILE=prod; aws s3 rm s3://b/k", aws, adp, tmp_path),
        (f"cd {prod}; {{ cd {dev}; }} | cat; terraform apply", tf, {}, dev),
        (f"cd {prod}; false && cd {dev} || true; terraform apply", tf, {}, dev),
        (f"cd {prod}; cd {dev} 2>/dev/null & terraform apply", tf, {}, dev),
        # C2: escaped backslash before a newline
        (f"echo a\\\\\ncd {prod}\nterraform apply", tf, {}, dev),
        ("echo a\\\\\nexport AWS_PROFILE=prod\naws s3 rm s3://b/k", aws, dv, tmp_path),
        (f"true \\\\\ncd {prod} && terraform apply", tf, {}, dev),
        # I1: quoted redirect lookalikes
        ("AWS_PROFILE=dev '>' x; aws s3 rm s3://b/k", aws, p, tmp_path),
        ("AWS_PROFILE=dev '>x'; aws s3 rm s3://b/k", aws, p, tmp_path),
        ("AWS_PROFILE=dev \\>x; aws s3 rm s3://b/k", aws, p, tmp_path),
        # ${…}, time {…}, computed command name
        (f"echo ${{X:-a; cd {dev}; true }}\nterraform apply", tf, {}, prod),
        ("echo ${X:-a; export AWS_PROFILE=dev }\naws s3 rm s3://b/k", aws, p, tmp_path),
        (f"time {{ cd {prod}; }}; terraform apply", tf, {}, dev),
        ("time { export AWS_PROFILE=prod; }; aws s3 rm s3://b/k", aws, dv, tmp_path),
        (f"cd${{IFS}}{prod}; terraform apply", tf, {}, dev),
    ):
        d = _check_in(tmp_path, cwd, command, targets, environ)
        assert d.action == "ask", command
    for command, targets, environ in (
        (f"cd {dev} && terraform apply", tf, {}),
        (f"cd {dev}; terraform apply", tf, {}),
        ("export AWS_PROFILE=dev; aws s3 ls", aws, p),
        ("export AWS_PROFILE=dev && aws s3 ls", aws, p),
        (f"pushd {dev} >/dev/null && terraform apply", tf, {}),
        (f"pushd {dev} > /dev/null && terraform apply", tf, {}),
        (f"cd {dev} 2>/dev/null && terraform apply", tf, {}),
    ):
        d = _check_in(tmp_path, prod, command, targets, environ)
        assert d.action == "allow", command


def test_review_round4_probes_ask(tmp_path):
    """Brace expansion, zsh forms, computed words, `${A:=}`, trap, `+=`, cd search paths,
    `local`, declare flags, a folder `cd` cannot enter: each asks."""
    dev, prod, locked = tmp_path / "dev", tmp_path / "prod", tmp_path / "locked"
    for d in (dev, prod, locked, tmp_path / "other" / "dev"):
        d.mkdir(parents=True)
    tf = {"terraform_dir": [str(dev)]}
    aws = {"aws_profile": ["dev"]}
    dv, p = {"AWS_PROFILE": "dev"}, {"AWS_PROFILE": "prod"}
    adp = {"AWS_DEFAULT_PROFILE": "dev"}
    rm = "aws s3 rm s3://b/k"
    cases = [
        (f"{{cd,{prod}}}; terraform apply", tf, {}, dev),
        (f"{{export,AWS_PROFILE=prod}}; {rm}", aws, dv, tmp_path),
        (f"V=AWS_PROFILE=prod; export $V; {rm}", aws, dv, tmp_path),
        (f"export AWS_PROFILE+=prod; {rm}", aws, adp, tmp_path),
        (f"AWS_PROFILE+=prod {rm}", aws, adp, tmp_path),
        (f": ${{AWS_PROFILE:=prod}}; export AWS_PROFILE; {rm}", aws, adp, tmp_path),
        (
            f"CDPATH={tmp_path}/other cd dev && terraform apply",
            {"terraform_dir": [str(dev)]},
            {},
            tmp_path,
        ),
        (f"local AWS_PROFILE=dev; {rm}", aws, p, tmp_path),
        (f"chdir {prod}; terraform apply", tf, {}, dev),
        (f"chdir {dev}; terraform apply", tf, {}, prod),
        (f"noglob cd {prod}; terraform apply", tf, {}, dev),
        (f"nocorrect cd {prod}; terraform apply", tf, {}, dev),
        (f"repeat 1 cd {prod}; terraform apply", tf, {}, dev),
        (f"setopt allexport; AWS_PROFILE=prod; {rm}", aws, adp, tmp_path),
        (f"unsetopt noallexport; AWS_PROFILE=prod; {rm}", aws, adp, tmp_path),
        (f"trap 'cd {prod}' DEBUG; terraform apply", tf, {}, dev),
        (f"trap 'cd {prod}' ERR; false; terraform apply", tf, {}, dev),
        (f"emulate sh -c 'cd {prod}'; terraform apply", tf, {}, dev),
        (f"{prod}; terraform apply", tf, {}, dev),
        ("../prod && terraform apply", tf, {}, dev),
        (f"declare -n AWS_PROFILE; {rm}", aws, {**p, **adp}, tmp_path),
        (f"O=a; set -$O; AWS_PROFILE=prod; {rm}", aws, adp, tmp_path),
        (f"X=-x; declare $X AWS_PROFILE=prod; {rm}", aws, adp, tmp_path),
        (f"V=AWS_PROFILE; unset $V; {rm}", aws, {**dv, "AWS_DEFAULT_PROFILE": "prod"}, tmp_path),
        (f"set -k; {rm} AWS_PROFILE=prod", aws, adp, tmp_path),
        (f"enable -n cd; cd {dev}; terraform apply", tf, {}, prod),
        (f"HOME={prod}; cd; terraform apply", tf, {}, dev),
    ]
    if os.geteuid() != 0:
        locked.chmod(0)
        cases.append((f"cd {locked}; terraform apply", {"terraform_dir": [str(locked)]}, {}, prod))
    try:
        for command, targets, environ, cwd in cases:
            d = _check_in(tmp_path, cwd, command, targets, environ)
            assert d.action == "ask", command
    finally:
        locked.chmod(0o755)
    for command, targets, environ in (
        (f"cd {dev} && terraform apply", tf, {}),
        (f"cd {dev}; terraform apply", tf, {}),
        (f"cd {dev}\nterraform apply", tf, {}),
        ("export AWS_PROFILE=dev; aws s3 ls", aws, p),
        ("export AWS_PROFILE=dev && aws s3 ls", aws, p),
        ("export AWS_PROFILE=dev\naws s3 ls", aws, p),
        (f"pushd {dev} >/dev/null && terraform apply", tf, {}),
        (f"cd {dev} 2>/dev/null && terraform apply", tf, {}),
        (f"cd {dev} && terraform plan | tee /tmp/p", tf, {}),
        ("AWS_PROFILE=dev aws s3 ls", aws, p),
        ("set -euo pipefail; export AWS_PROFILE=dev; aws s3 ls", aws, p),
    ):
        d = _check_in(tmp_path, prod, command, targets, environ)
        assert d.action == "allow", command


def test_review_round5_probes_ask(tmp_path):
    """Declarations that split, zsh `set -o` spellings, read-only and global variables,
    zsh short forms after a test, computed words in a matched command: each asks."""
    aws = {"aws_profile": ["dev"]}
    p, dv = {"AWS_PROFILE": "prod"}, {"AWS_PROFILE": "dev"}
    adp = {"AWS_DEFAULT_PROFILE": "dev"}
    rm = "aws s3 rm s3://b/k"
    x = 'X="1 AWS_PROFILE=prod"; '
    cases = [
        (f"{x}builtin export A=$X; {rm}", adp),
        (f"{x}builtin declare -x A=$X; {rm}", adp),
        (f"builtin export A=$X; {rm}", adp),
        (f"{x}\\export A=$X; {rm}", adp),
        (f'{x}"export" A=$X; {rm}', adp),
        (f'{x}ex""port A=$X; {rm}', adp),
        (f"{x}'declare' -x A=$X; {rm}", adp),
        (f"{x}B=1 export A=$X; {rm}", adp),
        (f'X="1 AWS_PROFILE"; unset A=$X; {rm}', {**dv, "AWS_DEFAULT_PROFILE": "prod"}),
        (f"set -a; set -o noallexport; AWS_PROFILE=prod; {rm}", adp),
        (f"set -a; set +o all_export; AWS_PROFILE=prod; {rm}", adp),
        (f"set -a; set +o ALLEXPORT; AWS_PROFILE=prod; {rm}", adp),
        (f"readonly AWS_PROFILE; export AWS_PROFILE=dev; {rm}", p),
        (f"declare -r AWS_PROFILE; export AWS_PROFILE=dev; {rm}", p),
        (f"typeset -r AWS_PROFILE; AWS_PROFILE=dev; {rm}", p),
        (f"readonly AWS_PROFILE; AWS_PROFILE=dev {rm}", p),
        (f"readonly AWS_PROFILE; unset AWS_PROFILE; {rm}", {**p, **adp}),
        (f"declare -gx AWS_PROFILE=dev; {rm}", p),
        (f"readonly -x AWS_PROFILE=dev; {rm}", p),
        (f"declare -g AWS_PROFILE=dev; {rm}", p),
        ("while [[ 1 ]] { aws s3 rm s3://b/k --profile prod; break }", dv),
        ("if [[ 1 ]] aws s3 rm s3://b/k --profile prod", dv),
        ("aws {--profile,prod} s3 rm s3://b/k", dv),
        (f'X="--profile prod"; {rm} $X', dv),
    ]
    for command, environ in cases:
        d = _check_in(tmp_path, tmp_path, command, aws, environ)
        assert d.action == "ask", command
    for command, environ in (
        (f"{x}export A=$X; {rm}", adp),
        ("set -euo pipefail; export AWS_PROFILE=dev; aws s3 ls", p),
        ("set -o allexport; AWS_PROFILE=dev; aws s3 ls", p),
        ("declare -x -- AWS_PROFILE=dev; aws s3 ls", p),
        ("[[ -n x ]] && aws s3 ls", dv),
        ('aws s3 cp f "s3://b/$K"', dv),
    ):
        d = _check_in(tmp_path, tmp_path, command, aws, environ)
        assert d.action == "allow", command


def test_switch_then_kubectl_asks_at_engine_level(tmp_path):
    d = _check(tmp_path, "kubectx prod && kubectl get po", {"kube_context": ["prod"]})
    assert d.action == "ask" and d.unknown == ["kube_context"]


def test_loop_variable_asks(tmp_path):
    rm = "aws s3 rm s3://b/k"
    for command in (
        f"for AWS_PROFILE in prod; do {rm}; done",
        f"select AWS_PROFILE in prod; do {rm}; break; done",
    ):
        d = _check(tmp_path, command, {"aws_profile": ["dev"]}, environ={"AWS_PROFILE": "dev"})
        assert d.action == "ask" and d.unknown == ["aws_profile"], command
    d = _check(
        tmp_path,
        "for x in a b; do aws s3 ls; done",
        {"aws_profile": ["dev"]},
        environ={"AWS_PROFILE": "dev"},
    )
    assert d.action == "allow"


def test_common_git_forms_allow_after_switch_narrowing(tmp_path):
    def run(argv, **kw):
        out = "origin\tgit@github.com:a/b.git (push)\n" if "remote" in argv else ""
        return subprocess.CompletedProcess(argv, 0, out, "")

    gh = {"git_remote_host": ["github.com"]}
    for command in (
        "git commit -m \"$(cat <<'EOF'\nfix: x\nEOF\n)\" && git push",
        'git commit -m "$MSG" && git push',
        "git add config && git commit -m x && git push",
        "git remote -v && git push",
    ):
        assert _check(tmp_path, command, gh, run=run).action == "allow", command
    for command in ("git remote set-url origin x && git push", "kubectx prod && kubectl get ns"):
        assert _check(tmp_path, command, {**gh, "kube_context": ["dev"]}, run=run).action == "ask"


def test_final_wave_silent_allows_ask(tmp_path):
    """Folders after a symlink, persisting prefixes, computed or `=` command names, leading
    redirects and `>|`: each asks (or denies) rather than allows."""
    (tmp_path / "prod" / "sub").mkdir(parents=True)
    (tmp_path / "dev").mkdir()
    os.symlink(tmp_path / "prod" / "sub", tmp_path / "dev" / "link")
    tf = {"terraform_dir": [str(tmp_path / "dev")]}
    tfd = {"acme": {"terraform_dir": [str(tmp_path / "prod")]}}
    for command in (
        "cd -P dev/link/.. && terraform apply",
        "cd -P dev/link && cd .. && terraform apply",
        "terraform -chdir=dev/link/.. apply",
        "cd dev/link && terraform -chdir=.. apply",
    ):
        assert _check(tmp_path, command, tf, deny=tfd).action == "deny", command
    aws, kc = {"aws_profile": ["dev"]}, {"kube_context": ["dev"]}
    dev = {"AWS_PROFILE": "dev"}
    rm = "aws s3 rm s3://b/k"
    for command, targets, environ in (
        (f"AWS_PROFILE=prod export AWS_PROFILE; {rm}", aws, dev),
        (f"AWS_PROFILE=prod typeset -x AWS_PROFILE; {rm}", aws, dev),
        (f"={rm} --profile prod", aws, dev),
        ("=kubectl --context prod delete ns x", kc, {}),
        ("K=kubectl; $K --context prod delete ns x", kc, {}),
        ("{kubectl,} --context prod delete ns x", kc, {}),
        (f">/dev/null {rm} --profile prod", aws, dev),
        (f"2>&1 {rm} --profile prod", aws, dev),
        (f"{rm} >| out --profile prod", aws, dev),
    ):
        assert _check(tmp_path, command, targets, environ=environ).action == "ask", command
    assert _check(tmp_path, f">/dev/null {rm} --profile dev", aws).action == "allow"


def test_hidden_programs_ask(tmp_path):
    """Wrappers, `env -S`, `sh -c`, git environment, zsh brace bodies and substitution
    bodies: each asks rather than allows."""
    kc, aws = {"kube_context": ["dev"]}, {"aws_profile": ["dev"]}
    gh = {"git_remote_host": ["github.com"]}

    def run(argv, **kw):
        out = "origin\tgit@github.com:a/b.git (push)\n" if "remote" in argv else ""
        return subprocess.CompletedProcess(argv, 0, out, "")

    kube = "kubectl --context prod delete ns x"
    for command, targets, environ in (
        (f"nice -n 5 {kube}", kc, {}),
        (f"stdbuf -oL {kube}", kc, {}),
        (f"caffeinate -t 5 {kube}", kc, {}),
        (f"coproc {kube}", kc, {}),
        (f"env -S '{kube}'", kc, {}),
        (f"bash -c '{kube}'", kc, {}),
        (f'echo "$({kube})"', kc, {}),
        (f"echo `{kube}`", kc, {}),
        ("while { true } { aws s3 rm s3://b/k --profile prod; break }", aws, {}),
        ("git push", gh, {"GIT_DIR": "/elsewhere/.git"}),
        ("git push --repo=git@prod.example.com:x", gh, {}),
    ):
        d = _check(tmp_path, command, targets, environ=environ, run=run)
        assert d.action == "ask", command
    d = _check(tmp_path, "git push", gh, run=run)
    assert d.action == "allow"


def test_printf_v_and_read_ask(tmp_path):
    aws, dev = {"aws_profile": ["dev"]}, {"AWS_PROFILE": "dev"}
    for command in ("printf -v AWS_PROFILE prod; aws s3 ls", "read AWS_PROFILE <f; aws s3 ls"):
        assert _check(tmp_path, command, aws, environ=dev).action == "ask", command


# --- minors (2026-11-02) --------------------------------------------------------


def test_set_noop_options_allow_followed_assignments(tmp_path):
    aws, p = {"aws_profile": ["dev"]}, {"AWS_PROFILE": "prod"}
    for command in (
        "set -euo pipefail; AWS_PROFILE=dev; aws s3 ls",
        "set -euo pipefail; export AWS_PROFILE=dev && aws s3 ls",
        "set -o pipefail\nAWS_PROFILE=dev\naws s3 ls",
        "set -eE; AWS_PROFILE=dev; aws s3 ls",
    ):
        assert _check(tmp_path, command, aws, environ=p).action == "allow", command
    adp = {"AWS_DEFAULT_PROFILE": "dev"}
    for command in (
        "set -o physical; AWS_PROFILE=prod; aws s3 ls",
        "set -o PIPE_FAIL -a; AWS_PROFILE=prod; aws s3 ls",
    ):
        assert _check(tmp_path, command, aws, environ=adp).action == "ask", command
    d = _check(tmp_path, "set -euo pipefail; AWS_PROFILE=prod; aws s3 ls", aws, environ=adp)
    assert d.action == "allow"


def test_redirect_inside_wrapper_asks(tmp_path):
    kc = {"kube_context": ["dev"]}
    for command in (
        "timeout >/dev/null 5 kubectl --context prod get po",
        "nice >/dev/null -n 5 kubectl --context prod get po",
        "sudo >/dev/null -u x kubectl --context prod get po",
    ):
        d = _check(tmp_path, command, kc)
        assert d.action == "ask" and d.not_allowed == [("kube_context", "prod")], command
    d = _check(tmp_path, "timeout >/dev/null 5 kubectl --context dev get po", kc)
    assert d.action == "allow"


def test_eval_and_heredoc_substitutions_ask(tmp_path):
    kc, aws = {"kube_context": ["dev"]}, {"aws_profile": ["dev"]}
    for command, targets in (
        ('eval "kubectl --context prod delete ns x"', kc),
        ('X="$(eval "$CMD")"; kubectl --context dev get po', kc),
        ("X=\"$(cat <<'EOF'\n1) one\nEOF\n)\"; aws --profile dev s3 ls", aws),
        ('fish --command "kubectl --context prod delete ns x"', kc),
        ('pwsh -Command "kubectl --context prod delete ns x"', kc),
    ):
        d = _check(tmp_path, command, targets)
        assert d.action == "ask", command


def test_quoted_hash_allows_push(tmp_path):
    def run(argv, **kw):
        out = "origin\tgit@github.com:a/b.git (push)\n"
        return subprocess.CompletedProcess(argv, 0, out, "")

    gh = {"git_remote_host": ["github.com"]}
    for command in ('git commit -m "fix #12" && git push', 'git commit -m "(#12)" && git push'):
        assert _check(tmp_path, command, gh, run=run).action == "allow", command
    assert _check(tmp_path, "git push # done", gh, run=run).action == "ask"


def test_default_from_reads_the_followed_folder(tmp_path):
    prod = tmp_path / "prod"
    prod.mkdir()

    def run(argv, **kw):
        ctx = "prod" if kw.get("cwd") == str(prod) else "dev"
        return subprocess.CompletedProcess(argv, 0, ctx + "\n", "")

    kc = {"kube_context": ["dev"]}
    assert _check(tmp_path, "kubectl get po", kc, run=run).action == "allow"
    d = _check(tmp_path, f"cd {prod} && kubectl get po", kc, run=run)
    assert d.action == "ask" and d.not_allowed == [("kube_context", "prod")]
    d = _check(tmp_path, "export X=$(date); kubectl get po", kc, run=run)
    assert d.action == "ask" and d.unknown == ["kube_context"]


def test_cd_home_follows_home_at_engine_level(tmp_path):
    dev, prod = tmp_path / "dev", tmp_path / "prod"
    dev.mkdir()
    prod.mkdir()
    tf = {"terraform_dir": [str(dev)]}
    deny = {"acme": {"terraform_dir": [str(prod)]}}
    d = _check(tmp_path, f"HOME={dev}; cd; terraform apply", tf, environ={"HOME": "/h"})
    assert d.action == "allow"
    d = _check(tmp_path, f"export HOME={prod}; cd ~; terraform apply", tf, deny=deny)
    assert d.action == "deny"
    d = _check(tmp_path, "HOME=$X; cd; terraform apply", tf)
    assert d.action == "ask"


def test_review_round1_asks(tmp_path):
    kc = {"kube_context": ["dev"]}

    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, "prod\n", "")

    for command in (
        "echo \"<<'X'\"\nkubectl delete pod p # --context dev",
        "cat <<'E'OF\ndata\nEOF\nkubectl delete pod p # --context dev",
        "X=\"$(cat <<'Q'Z\nhi\nQZ\nkubectl --context prod delete ns x)\"; echo",
        "nice -n >/dev/null 5 kubectl --context prod get po",
        "sudo -u >/dev/null root kubectl --context prod get po",
        "echo x | xargs kubectl --context dev delete ns",
        "parallel kubectl --context dev delete ns ::: a",
        r"find . -exec kubectl --context dev apply -f {} \;",
    ):
        assert _check(tmp_path, command, kc, run=run).action == "ask", command


def test_review_round2_asks(tmp_path):
    kc = {"kube_context": ["dev"]}
    c, p = "kubectl delete pod p # --context dev", "kubectl --context prod delete ns x"

    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, "prod\n", "")

    for command in (
        f"cat <<<'X'\n{c}\nX",
        f"X=\"$(cat <<<'Q'\n{p}\nQ\n)\"; echo",
        f"X=\"$(# <<'Q'\n{p}\nQ\n)\"; echo",
        f"echo '{p}' | xargs -0 sh -c",
        f"echo '{p}' | parallel",
        f"echo n | xargs --max-a 1 {p}",
        r"find /usr/local/bin -name kubectl -exec {} --context prod delete ns x \;",
    ):
        assert _check(tmp_path, command, kc, run=run).action == "ask", command


def test_review_round3_asks(tmp_path):
    kc = {"kube_context": ["dev"]}
    c = "kubectl delete pod p # --context dev"

    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, "prod\n", "")

    for command in (
        f"echo '$((' ; {c} '))'",
        f'X=$(({c}\n) ); echo "[$X]"',
        "echo kubectl delete ns x | xargs env",
        "echo /bin/kubectl | xargs -I% nice % --context prod delete ns x",
        r"find /bin -name kubectl -exec nice {} --context prod delete ns x \;",
        r"find /bin -name kubectl -exec true \; -exec env {} --context prod delete ns x \;",
        "echo kubectl delete ns x | parallel nice",
    ):
        assert _check(tmp_path, command, kc, run=run).action == "ask", command


def test_review_round4_asks(tmp_path):
    kc = {"kube_context": ["dev"]}
    for command in (
        "find /bin -name kubectl -exec ionice2 {} --context dev delete ns x \\;",
        "echo /bin/kubectl | xargs -I% mywrap % --context dev delete ns x",
        "chrt -f 10 kubectl --context prod get po",
        "taskset -c 0,2 kubectl --context prod get po",
    ):
        assert _check(tmp_path, command, kc).action == "ask", command


def test_placeholder_allow_list_at_engine_level(tmp_path):
    kc = {"kube_context": ["dev"]}

    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, "dev\n", "")

    for command in (
        "kubectl get pods -o name | xargs -I{} echo {}",
        "kubectl get pods -o name | xargs -I{} rm {}",
        "find . -name x -exec grep y {} + && kubectl get po",
    ):
        assert _check(tmp_path, command, kc, run=run).action == "allow", command
    for command in (
        "find /bin -name kubectl -exec ionice {} --context dev delete ns x \\;",
        "echo p | xargs -I{} kubectl delete pod {}",
        "echo kubectl | xargs -I{} awk {}",
        # a placeholder in a wrapper option can name the program (BSD -J fills in words)
        "echo 5 kubectl --context prod delete ns x | xargs -J% nice -n % cat",
        "echo X kubectl --context prod delete ns x | xargs -J% env -u % cat",
        "echo kubectl | parallel -I XX nice XX --context prod delete ns x",
        "echo $((echo '))<<\\Q '\\'\nkubectl delete pod p # --context dev\n) )\nQ",
    ):
        assert _check(tmp_path, command, kc, run=run).action == "ask", command


# --- path prefixes -----------------------------------------------------------


def test_path_prefix_stops_at_a_whole_folder(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "bc").mkdir(parents=True)
    allow = {"terraform_dir": [str(tmp_path / "a" / "b")]}
    d = _check(tmp_path, f"terraform -chdir={tmp_path}/a/bc plan", allow)
    assert d.action == "ask"
    assert d.not_allowed == [("terraform_dir", f"{tmp_path}/a/bc")]
    assert _check(tmp_path, f"terraform -chdir={tmp_path}/a/b plan", allow).action == "allow"
    deny = {"acme": {"terraform_dir": [str(tmp_path / "a" / "b")]}}
    wide = {"terraform_dir": [str(tmp_path)]}
    d = _check(tmp_path, f"terraform -chdir={tmp_path}/a/bc plan", wide, deny=deny)
    assert d.action == "allow"


def test_symlink_inside_an_allowed_folder_is_not_allowed(tmp_path):
    """A link under an allowed folder that leads elsewhere is checked where it leads."""
    root, other = tmp_path / "root", tmp_path / "other"
    root.mkdir()
    other.mkdir()
    (root / "link").symlink_to(other)
    allow = {"terraform_dir": [str(root)]}
    d = _check(tmp_path, f"terraform -chdir={root}/link plan", allow)
    assert d.action == "ask"
    d = check(
        "terraform plan",
        cwd=str(root / "link"),
        workspace_dir=str(tmp_path),
        environ={},
        config=_cfg(tmp_path, {"acme": allow}),
        run=_no_run,
        home="/home/x",
    )
    assert d.action == "ask"
    # a link that stays inside, and an allowed folder reached through its own link, allow
    (root / "sub").mkdir()
    (root / "inner").symlink_to(root / "sub")
    assert _check(tmp_path, f"terraform -chdir={root}/inner plan", allow).action == "allow"
    (tmp_path / "alias").symlink_to(root)
    alias = {"terraform_dir": [str(tmp_path / "alias")]}
    assert _check(tmp_path, f"terraform -chdir={tmp_path}/alias/sub plan", alias).action == "allow"


def test_injected_source_tilde_entries_use_the_given_home(tmp_path):
    class Source:
        def lists(self, ws):
            return {"terraform_dir": ["~/infra"]}, {"terraform_dir": ["~/infra/prod"]}

        def deny_parts(self, ws):
            return {"terraform_dir": ["~/infra/prod"]}, {}

        def protected(self, ws):
            return {}

        def others(self, ws):
            return {}

    (tmp_path / "infra" / "prod").mkdir(parents=True)
    (tmp_path / "infra" / "dev").mkdir(parents=True)

    def run(command):
        return check(
            command,
            cwd=str(tmp_path),
            workspace_dir=str(tmp_path),
            environ={},
            config=_cfg(tmp_path, {}),
            source=Source(),
            run=_no_run,
            home=str(tmp_path),
        )

    assert run(f"terraform -chdir={tmp_path}/infra/dev plan").action == "allow"
    d = run(f"terraform -chdir={tmp_path}/infra/prod plan && terraform -chdir=/elsewhere plan")
    assert d.action == "deny"
    assert d.not_allowed == [("terraform_dir", "/elsewhere")]


def test_deny_comes_from_deny_parts_only(tmp_path):
    """One source of truth for deny: a source whose merged ``lists`` deny lags behind its
    ``deny_parts`` still denies."""

    class Source:
        def lists(self, ws):
            return {"kube_context": ["prod", "stage"]}, {}

        def deny_parts(self, ws):
            return {"kube_context": ["prod"]}, {"kube_context": ["stage"]}

        def protected(self, ws):
            return {}

        def others(self, ws):
            return {}

    def run(command):
        return check(
            command,
            cwd=str(tmp_path),
            workspace_dir=str(tmp_path),
            environ={},
            config=_cfg(tmp_path, {}),
            source=Source(),
            run=_no_run,
            home="/home/x",
        )

    d = run("kubectl --context prod get po && kubectl --context stage get po")
    assert d.action == "deny"
    assert d.denied == [("kube_context", "prod", False), ("kube_context", "stage", True)]


@pytest.mark.parametrize(
    "form",
    [
        "terragrunt --working-dir {d} apply",
        "terragrunt --working-dir={d} apply",
        "terragrunt --terragrunt-working-dir {d} apply",
        "terragrunt run --all apply --working-dir {d}",
        "TG_WORKING_DIR={d} terragrunt apply",
        "TERRAGRUNT_WORKING_DIR={d} terragrunt apply",
    ],
)
def test_terragrunt_working_dir_is_the_target(tmp_path, form):
    # the folder terragrunt is pointed at is what it acts on, not the shell's folder
    allowed = {"terraform_dir": [str(tmp_path)]}
    other = tmp_path.parent / f"{tmp_path.name}-other"
    other.mkdir()
    d = _check(tmp_path, form.format(d=other), allowed)
    assert d.action == "ask", form
    assert d.not_allowed == [("terraform_dir", str(other))], form
    inside = tmp_path / "stack"
    inside.mkdir()
    assert _check(tmp_path, form.format(d=inside), allowed).action == "allow", form


def test_terragrunt_working_dir_from_the_environment(tmp_path):
    allowed = {"terraform_dir": [str(tmp_path)]}
    other = tmp_path.parent / f"{tmp_path.name}-env"
    other.mkdir()
    d = _check(tmp_path, "terragrunt apply", allowed, environ={"TG_WORKING_DIR": str(other)})
    assert d.action == "ask"


def test_terragrunt_unreadable_working_dir_cannot_tell(tmp_path):
    allowed = {"terraform_dir": [str(tmp_path)]}
    for command in ('terragrunt --working-dir "$D" apply', "terragrunt --working-dir= apply"):
        d = _check(tmp_path, command, allowed)
        assert d.action == "ask", command
        assert "terraform_dir" in d.unknown, command
