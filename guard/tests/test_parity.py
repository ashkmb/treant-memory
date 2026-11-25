"""Parity with the server guard: the tables of tests/test_guard.py as of 965f973."""

import os
import subprocess

import pytest

from treant_guard.resolve import targets_for
from treant_guard.rules import default_rules

RULES = default_rules()
NO_HOME = "/nonexistent-home"


def _env(cwd, kube_context, aws_profile, remotes, ansible_inventory=None):
    """The old GuardEnv as (cwd, environ, run) fakes."""
    environ = {}
    if aws_profile:
        environ["AWS_PROFILE"] = aws_profile
    if ansible_inventory is not None:
        environ["ANSIBLE_INVENTORY"] = ansible_inventory

    def run(argv, **kw):
        if argv == ["kubectl", "config", "current-context"]:
            if kube_context is None:
                return subprocess.CompletedProcess(argv, 1, "", "error: current-context not set")
            return subprocess.CompletedProcess(argv, 0, kube_context + "\n", "")
        if argv[:2] == ["git", "-C"] and argv[3:] == ["remote", "-v"]:
            out = "".join(
                f"{name}\t{url} (fetch)\n{name}\t{url} (push)\n" for name, url in remotes.items()
            )
            return subprocess.CompletedProcess(argv, 0, out, "")
        raise AssertionError(f"unexpected run {argv}")

    return cwd, environ, run


def _check(command, env):
    cwd, environ, run = env
    return targets_for(command, cwd=cwd, environ=environ, rules=RULES, run=run, home=NO_HOME)


ENV = _env(
    "/work/repo",
    "ctx-active",
    "prof-active",
    {
        "origin": "git@git.example.com:grp/repo.git",
        "upstream": "https://GitHub.com:8443/org/repo.git",
        "sshurl": "ssh://deploy@Host.Example.org:2222/x/y.git",
    },
)

CASES = [
    ("ls", (False, [])),
    ("git status", (False, [])),
    ("kubectl get pods", (True, [("kube_context", "ctx-active")])),
    ("kubectl --context=x get po", (True, [("kube_context", "x")])),
    ("kubectl --context x get po", (True, [("kube_context", "x")])),
    ("helm --kube-context y list", (True, [("kube_context", "y")])),
    ("helm --kube-context=y list", (True, [("kube_context", "y")])),
    ("helm list", (True, [("kube_context", "ctx-active")])),
    ("aws --profile p s3 ls", (True, [("aws_profile", "p")])),
    ("aws --profile=p s3 ls", (True, [("aws_profile", "p")])),
    ("aws s3 ls", (True, [("aws_profile", "prof-active")])),
    ("AWS_PROFILE=q aws s3 ls", (True, [("aws_profile", "q")])),
    ("env AWS_PROFILE=q aws s3 ls", (True, [("aws_profile", "q")])),
    ("FOO=bar kubectl get po", (True, [("kube_context", "ctx-active")])),
    ("sudo kubectl --context s get po", (True, [("kube_context", "s")])),
    ("/usr/local/bin/kubectl get po", (True, [("kube_context", "ctx-active")])),
    ("git push", (True, [("git_remote_host", "git.example.com")])),
    ("git push origin main", (True, [("git_remote_host", "git.example.com")])),
    ("git push upstream main", (True, [("git_remote_host", "github.com")])),
    ("git push --force sshurl", (True, [("git_remote_host", "host.example.org")])),
    ("git -C /some/dir push", (True, [("git_remote_host", "git.example.com")])),
    (
        "git push https://Other.example.net/a/b.git",
        (True, [("git_remote_host", "other.example.net")]),
    ),
    ("git push nowhere", (True, [])),
    ("git pull", (False, [])),
    ("terraform plan", (True, [("terraform_dir", "/work/repo")])),
    ("terragrunt apply", (True, [("terraform_dir", "/work/repo")])),
    ("terraform -chdir=infra plan", (True, [("terraform_dir", "/work/repo/infra")])),
    ("terraform -chdir=../other plan", (True, [("terraform_dir", "/work/other")])),
    (
        "ansible-playbook -i inv/prod site.yml --inventory=inv/stage",
        (True, [("ansible_inventory", "inv/prod"), ("ansible_inventory", "inv/stage")]),
    ),
    ("ansible-playbook -iinv/x site.yml", (True, [("ansible_inventory", "inv/x")])),
    (
        "echo hi && kubectl --context z apply -f x",
        (True, [("kube_context", "z")]),
    ),
    (
        "kubectl --context z get po | grep x; aws --profile p s3 ls || true",
        (True, [("kube_context", "z"), ("aws_profile", "p")]),
    ),
    ("kubectl --context z get po && kubectl --context z get no", (True, [("kube_context", "z")])),
    ('echo "a && b"', (False, [])),
    ('echo "kubectl get po"', (False, [])),
    ("echo a;(kubectl --context prod get po)", (True, [("kube_context", "prod")])),
    ("echo a;\nkubectl --context prod get po", (True, [("kube_context", "prod")])),
    ("echo a &&\nkubectl --context prod get po", (True, [("kube_context", "prod")])),
    ("echo a\nkubectl --context prod get po", (True, [("kube_context", "prod")])),
    ("echo $(kubectl --context prod get po)", (True, [("kube_context", "prod")])),
    ("kubectl get po \\\n --context prod", (True, [("kube_context", "prod")])),
    ("kubectl get po 2>&1 --context prod", (True, [("kube_context", "prod")])),
    ('echo \'a;b\' "c|d" "(e)" kubectl', (False, [])),
    ("kubectl get po -l 'a;b' --context prod", (True, [("kube_context", "prod")])),
    ("echo a\\;kubectl --context prod", (False, [])),
    ('echo "unbalanced', (True, [])),
    ("git push --signed origin main", (True, [("git_remote_host", "git.example.com")])),
    ("timeout 60 kubectl --context prod get po", (True, [("kube_context", "prod")])),
    ("timeout -s KILL -k 5 60 kubectl get po", (True, [("kube_context", "ctx-active")])),
    ("timeout --signal=TERM 5m aws s3 ls", (True, [("aws_profile", "prof-active")])),
    ("sudo timeout 60 git push origin", (True, [("git_remote_host", "git.example.com")])),
]


