"""Production mode: ask only for protected (production) targets; deny still refuses.

A workspace set to it no longer asks about targets another workspace's allow list names.
"""

import subprocess

from treant_guard.config import GuardConfig
from treant_guard.engine import check
from treant_guard.rules import default_rules


def _no_run(argv, **kw):
    return subprocess.CompletedProcess(argv, 1, "", "")


def _ctx(name):
    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, f"{name}\n", "")

    return run


PROTECT = {"*": {"kube_context": ["prod"], "aws_profile": ["default", "live"]}}
ALLOW = {
    "startup": {"kube_context": ["startup-ops"]},
    "acme": {"kube_context": ["pre-prod"], "aws_profile": ["acme-ops"]},
}


def _check(tmp_path, command, run=_no_run, *, mode="protect", modes=None, folder="t", **kw):
    t, c = tmp_path / "t", tmp_path / "c"
    t.mkdir(exist_ok=True)
    c.mkdir(exist_ok=True)
    cfg = GuardConfig(
        rules=default_rules(),
        workspaces={str(t): "startup", str(c): "acme"},
        allow=ALLOW,
        deny=kw.get("deny", {}),
        about={},
        protect=PROTECT,
        mode=mode,
        modes=modes or {},
    )
    where = str(tmp_path / folder)
    return check(
        command, cwd=where, workspace_dir=where, environ={}, config=cfg, run=run, home="/h"
    )


STARTUP_PROD_ONLY = {"startup": "production"}


def test_other_workspace_target_runs(tmp_path):
    d = _check(tmp_path, "kubectl get po", _ctx("pre-prod"), modes=STARTUP_PROD_ONLY)
    assert (d.action, d.reason, d.foreign) == ("allow", None, [])
    d = _check(tmp_path, "aws --profile acme-ops s3 ls", modes=STARTUP_PROD_ONLY)
    assert d.action == "allow"


def test_protected_targets_still_ask(tmp_path):
    for command, run in (
        ("kubectl --context prod get po", _no_run),
        ("kubectl get po", _ctx("prod")),
        ("aws --profile live s3 ls", _no_run),
        ("kubectx prod", _no_run),
    ):
        d = _check(tmp_path, command, run, modes=STARTUP_PROD_ONLY)
        assert d.action == "ask", command
        assert "protected" in d.reason, command


def test_deny_still_refuses(tmp_path):
    deny = {"startup": {"kube_context": ["pre-prod"]}}
    d = _check(tmp_path, "kubectl get po", _ctx("pre-prod"), modes=STARTUP_PROD_ONLY, deny=deny)
    assert d.action == "deny"


def test_override_applies_to_that_workspace_only(tmp_path):
    # acme keeps protect mode: startup's own target used from acme still asks
    d = _check(
        tmp_path, "kubectl --context startup-ops get po", modes=STARTUP_PROD_ONLY, folder="c"
    )
    assert d.action == "ask" and "belongs to startup" in d.reason
    # and without the override startup asks about acme's target as before
    d = _check(tmp_path, "kubectl get po", _ctx("pre-prod"))
    assert d.action == "ask" and "belongs to acme" in d.reason


def test_global_production_mode(tmp_path):
    d = _check(tmp_path, "kubectl get po", _ctx("pre-prod"), mode="production")
    assert d.action == "allow"
    d = _check(tmp_path, "kubectl get po", _ctx("prod"), mode="production")
    assert d.action == "ask"
