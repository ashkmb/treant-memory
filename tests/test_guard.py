import uuid

import pytest
from psycopg import sql

from treant import graph
from treant.guard import GuardEnv, analyse, guard_check, parse_targets, warnings_for

ENV = GuardEnv(
    "/work/repo",
    "ctx-active",
    "prof-active",
    {
        "origin": "git@git.example.com:grp/repo.git",
        "upstream": "https://GitHub.com:8443/org/repo.git",
        "sshurl": "ssh://deploy@Host.Example.org:2222/x/y.git",
    },
)


def _u(p: str) -> str:
    return f"{p}-{uuid.uuid4().hex[:8]}"


def _add_target(db, ws, kind, value):
    with db.ws_tx(ws) as c:
        c.execute(
            sql.SQL(
                "INSERT INTO {}.targets (kind, value) VALUES (%s, %s) ON CONFLICT DO NOTHING"
            ).format(graph.schema_of(ws)),
            (kind, value),
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
    ("git push", (True, [("git_host", "git.example.com")])),
    ("git push origin main", (True, [("git_host", "git.example.com")])),
    ("git push upstream main", (True, [("git_host", "github.com")])),
    ("git push --force sshurl", (True, [("git_host", "host.example.org")])),
    ("git -C /some/dir push", (True, [("git_host", "git.example.com")])),
    ("git push https://Other.example.net/a/b.git", (True, [("git_host", "other.example.net")])),
    ("git push nowhere", (True, [])),
    ("git pull", (False, [])),
    ("terraform plan", (True, [("tf_root", "/work/repo")])),
    ("terragrunt apply", (True, [("tf_root", "/work/repo")])),
    ("terraform -chdir=infra plan", (True, [("tf_root", "/work/repo/infra")])),
    ("terraform -chdir=../other plan", (True, [("tf_root", "/work/other")])),
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
    ("git push --signed origin main", (True, [("git_host", "git.example.com")])),
    ("timeout 60 kubectl --context prod get po", (True, [("kube_context", "prod")])),
    ("timeout -s KILL -k 5 60 kubectl get po", (True, [("kube_context", "ctx-active")])),
    ("timeout --signal=TERM 5m aws s3 ls", (True, [("aws_profile", "prof-active")])),
    ("sudo timeout 60 git push origin", (True, [("git_host", "git.example.com")])),
    # an escaped backslash before a newline is no continuation: the newline still separates
    ("echo \\\\\nkubectl --context prod get po", (True, [("kube_context", "prod")])),
    ("echo 'a\\\n';kubectl --context prod get po", (True, [("kube_context", "prod")])),
    ('echo "a\\\nb";kubectl --context prod get po', (True, [("kube_context", "prod")])),
    # options after `--` belong to the command kubectl runs, not to kubectl
    ("kubectl exec pod -- kubectl --context dev get po", (True, [("kube_context", "ctx-active")])),
    ("kubectl --context x exec pod -- sh --context=y", (True, [("kube_context", "x")])),
    ("ansible-playbook site.yml -- -i inv/x", (True, [])),
]


@pytest.mark.parametrize(("command", "expected"), CASES)
def test_parse_targets_cases(command, expected):
    assert parse_targets(command, ENV) == expected


def test_parse_targets_no_active_context():
    env = GuardEnv("/x", None, None, {})
    assert parse_targets("kubectl get po", env) == (True, [])
    assert parse_targets("aws s3 ls", env) == (True, [])


NO_ACTIVE = GuardEnv("/x", None, None, {"origin": "git@git.example.com:grp/repo.git"})

UNRESOLVED_CASES = [
    ("ls", (False, [], [])),
    ('echo "unbalanced', (True, [], ["command"])),
    ("kubectl get po", (True, [], ["kube_context"])),
    ("helm list", (True, [], ["kube_context"])),
    ("aws s3 ls", (True, [], ["aws_profile"])),
    ("git push nowhere", (True, [], ["git_host"])),
    ("ansible-playbook site.yml", (True, [], ["ansible_inventory"])),
    ("kubectl --context x get po", (True, [("kube_context", "x")], [])),
    ("git push origin", (True, [("git_host", "git.example.com")], [])),
    ("terraform plan", (True, [("tf_root", "/x")], [])),
    (
        "kubectl --context x get po; kubectl get no && aws s3 ls; aws s3 ls",
        (True, [("kube_context", "x")], ["kube_context", "aws_profile"]),
    ),
]