@pytest.mark.parametrize(("command", "expected"), CASES)
def test_parse_targets_cases(command, expected):
    assert _check(command, ENV)[:2] == expected


def test_parse_targets_no_active_context():
    env = _env("/x", None, None, {})
    assert _check("kubectl get po", env)[:2] == (True, [])
    assert _check("aws s3 ls", env)[:2] == (True, [])


NO_ACTIVE = _env("/x", None, None, {"origin": "git@git.example.com:grp/repo.git"})

UNRESOLVED_CASES = [
    ("ls", (False, [], [])),
    ('echo "unbalanced', (True, [], ["command"])),
    ("kubectl get po", (True, [], ["kube_context"])),
    ("helm list", (True, [], ["kube_context"])),
    ("aws s3 ls", (True, [], ["aws_profile"])),
    ("git push nowhere", (True, [], ["git_remote_host"])),
    ("ansible-playbook site.yml", (True, [], ["ansible_inventory"])),
    ("kubectl --context x get po", (True, [("kube_context", "x")], [])),
    ("git push origin", (True, [("git_remote_host", "git.example.com")], [])),
    ("terraform plan", (True, [("terraform_dir", "/x")], [])),
    (
        "kubectl --context x get po; kubectl get no && aws s3 ls; aws s3 ls",
        (True, [("kube_context", "x")], ["kube_context", "aws_profile"]),
    ),
]


@pytest.mark.parametrize(("command", "expected"), UNRESOLVED_CASES)
def test_analyse_reports_unresolved_kinds(command, expected):
    assert _check(command, NO_ACTIVE) == expected


ANSIBLE_CFG_ENV = _env("/x", None, None, {}, "inventory/hosts.yml")

ANSIBLE_DEFAULT_CASES = [
    ("ansible-playbook site.yml", (True, [("ansible_inventory", "inventory/hosts.yml")], [])),
    ("ansible-playbook -i inv/x site.yml", (True, [("ansible_inventory", "inv/x")], [])),
    (
        "ANSIBLE_INVENTORY=inv/a,inv/b ansible-playbook site.yml",
        (True, [("ansible_inventory", "inv/a"), ("ansible_inventory", "inv/b")], []),
    ),
]


@pytest.mark.parametrize("command,expected", ANSIBLE_DEFAULT_CASES)
def test_ansible_inventory_falls_back_to_client_default(command, expected):
    assert _check(command, ANSIBLE_CFG_ENV) == expected


def test_ansible_inventory_default_comma_list_and_blank():
    env = _env("/x", None, None, {}, " a , ,b ")
    assert _check("ansible-playbook s.yml", env) == (
        True,
        [("ansible_inventory", "a"), ("ansible_inventory", "b")],
        [],
    )
    blank = _env("/x", None, None, {}, " , ")
    assert _check("ansible-playbook s.yml", blank) == (True, [], ["ansible_inventory"])


def test_host_ansible_cfg_is_hidden_from_the_tests():
    assert not os.path.isfile("/etc/ansible/ansible.cfg")
    assert not os.path.isfile(os.path.expanduser("~/.ansible.cfg"))
