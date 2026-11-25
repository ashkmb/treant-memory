"""Off mode: the guard steps aside for that workspace; nothing asks and nothing is denied.

Other workspaces keep their own mode, and a workspace set to off is still a workspace (its
folders keep their Treant memory scope).
"""

import subprocess

import pytest

from treant_guard.config import MODES, GuardConfig, load_config
from treant_guard.engine import check
from treant_guard.rules import default_rules


def _no_run(argv, **kw):
    return subprocess.CompletedProcess(argv, 1, "", "")


def _ctx(name):
    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, f"{name}\n", "")

    return run


PROTECT = {"*": {"kube_context": ["prod"], "aws_profile": ["live"]}}
ALLOW = {"acme": {"kube_context": ["pre-prod"], "aws_profile": ["acme-ops"]}}
OFF = {"personal": "off"}


def _check(tmp_path, command, run=_no_run, *, modes=OFF, folder="p", deny=None):
    p, c = tmp_path / "p", tmp_path / "c"
    p.mkdir(exist_ok=True)
    c.mkdir(exist_ok=True)
    cfg = GuardConfig(
        rules=default_rules(),
        workspaces={str(p): "personal", str(c): "acme"},
        allow=ALLOW,
        deny=deny or {},
        about={},
        protect=PROTECT,
        mode="protect",
        modes=modes,
    )
    where = str(tmp_path / folder)
    return check(
        command, cwd=where, workspace_dir=where, environ={}, config=cfg, run=run, home="/h"
    )


def test_off_is_a_mode():
    assert "off" in MODES


@pytest.mark.parametrize(
    "command,run",
    [
        ("kubectl --context prod delete ns x", _no_run),
        ("kubectl delete ns x", _ctx("prod")),
        ("aws --profile live s3 rm s3://b --recursive", _no_run),
        ("kubectl get po", _ctx("pre-prod")),  # acme's own target
        ("kubectx prod", _no_run),
        ("kubectl get po $(echo x)", _no_run),  # can't tell
    ],
)
def test_everything_runs(tmp_path, command, run):
    d = _check(tmp_path, command, run)
    assert (d.action, d.reason, d.workspace) == ("allow", None, "personal")


def test_deny_does_not_apply(tmp_path):
    deny = {"personal": {"kube_context": ["prod"]}, "*": {"aws_profile": ["live"]}}
    assert _check(tmp_path, "kubectl --context prod get po", deny=deny).action == "allow"
    assert _check(tmp_path, "aws --profile live s3 ls", deny=deny).action == "allow"


def test_other_workspaces_keep_their_mode(tmp_path):
    d = _check(tmp_path, "kubectl --context prod get po", folder="c")
    assert d.action == "ask" and "protected" in d.reason


def test_config_accepts_off_for_a_workspace(tmp_path):
    d = tmp_path / "treant"
    d.mkdir(mode=0o700)
    f = d / "guard.toml"
    f.write_text('[guard]\nmode = "protect"\n[guard.modes]\npersonal = "off"\n')
    f.chmod(0o600)
    cfg = load_config(d)
    assert cfg.error is None
    assert cfg.modes == {"personal": "off"}