# State a same-line command changes, and commands run by other commands, never fall back to
# the client's environment: they are read from the line or reported unresolved.
STATE_CASES = [
    ("cd /prod && terraform apply", (True, [], ["tf_root"])),
    ("cd /prod && terraform -chdir=/infra apply", (True, [("tf_root", "/infra")], [])),
    ("pushd /prod; terragrunt apply", (True, [], ["tf_root"])),
    ("export AWS_PROFILE=prod; aws s3 rm s3://b/k", (True, [], ["aws_profile"])),
    ("AWS_PROFILE=prod; aws s3 ls", (True, [], ["aws_profile"])),
    ("export AWS_PROFILE=prod; aws --profile dev s3 ls", (True, [("aws_profile", "dev")], [])),
    ("export FOO=1; aws s3 ls", (True, [("aws_profile", "prof-active")], [])),
    ("export KUBECONFIG=/k; kubectl get po", (True, [], ["kube_context"])),
    (
        "kubectl config use-context prod && kubectl delete ns x",
        (True, [("kube_context", "ctx-active")], ["kube_context"]),
    ),
    ("kubectx prod; kubectl delete ns x", (True, [], ["kube_context"])),
    ("source env.sh; aws s3 ls", (True, [], ["aws_profile"])),
    ("set -euo pipefail; aws s3 ls", (True, [("aws_profile", "prof-active")], [])),
    ("set -a; . ./x; set +a; aws s3 ls", (True, [], ["aws_profile"])),
    ("set -o allexport; aws s3 ls", (True, [], ["aws_profile"])),
    (". ./env.sh && kubectl get po", (True, [], ["kube_context"])),
    ("git remote set-url origin x@evil:y; git push", (True, [], ["git_host"])),
    ("bash -c 'kubectl --context prod delete ns x'", (True, [("kube_context", "prod")], [])),
    ('sh -ec "aws --profile p s3 ls"', (True, [("aws_profile", "p")], [])),
    ("bash -c 'cd /x && terraform apply'", (True, [], ["tf_root"])),
    ("eval kubectl --context prod get po", (True, [("kube_context", "prod")], [])),
    ("echo `kubectl --context prod get po`", (True, [("kube_context", "prod")], [])),
    ('echo "$(kubectl --context prod get po)"', (True, [], ["command"])),
    ('echo "`aws s3 ls`"', (True, [], ["command"])),
    ('git commit -m "fix $(date)"', (False, [], [])),
    ("xargs kubectl delete pod", (True, [], ["command"])),
    ("find . -name x -exec terraform apply \\;", (True, [], ["command"])),
    ("watch kubectl get po", (True, [], ["command"])),
    ("xargs echo", (False, [], [])),
    ("terragrunt --working-dir infra apply", (True, [("tf_root", "/x/infra")], [])),
    ("terragrunt --terragrunt-working-dir=/abs apply", (True, [("tf_root", "/abs")], [])),
]

STATE_ENV = GuardEnv(
    "/x",
    "ctx-active",
    "prof-active",
    {"origin": "git@git.example.com:grp/repo.git"},
)


@pytest.mark.parametrize(("command", "expected"), STATE_CASES)
def test_analyse_same_line_state_and_nested_commands(command, expected):
    assert analyse(command, STATE_ENV) == expected


@pytest.mark.parametrize(("command", "expected"), UNRESOLVED_CASES)
def test_analyse_reports_unresolved_kinds(command, expected):
    assert analyse(command, NO_ACTIVE) == expected
    assert parse_targets(command, NO_ACTIVE) == expected[:2]


def test_guard_check_reports_unresolved(db, ctx_a):
    env = GuardEnv("/x", None, None, {})
    assert guard_check(db, ctx_a, "kubectl get po", env) == {
        "risky": True,
        "unlisted": [],
        "warnings": [],
        "unresolved": ["kube_context"],
    }
    assert guard_check(db, ctx_a, 'kubectl "unbalanced', env)["unresolved"] == ["command"]
    assert guard_check(db, ctx_a, "git push somewhere", env)["unresolved"] == ["git_host"]
    assert guard_check(db, ctx_a, "ansible-playbook x.yml", env)["unresolved"] == [
        "ansible_inventory"
    ]


