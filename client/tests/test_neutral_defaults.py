"""With no url / kiro_agent configured the client keeps working locally and never goes online."""

import io
import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from treant_client import installer
from treant_client.hooks_claude import pre_tool_use, session_start
from treant_client.kiro import kiro_guard, kiro_start
from treant_client.proxy import run_proxy
from treant_client.save import save_worker

LISTED = "kubectl get pods"
RETRY = " Re-run the same command to proceed."


def _boom(*a, **k):
    raise AssertionError("must not be called")


def _no_git(argv, **kw):
    return subprocess.CompletedProcess(argv, 1, "", "")


class _Factory:
    """Session factory that records every construction; swallowed errors can't hide a call."""

    def __init__(self):
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        raise AssertionError("session factory must not be built without a url")


def _tok(ws):
    return "tok-test"


@pytest.fixture
def offline(cfg_tmp):
    return replace(cfg_tmp, url=None)


def _work(cfg):
    return next(p for p, w in cfg.folders.items() if w == "work")


def test_no_url_means_memory_off_session_start(offline):
    out = session_start(
        {"cwd": _work(offline)},
        offline,
        token_getter=_tok,
        session_factory=_boom,
        run=_no_git,
    )
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("Workspace: work (folder ")
    assert "Treant memory is on" not in ctx


def test_no_url_means_memory_off_pre_tool_use(offline, guard_cfg):
    payload = {
        "tool_name": "Bash",
        "cwd": _work(offline),
        "tool_input": {"command": "kubectl --context prod get pods"},
    }
    factory = _Factory()
    out = pre_tool_use(
        payload,
        offline,
        guard_config_loader=lambda: guard_cfg,
        token_getter=_tok,
        session_factory=factory,
        run=_no_git,
    )
    assert factory.calls == 0
    h = json.loads(out)["hookSpecificOutput"]
    assert h["permissionDecision"] == "ask"
    assert "warnings" not in h["permissionDecisionReason"]  # the guard decided locally


def test_no_url_means_memory_off_kiro(offline, guard_cfg):
    note = kiro_start(
        {}, _work(offline), offline, token_getter=_tok, session_factory=_boom, run=_no_git
    )
    assert note.startswith("Workspace: work (folder ")
    payload = {"tool_name": "execute_bash", "tool_input": {"command": LISTED}}
    factory = _Factory()
    code, err = kiro_guard(
        payload,
        _work(offline),
        1000.0,
        offline,
        guard_config_loader=lambda: guard_cfg,
        token_getter=_tok,
        session_factory=factory,
        run=_no_git,
    )
    assert factory.calls == 0
    assert (code, err) == (
        2,
        "Treant guard (work): can't tell which kube_context this uses" + RETRY,
    )


def test_no_url_means_memory_off_save(offline):
    status = save_worker(
        "sid-1",
        _work(offline),
        lambda: ("x" * 5000, 5),
        "session_end",
        offline,
        token_getter=_tok,
        session_factory=_boom,
        summarizer=_boom,
    )
    assert status == "skipped_no_url"


def test_proxy_without_url(offline, monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "search"}},
    ]
    out = io.StringIO()
    inp = io.StringIO("".join(json.dumps(m) + "\n" for m in msgs))
    rc = run_proxy(inp, out, _work(offline), offline, token_getter=_tok, session_factory=_boom)
    replies = [json.loads(x) for x in out.getvalue().splitlines()]
    assert rc == 0
    assert replies[0]["result"]["serverInfo"]["version"] == "off"
    assert replies[1]["result"] == {"tools": []}
    call = replies[2]
    assert call["id"] == 3
    assert call["result"]["isError"] is True
    assert call["result"]["content"][0]["text"] == (
        "Treant: no url configured in ~/.config/treant/config.toml"
    )


class _Runner:
    def __call__(self, cmd, **kw):
        rc = 1 if cmd[:3] == ["claude", "mcp", "get"] or cmd[:2] == ["launchctl", "print"] else 0
        return subprocess.CompletedProcess(cmd, rc, "", "")


def test_install_without_kiro_agent_skips_kiro(tmp_path, offline):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".kiro" / "agents").mkdir(parents=True)
    agent = home / ".kiro" / "agents" / "other.json"
    agent.write_text('{"name": "other"}')
    cfg = replace(offline, kiro_agent=None)
    out: list[str] = []
    kw = {
        "binary": "/opt/bin/treant-client",
        "home": home,
        "cfg": cfg,
        "run": _Runner(),
        "which": lambda n: "/opt/homebrew/bin/claude" if n == "claude" else None,
        "out": out.append,
        "uid": 501,
        "now": lambda: datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC),
    }
    assert installer.install(**kw) == 0
    assert "skipped Kiro: set kiro_agent in config.toml\n" in out
    assert agent.read_text() == '{"name": "other"}'
    assert list(agent.parent.glob("*bak*")) == []
    assert not (agent.parent / "None.json").exists()
    kw.pop("binary"), kw.pop("which")
    out.clear()
    assert installer.uninstall(**kw) == 0
    assert agent.read_text() == '{"name": "other"}'
    assert agent.exists()
    assert not (agent.parent / "None.json").exists()


def test_guard_command_tolerates_missing_config_toml(tmp_path, monkeypatch, guard_cfg):
    """cfg=None takes the real load_config path; defaults point at an empty tmp dir, not the
    real home."""
    from treant_client import config as cc
    from treant_client.hooks_claude import guard_command

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        cc.load_config, "__defaults__", (tmp_path / "none.toml", tmp_path / "no-guard")
    )
    work = next(p for p, w in guard_cfg.workspaces.items() if w == "work")
    factory = _Factory()
    action, reason = guard_command(
        "kubectl get pods",
        cwd=work,
        project=work,
        cfg=None,
        event="guard_hook",
        guard_config_loader=lambda: guard_cfg,
        token_getter=_tok,
        session_factory=factory,
        run=_no_git,
        budget=3.0,
        http_timeout=1.0,
    )
    assert action == "ask" and reason
    assert factory.calls == 0
