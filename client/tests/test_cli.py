import io
import json
import os
import subprocess
import sys

from treant_client import cli, hooks_claude, keychain, kiro, save


def _stdin(monkeypatch, text):
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


def test_cli_hook_always_exit_zero_on_bad_stdin(monkeypatch, capsys):
    for name in cli.CLAUDE_HOOKS:
        for bad in ("", "not json", "[1,2]", "null", '"s"'):
            _stdin(monkeypatch, bad)
            assert cli.main(["hook", name]) == 0
    for name in cli.KIRO_HOOKS:
        _stdin(monkeypatch, "garbage")
        assert cli.main(["hook", name]) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_hook_fails_open_when_handler_raises(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(hooks_claude, "session_start", boom)
    monkeypatch.setattr(kiro, "kiro_start", boom)
    _stdin(monkeypatch, "{}")
    assert cli.main(["hook", "claude-session-start"]) == 0
    _stdin(monkeypatch, "{}")
    assert cli.main(["hook", "kiro-start"]) == 0
    assert capsys.readouterr().out == ""


def test_hook_prints_output(monkeypatch, capsys):
    monkeypatch.setattr(hooks_claude, "pre_tool_use", lambda p: json.dumps({"seen": p["a"]}))
    _stdin(monkeypatch, '{"a": 1}')
    assert cli.main(["hook", "claude-pre-tool-use"]) == 0
    assert json.loads(capsys.readouterr().out) == {"seen": 1}


def test_kiro_guard_exit_code_and_stderr(monkeypatch, capsys):
    seen = {}

    def fake(payload, cwd, now):
        seen["cwd"] = cwd
        return 2, "blocked"

    monkeypatch.setattr(kiro, "kiro_guard", fake)
    _stdin(monkeypatch, '{"cwd": "/x/y"}')
    assert cli.main(["hook", "kiro-guard"]) == 2
    assert capsys.readouterr().err == "blocked" and seen["cwd"] == "/x/y"


def _guard_payload(command, tool="Bash"):
    return json.dumps({"cwd": "/x/y", "tool_name": tool, "tool_input": {"command": command}})


def _assert_fails_closed(monkeypatch, capsys, tmp_path):
    _stdin(monkeypatch, _guard_payload("kubectl get po"))
    assert cli.main(["hook", "claude-pre-tool-use"]) == 0
    h = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert h == {
        "hookEventName": "PreToolUse",
        "permissionDecision": "ask",
        "permissionDecisionReason": "Treant guard: guard error",
    }
    _stdin(monkeypatch, _guard_payload("kubectl get po", tool="shell"))
    assert cli.main(["hook", "kiro-guard"]) == 2
    assert (
        capsys.readouterr().err == "Treant guard: guard error Re-run the same command to proceed."
    )
    assert not (tmp_path / "_state" / "kiro-guard.json").exists()  # no state: no free retry
    for hook, tool in (("claude-pre-tool-use", "Bash"), ("kiro-guard", "shell")):
        _stdin(monkeypatch, _guard_payload("ls -la", tool=tool))
        assert cli.main(["hook", hook]) == 0
        _stdin(monkeypatch, _guard_payload("kubectl get po", tool="Read"))
        assert cli.main(["hook", hook]) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_guard_hooks_fail_closed_when_handler_raises(monkeypatch, capsys, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(hooks_claude, "pre_tool_use", boom)
    monkeypatch.setattr(kiro, "kiro_guard", boom)
    _assert_fails_closed(monkeypatch, capsys, tmp_path)


def test_guard_hooks_fail_closed_when_engine_cannot_import(monkeypatch, capsys, tmp_path):
    import treant_client

    for name in list(sys.modules):
        if name == "treant_guard" or name.startswith("treant_guard."):
            monkeypatch.setitem(sys.modules, name, None)
    for name in ("hooks_claude", "kiro"):
        monkeypatch.delitem(sys.modules, f"treant_client.{name}")
        monkeypatch.delattr(treant_client, name)
    _assert_fails_closed(monkeypatch, capsys, tmp_path)


def test_guard_fallback_off_inside_summarizer(monkeypatch, capsys):
    monkeypatch.setenv("TREANT_SUMMARIZER", "1")
    monkeypatch.setattr(hooks_claude, "pre_tool_use", lambda p: 1 / 0)
    _stdin(monkeypatch, _guard_payload("kubectl get po"))
    assert cli.main(["hook", "claude-pre-tool-use"]) == 0
    assert capsys.readouterr().out == ""


def test_fallback_constants_match_the_engine():
    from treant_guard.rules import default_rules

    pack = {c for r in default_rules() for c in r.command}
    assert set(cli._FALLBACK_GUARD_WORDS) == pack
    assert cli._RETRY_HINT == kiro.RETRY_HINT
    assert cli._KIRO_SHELL_TOOLS == kiro.SHELL_TOOLS


def test_kiro_start_prints_text(monkeypatch, capsys):
    monkeypatch.setattr(kiro, "kiro_start", lambda p, cwd: f"hello {cwd}")
    _stdin(monkeypatch, '{"cwd": "/w"}')
    assert cli.main(["hook", "kiro-start"]) == 0
    assert capsys.readouterr().out == "hello /w"


def test_save_worker_args(monkeypatch, capsys, tmp_path):
    got = {}

    def fake(sid, cwd, load, trigger, *a, **k):
        got.update(sid=sid, cwd=cwd, trigger=trigger, load=load)
        return "saved"

    monkeypatch.setattr(save, "save_worker", fake)
    monkeypatch.setattr("treant_client.transcript.claude_text", lambda p: (f"T:{p}", 2))
    rc = cli.main(
        [
            "save-worker",
            "--source",
            "claude",
            "--session-id",
            "s1",
            "--cwd",
            "/c",
            "--path",
            str(tmp_path / "t.jsonl"),
            "--trigger",
            "session_end",
        ]
    )
    assert rc == 0 and capsys.readouterr().out == ""
    assert (got["sid"], got["cwd"], got["trigger"]) == ("s1", "/c", "session_end")
    assert got["load"]() == (f"T:{tmp_path / 't.jsonl'}", 2)


def test_token_set_refuses_unknown_workspace(monkeypatch, capsys, cfg_tmp):
    monkeypatch.setattr("treant_client.config.load_config", lambda *a, **k: cfg_tmp)
    called = []
    monkeypatch.setattr(keychain, "set_token", lambda ws, **k: called.append(ws) or True)
    assert cli.main(["token", "set", "nope"]) == 1
    assert not called and "unknown workspace" in capsys.readouterr().err
    assert cli.main(["token", "set", "work"]) == 0 and called == ["work"]


def test_token_check_never_prints_token(monkeypatch, capsys, cfg_tmp):
    monkeypatch.setattr("treant_client.config.load_config", lambda *a, **k: cfg_tmp)
    monkeypatch.setattr(keychain, "get_token", lambda ws, **k: "tok-test" if ws == "work" else None)

    class S:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

        def call_tool(self, *a):
            return {"brief": ""}

    monkeypatch.setattr("treant_client.mcp_http.TreantSession", S)
    assert cli.main(["token", "check"]) == 0
    out = capsys.readouterr().out
    assert "work: set ok" in out and "volunteer: missing" in out and "tok-test" not in out


def test_runnable_as_module(tmp_path):
    r = subprocess.run(
        [sys.executable, "-P", "-m", "treant_client.cli", "--help"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    assert r.returncode == 0 and "treant-client" in r.stdout


def test_kiro_brief_prints_resume_text(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(kiro, "resume_brief", lambda cwd: f"brief {cwd}")
    assert cli.main(["kiro-brief"]) == 0
    assert capsys.readouterr().out == f"brief {tmp_path}"


def test_kiro_brief_fails_open(monkeypatch, capsys):
    def boom(cwd):
        raise RuntimeError("x")

    monkeypatch.setattr(kiro, "resume_brief", boom)
    assert cli.main(["kiro-brief"]) == 0
    assert capsys.readouterr().out == ""


def test_unknown_hook_name_exits_zero(monkeypatch, capsys):
    # settings or an agent file naming a hook this binary does not know (binary/settings
    # drift) must not exit 2: Claude Code and Kiro read 2 as "block this tool call"
    _stdin(monkeypatch, _guard_payload("kubectl get po"))
    assert cli.main(["hook", "claude-not-a-hook"]) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_kiro_sweep_runs_and_fails_open(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(kiro, "kiro_sweep", lambda now_ms: seen.append(now_ms) or ["saved"])
    assert cli.main(["kiro-sweep"]) == 0 and len(seen) == 1 and seen[0] > 1_700_000_000_000

    def boom(now_ms):
        raise RuntimeError("x")

    monkeypatch.setattr(kiro, "kiro_sweep", boom)
    assert cli.main(["kiro-sweep"]) == 0
    assert capsys.readouterr().out == ""


def test_check_stale_args_and_exit_codes(monkeypatch, capsys, tmp_path):
    from treant_client import stale
    from treant_client.config import Config

    calls = []
    result = {}

    def fake(path, cfg, **kw):
        calls.append((path, cfg, kw))
        return result["res"]

    cfg = Config(url="https://example.invalid/mcp")
    monkeypatch.setattr(stale, "run", fake)
    monkeypatch.setattr("treant_client.config.load_config", lambda *a, **k: cfg)
    doubt = stale.InDoubt(
        "f-1",
        [
            {"token": "old_name.py", "commit": "abc1234", "subject": "rename old_name"},
            {"token": "src/gone.py", "commit": None, "subject": None},
        ],
    )
    result["res"] = stale.RunResult("ok", 3, 0, [doubt])
    p = str(tmp_path)
    assert cli.main(["check-stale", "--repo", p, "--dry-run"]) == 0
    assert calls[-1] == (p, cfg, {"dry_run": True, "use_memo": False})
    out = capsys.readouterr().out
    assert "would flag f-1: old_name.py (removed in abc1234: rename old_name)" in out
    assert "would flag f-1: src/gone.py (not found)" in out
    assert out.rstrip().endswith("checked 3 facts, 1 in doubt (dry run)")

    monkeypatch.chdir(tmp_path)
    result["res"] = stale.RunResult("ok", 5, 2, [doubt])
    assert cli.main(["check-stale"]) == 0
    assert calls[-1][0] == os.getcwd()
    assert calls[-1][2] == {"dry_run": False, "use_memo": False}
    assert capsys.readouterr().out.strip() == "checked 5 facts, flagged 2"

    result["res"] = stale.RunResult("stopped_budget", 5, 2, [])
    assert cli.main(["check-stale"]) == 0
    capsys.readouterr()

    result["res"] = stale.RunResult("not_a_repo", 0, 0, [])
    assert cli.main(["check-stale"]) == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "check-stale: not_a_repo" and captured.out == ""