def test_guard_unknown_target_reveals_nothing(db, ctx_a):
    # "prod-eks" is a listed kube_context in ws beta only
    _add_target(db, "beta", "kube_context", "prod-eks")
    out = guard_check(
        db, ctx_a, "kubectl --context prod-eks get pods", GuardEnv("/x", None, None, {})
    )
    assert out == {
        "risky": True,
        "unlisted": [{"kind": "kube_context", "value": "prod-eks"}],
        "warnings": [],
        "unresolved": [],
    }


def test_listed_target_is_not_reported(db, ctx_a):
    name = _u("ctx")
    _add_target(db, "alpha", "kube_context", name)
    out = guard_check(db, ctx_a, f"kubectl --context {name} get po", GuardEnv("/x", None, None, {}))
    assert out == {"risky": True, "unlisted": [], "warnings": [], "unresolved": []}


def test_not_risky_has_no_lookup_output(db, ctx_a):
    out = guard_check(db, ctx_a, "ls -la", GuardEnv("/x", None, None, {}))
    assert out == {"risky": False, "unlisted": [], "warnings": [], "unresolved": []}


def test_tf_root_prefix_match(db, ctx_a):
    root = f"/srv/{_u('infra')}"
    _add_target(db, "alpha", "tf_root", root)

    def run(cwd, cmd="terraform plan"):
        return guard_check(db, ctx_a, cmd, GuardEnv(cwd, None, None, {}))["unlisted"]

    assert run(root) == []
    assert run(root + "/") == []
    assert run(root + "/stacks/prod") == []
    assert run(root + "x") == [{"kind": "tf_root", "value": root + "x"}]
    assert run(root + "/../other") == [{"kind": "tf_root", "value": root + "/../other"}]
    assert run("/srv", "terraform -chdir=" + root.split("/")[-1] + " plan") == []


def test_warning_on_named_node(db, ctx_a):
    name = _u("prodcluster")
    alias = _u("alias")
    with db.ws_tx("alpha") as c:
        n = graph.add_node(c, "alpha", "instance", "cluster", name, aliases=[alias])
        graph.add_fact(c, "alpha", "warning", "never drain at noon", node_id=n)
        graph.add_fact(c, "alpha", "warning", "x" * 500, node_id=n)
        graph.add_fact(c, "alpha", "lesson", "not a warning", node_id=n)
        retracted = graph.add_fact(c, "alpha", "warning", "retracted one", node_id=n)
        graph.retract(c, "alpha", "fact", retracted)
    with db.ws_tx("beta") as c:
        m = graph.add_node(c, "beta", "instance", "cluster", name)
        graph.add_fact(c, "beta", "warning", "beta secret warning", node_id=m)
    env = GuardEnv("/x", None, None, {})
    for cmd in (
        f"kubectl --context ZZ get po -n {name.upper()}",
        f"kubectl get po --selector=app:{alias}",
        f"kubectl get po/{name}",
    ):
        out = guard_check(db, ctx_a, cmd, env)
        assert sorted(len(w) for w in out["warnings"]) == [19, 300], cmd
        assert "never drain at noon" in out["warnings"]
        assert all("beta" not in w and "retracted" not in w for w in out["warnings"])
    out = guard_check(db, ctx_a, f"kubectl get po {name}x", env)
    assert out["warnings"] == []


def test_warnings_for_matches_names_and_caps(db, ctx_a):
    name = _u("wf")
    alias = _u("wfalias")
    with db.ws_tx("alpha") as c:
        n = graph.add_node(c, "alpha", "instance", "svc", name, aliases=[alias])
        for i in range(8):
            graph.add_fact(c, "alpha", "warning", f"w{i}" + "x" * 400, node_id=n)
        graph.add_fact(c, "alpha", "lesson", "not a warning", node_id=n)
    with db.ws_tx("beta") as c:
        m = graph.add_node(c, "beta", "instance", "svc", name)
        graph.add_fact(c, "beta", "warning", "beta secret", node_id=m)
    assert warnings_for(db, ctx_a, []) == []
    assert warnings_for(db, ctx_a, ["nothing-" + name]) == []
    out = warnings_for(db, ctx_a, [name.upper(), name, alias])
    assert len(out) == 5 and all(len(w) == 300 for w in out)
    assert warnings_for(db, ctx_a, [alias]) == out


def test_warnings_capped_at_five(db, ctx_a):
    name = _u("many")
    with db.ws_tx("alpha") as c:
        n = graph.add_node(c, "alpha", "instance", "svc", name)
        for i in range(8):
            graph.add_fact(c, "alpha", "warning", f"w{i}", node_id=n)
    out = guard_check(db, ctx_a, f"kubectl get {name}", GuardEnv("/x", None, None, {}))
    assert len(out["warnings"]) == 5
    assert out == guard_check(db, ctx_a, f"kubectl get {name}", GuardEnv("/x", None, None, {}))


def test_active_kube_context_used_when_not_named(db, ctx_a):
    out = guard_check(db, ctx_a, "kubectl get pods", GuardEnv("/x", _u("active"), None, {}))
    assert [u["kind"] for u in out["unlisted"]] == ["kube_context"]
    out = guard_check(db, ctx_a, "kubectl get pods", GuardEnv("/x", None, None, {}))
    assert out["risky"] is True and out["unlisted"] == []
    assert out["unresolved"] == ["kube_context"]


ANSIBLE_CFG_ENV = GuardEnv("/x", None, None, {}, "inventory/hosts.yml")

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
    assert analyse(command, ANSIBLE_CFG_ENV) == expected


def test_ansible_inventory_default_comma_list_and_blank():
    env = GuardEnv("/x", None, None, {}, " a , ,b ")
    assert analyse("ansible-playbook s.yml", env) == (
        True,
        [("ansible_inventory", "a"), ("ansible_inventory", "b")],
        [],
    )
    blank = GuardEnv("/x", None, None, {}, " , ")
    assert analyse("ansible-playbook s.yml", blank) == (True, [], ["ansible_inventory"])


def test_guard_check_listed_ansible_default_is_clear(db, ctx_a):
    inv = _u("inventory/hosts")
    _add_target(db, ctx_a.workspace, "ansible_inventory", inv)
    out = guard_check(db, ctx_a, "ansible-playbook site.yml", GuardEnv("/x", None, None, {}, inv))
    assert out["unlisted"] == [] and out["unresolved"] == []


def test_guard_check_with_nul_byte_does_not_raise(db, ctx_a):
    env = GuardEnv("/x", None, None, {})
    for cmd in ("kubectl get\x00 po", "kubectl get po 'unbalanced\x00"):
        out = guard_check(db, ctx_a, cmd, env)
        assert out["risky"] is True


def test_guard_check_reads_warnings_past_the_name_cap(db, ctx_a):
    name = _u("zzcap")
    with db.ws_tx("alpha") as c:
        n = graph.add_node(c, "alpha", "instance", "svc", name)
        graph.add_fact(c, "alpha", "warning", "cap warning", node_id=n)
    filler = " ".join(f"a{i:04d}" for i in range(300))  # sorts before the node name
    out = guard_check(db, ctx_a, f"kubectl get po {filler} {name}", GuardEnv("/x", None, None, {}))
    assert out["warnings"] == ["cap warning"]


def test_warnings_for_cap_dedupe_and_non_strings(db, ctx_a):
    name = _u("wcap")
    with db.ws_tx("alpha") as c:
        n = graph.add_node(c, "alpha", "instance", "svc", name)
        graph.add_fact(c, "alpha", "warning", "capped", node_id=n)
    filler = [f"b{i:04d}" for i in range(200)]
    assert warnings_for(db, ctx_a, [*filler, name]) == []  # the 201st name is not read
    assert warnings_for(db, ctx_a, [name, *filler]) == ["capped"]
    # the cap counts input entries, duplicates included; repeats of a hit return it once
    assert warnings_for(db, ctx_a, [*["dup"] * 200, name]) == []
    assert warnings_for(db, ctx_a, [name, name.upper(), name]) == ["capped"]
    assert warnings_for(db, ctx_a, [None, 7, "", {"x": 1}, name.upper()]) == ["capped"]
