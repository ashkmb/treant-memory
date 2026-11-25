import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from treant_guard.lexer import words

from treant_client.hooks_claude import (
    detach,
    pre_compact,
    pre_tool_use,
    session_end,
    session_start,
    start_text,
)
from treant_client.mcp_http import TreantError


class FakeSession:
    def __init__(self, result=None, delay=0.0, raises=None):
        self.result, self.delay, self.raises = result, delay, raises
        self.calls = []

    def factory(self, url, token, timeout):
        self.token, self.timeout = token, timeout
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def call_tool(self, name, args):
        self.calls.append((name, args))
        time.sleep(self.delay)
        if self.raises:
            raise self.raises
        return self.result


def no_git(argv, **kw):
    return subprocess.CompletedProcess(argv, 1, "", "")


def tok(ws):
    return "tok-test"


def _start(cfg, cwd, sess, getter=tok, run=no_git):
    return session_start(
        {"cwd": str(cwd)}, cfg, token_getter=getter, session_factory=sess.factory, run=run
    )


def test_start_unmapped_no_output(cfg_tmp, tmp_path):
    s = FakeSession({"brief": "x"})
    assert _start(cfg_tmp, tmp_path, s) is None
    assert s.calls == []


def test_start_no_token_note_only(cfg_tmp):
    s = FakeSession({"brief": "x"})
    cwd = next(p for p in cfg_tmp.folders if p.endswith("Work"))
    out = _start(cfg_tmp, cwd, s, getter=lambda ws: None)
    assert out is not None and "Treant memory" not in _ctx(out)
    assert s.calls == []


def test_start_includes_workspace_line_and_brief(cfg_tmp):
    cwd = next(p for p in cfg_tmp.folders if p.endswith("Work"))

    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, "/x/my-repo\n", "")

    s = FakeSession({"brief": "BRIEF TEXT"})
    out = json.loads(_start(cfg_tmp, cwd, s, run=run))
    h = out["hookSpecificOutput"]
    assert h["hookEventName"] == "SessionStart"
    assert h["additionalContext"] == _note(cfg_tmp) + "\n\n" + start_text("work", "BRIEF TEXT")
    assert "Treant memory is on for workspace work." in h["additionalContext"]
    assert s.calls == [("brief", {"repo": "my-repo"})]
    assert s.token == "tok-test"


def test_start_empty_brief_only_workspace_line(cfg_tmp):
    cwd = next(p for p in cfg_tmp.folders if p.endswith("Volunteer"))
    s = FakeSession({"brief": ""})
    out = json.loads(_start(cfg_tmp, cwd, s))
    assert out["hookSpecificOutput"]["additionalContext"] == (
        _note(cfg_tmp, "volunteer", "Volunteer") + "\n\n" + start_text("volunteer", "")
    )
    assert s.calls == [("brief", {})]


def test_start_error_keeps_note(cfg_tmp):
    cwd = next(p for p in cfg_tmp.folders if p.endswith("Volunteer"))
    out = _start(cfg_tmp, cwd, FakeSession(raises=TreantError("unreachable")))
    assert _ctx(out) == _note(cfg_tmp, "volunteer", "Volunteer")


def _guard(cfg, cwd, sess, command="kubectl --context prod get pods", tool="Bash", gcfg=None, **kw):
    payload = {"cwd": str(cwd), "tool_name": tool, "tool_input": {"command": command}}
    kw.setdefault("token_getter", tok)
    kw.setdefault("run", no_git)
    return pre_tool_use(
        payload,
        cfg,
        guard_config_loader=lambda: gcfg,
        session_factory=sess.factory,
        **kw,
    )


def _work(cfg):
    return next(p for p in cfg.folders if p.endswith("Work"))


def _reason(out, decision="ask"):
    h = json.loads(out)["hookSpecificOutput"]
    assert h["hookEventName"] == "PreToolUse"
    assert h["permissionDecision"] == decision
    assert set(h) == {"hookEventName", "permissionDecision", "permissionDecisionReason"}
    return h["permissionDecisionReason"]


def _log_lines():
    path = Path(os.environ["TREANT_CLIENT_LOG_DIR"]) / "client.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


LISTED = "kubectl --context ops get pods"


def test_guard_unlisted_asks_with_reason(cfg_tmp, guard_cfg):
    s = FakeSession({"warnings": []})
    out = _guard(cfg_tmp, _work(cfg_tmp), s, gcfg=guard_cfg)
    assert _reason(out) == "Treant guard (work): not allowed in work: kube_context=prod"


def test_guard_works_without_token(cfg_tmp, guard_cfg):
    s = FakeSession({"warnings": ["never"]})
    out = _guard(cfg_tmp, _work(cfg_tmp), s, gcfg=guard_cfg, token_getter=lambda ws: None)
    assert _reason(out) == "Treant guard (work): not allowed in work: kube_context=prod"
    assert s.calls == []


def test_guard_unresolved_asks(cfg_tmp, guard_cfg):
    out = _guard(cfg_tmp, _work(cfg_tmp), FakeSession(), command="kubectl get pods", gcfg=guard_cfg)
    assert _reason(out) == "Treant guard (work): can't tell which kube_context this uses"


def test_guard_allow_listed_no_http(cfg_tmp, guard_cfg):
    s = FakeSession({"warnings": []})
    assert _guard(cfg_tmp, _work(cfg_tmp), s, command="ls -la", gcfg=guard_cfg) is None
    assert s.calls == []
    # a listed target is allowed; the warnings lookup is the only server call
    assert _guard(cfg_tmp, _work(cfg_tmp), s, command=LISTED, gcfg=guard_cfg) is None
    assert s.calls == [("warnings", {"names": words(LISTED)})]


def test_guard_unmapped_folder_allows(cfg_tmp, guard_cfg, tmp_path):
    s = FakeSession({"warnings": ["w"]})
    assert _guard(cfg_tmp, tmp_path, s, gcfg=guard_cfg) is None
    assert s.calls == []


def test_warning_turns_allow_into_ask(cfg_tmp, guard_cfg):
    s = FakeSession({"warnings": ["w1", "w2"]})
    out = _guard(cfg_tmp, _work(cfg_tmp), s, command=LISTED, gcfg=guard_cfg)
    assert _reason(out) == "Treant guard (work): warnings: w1 | w2"
    assert s.token == "tok-test"


def test_warnings_appended_to_ask(cfg_tmp, guard_cfg):
    s = FakeSession({"warnings": ["w1"]})
    out = _guard(cfg_tmp, _work(cfg_tmp), s, gcfg=guard_cfg)
    assert _reason(out) == (
        "Treant guard (work): not allowed in work: kube_context=prod; warnings: w1"
    )


def test_warnings_failure_keeps_local_decision(cfg_tmp, guard_cfg):
    work = _work(cfg_tmp)
    down = FakeSession(raises=TreantError("http_404"))
    assert _guard(cfg_tmp, work, down, command=LISTED, gcfg=guard_cfg) is None
    out = _guard(cfg_tmp, work, down, gcfg=guard_cfg)
    assert _reason(out) == "Treant guard (work): not allowed in work: kube_context=prod"
    assert _guard(cfg_tmp, work, FakeSession({"nope": 1}), command=LISTED, gcfg=guard_cfg) is None


def test_slow_warnings_keep_local_decision(cfg_tmp, guard_cfg):
    work = _work(cfg_tmp)
    slow = FakeSession({"warnings": ["late"]}, delay=2)
    t0 = time.monotonic()
    out = _guard(cfg_tmp, work, slow, gcfg=guard_cfg, budget=0.5)
    assert time.monotonic() - t0 < 1.0
    assert _reason(out) == "Treant guard (work): not allowed in work: kube_context=prod"
    assert _guard(cfg_tmp, work, slow, command=LISTED, gcfg=guard_cfg, budget=0.5) is None


def test_warnings_skipped_when_budget_is_spent(cfg_tmp, guard_cfg):
    s = FakeSession({"warnings": ["w"]})
    assert _guard(cfg_tmp, _work(cfg_tmp), s, command=LISTED, gcfg=guard_cfg, budget=0.2) is None
    assert s.calls == []


def test_warnings_timeout_fits_the_budget(cfg_tmp, guard_cfg):
    s = FakeSession({"warnings": []})
    _guard(cfg_tmp, _work(cfg_tmp), s, command=LISTED, gcfg=guard_cfg)
    assert s.timeout == 2.0  # HTTP_TIMEOUT when the budget has room
    _guard(cfg_tmp, _work(cfg_tmp), s, command=LISTED, gcfg=guard_cfg, budget=1.0)
    assert 0.5 < s.timeout <= 0.9  # what is left of the budget, minus a margin


def test_guard_timeout_asks(cfg_tmp, guard_cfg):
    def slow_run(argv, **kw):
        time.sleep(1.5)
        return subprocess.CompletedProcess(argv, 0, "ops\n", "")

    s = FakeSession({"warnings": []})
    t0 = time.monotonic()
    out = _guard(
        cfg_tmp,
        _work(cfg_tmp),
        s,
        command="kubectl get pods",
        gcfg=guard_cfg,
        run=slow_run,
        budget=0.3,
    )
    assert time.monotonic() - t0 < 1.0
    assert _reason(out) == "Treant guard: guard timed out"
    assert s.calls == []
    last = _log_lines()[-1]
    assert (last["event"], last["status"]) == ("guard_hook", "timeout")


def test_invalid_guard_config_asks(cfg_tmp, tmp_path):
    from treant_guard.config import load_config as load_guard_config

    gdir = tmp_path / "g"
    gdir.mkdir()
    (gdir / "guard.toml").write_text("[workspaces\n")
    gcfg = load_guard_config(gdir)
    s = FakeSession({"warnings": []})
    out = _guard(cfg_tmp, _work(cfg_tmp), s, command=LISTED, gcfg=gcfg)
    assert _reason(out) == f"Treant guard: guard config invalid: {gdir / 'guard.toml'}"
    assert _guard(cfg_tmp, _work(cfg_tmp), s, command="ls -la", gcfg=gcfg) is None
    assert s.calls == []


def test_guard_error_asks_only_for_guarded_commands(cfg_tmp):
    def boom():
        raise RuntimeError("broken")

    payload = {"cwd": _work(cfg_tmp), "tool_name": "Bash", "tool_input": {"command": "git push"}}
    out = pre_tool_use(payload, cfg_tmp, guard_config_loader=boom, token_getter=tok)
    assert _reason(out) == "Treant guard: guard error"
    payload["tool_input"]["command"] = "ls -la"
    assert pre_tool_use(payload, cfg_tmp, guard_config_loader=boom, token_getter=tok) is None


def test_guard_client_config_error_asks(monkeypatch, tmp_path, guard_cfg):
    import treant_client.config as c

    def boom(*a, **k):
        raise ValueError("bad toml")

    monkeypatch.setattr(c, "load_config", boom)
    payload = {"cwd": str(tmp_path), "tool_name": "Bash", "tool_input": {"command": "git push"}}
    out = pre_tool_use(payload, None, guard_config_loader=lambda: guard_cfg)
    assert _reason(out) == "Treant guard: guard error"
    assert session_start({"cwd": str(tmp_path)}, None) is None


def test_guard_workspaces_fall_back_to_client_folders(cfg_tmp, guard_cfg):
    from dataclasses import replace

    no_map = replace(guard_cfg, workspaces={})
    out = _guard(cfg_tmp, _work(cfg_tmp), FakeSession({"warnings": []}), gcfg=no_map)
    assert _reason(out) == "Treant guard (work): not allowed in work: kube_context=prod"


def test_guard_logs_status_never_command(cfg_tmp, guard_cfg):
    _guard(cfg_tmp, _work(cfg_tmp), FakeSession({"warnings": []}), gcfg=guard_cfg)
    _guard(cfg_tmp, _work(cfg_tmp), FakeSession({"warnings": []}), command=LISTED, gcfg=guard_cfg)
    _guard(cfg_tmp, _work(cfg_tmp), FakeSession(), command="ls -la", gcfg=guard_cfg)
    lines = [r for r in _log_lines() if r["event"] == "guard_hook"]
    assert [(r["status"], r["workspace"]) for r in lines] == [
        ("ask", "work"),
        ("allow", "work"),
    ]
    assert "prod" not in json.dumps(lines) and "kubectl" not in json.dumps(lines)


def test_guard_non_bash_tool_ignored(cfg_tmp, guard_cfg):
    s = FakeSession({"warnings": ["w"]})
    assert _guard(cfg_tmp, _work(cfg_tmp), s, tool="Read", gcfg=guard_cfg) is None
    assert s.calls == []


def test_hooks_noop_inside_summarizer(cfg_tmp, guard_cfg, monkeypatch):
    monkeypatch.setenv("TREANT_SUMMARIZER", "1")
    cwd = _work(cfg_tmp)
    s = FakeSession({"brief": "x", "warnings": ["w"]})
    assert _start(cfg_tmp, cwd, s) is None
    assert _guard(cfg_tmp, cwd, s, gcfg=guard_cfg) is None
    assert s.calls == []


class FakePopen:
    def __init__(self, raises=None, gate=None):
        self.raises, self.gate = raises, gate  # gate: a threading.Event the call waits on
        self.calls = []

    def __call__(self, argv, **kw):
        if self.gate is not None:
            self.gate.wait(5)
        self.calls.append((argv, kw))
        if self.raises:
            raise self.raises
        return object()


def _end_payload(cwd, event="SessionEnd", **extra):
    return {
        "session_id": "sid-1",
        "transcript_path": "/x/sid-1.jsonl",
        "cwd": str(cwd),
        "hook_event_name": event,
        **extra,
    }


def _worker_argv(cwd, trigger):
    return [
        sys.executable,
        "-P",
        "-m",
        "treant_client.cli",
        "save-worker",
        "--source",
        "claude",
        "--session-id",
        "sid-1",
        "--cwd",
        str(cwd),
        "--path",
        "/x/sid-1.jsonl",
        "--trigger",
        trigger,
    ]


def test_session_end_detaches_and_returns_fast(cfg_tmp, tmp_path):
    cwd = next(p for p in cfg_tmp.folders if p.endswith("Work"))
    popen = FakePopen()
    t0 = time.monotonic()
    assert session_end(_end_payload(cwd, reason="prompt_input_exit"), cfg_tmp, popen=popen) is None
    assert time.monotonic() - t0 < 1.0
    [(argv, kw)] = popen.calls
    assert argv == _worker_argv(cwd, "session_end")
    assert kw["start_new_session"] is True and kw["close_fds"] is True
    # never the project folder: python -m would import modules from it
    state = tmp_path / "_state"
    assert kw["cwd"] == str(state) and kw["cwd"] != str(cwd)
    assert state.is_dir() and oct(state.stat().st_mode & 0o777) == "0o700"
    for stream in ("stdin", "stdout", "stderr"):
        assert kw[stream] == subprocess.DEVNULL


def test_pre_compact_detaches_with_trigger(cfg_tmp):
    cwd = next(p for p in cfg_tmp.folders if p.endswith("Volunteer"))
    popen = FakePopen()
    payload = _end_payload(cwd, event="PreCompact", trigger="auto")
    assert pre_compact(payload, cfg_tmp, popen=popen) is None
    assert [argv for argv, _ in popen.calls] == [_worker_argv(cwd, "pre_compact")]


def test_save_hooks_noop_when_unmapped_or_incomplete(cfg_tmp, tmp_path):
    cwd = next(p for p in cfg_tmp.folders if p.endswith("Work"))
    popen = FakePopen()
    assert session_end(_end_payload(tmp_path), cfg_tmp, popen=popen) is None
    assert pre_compact(_end_payload(tmp_path), cfg_tmp, popen=popen) is None
    for missing in ("session_id", "transcript_path", "cwd"):
        p = _end_payload(cwd)
        del p[missing]
        assert session_end(p, cfg_tmp, popen=popen) is None
    assert session_end(_end_payload(cwd, session_id=5), cfg_tmp, popen=popen) is None
    assert popen.calls == []


def test_save_hooks_noop_inside_summarizer(cfg_tmp, monkeypatch):
    monkeypatch.setenv("TREANT_SUMMARIZER", "1")
    cwd = next(p for p in cfg_tmp.folders if p.endswith("Work"))
    popen = FakePopen()
    assert session_end(_end_payload(cwd), cfg_tmp, popen=popen) is None
    assert pre_compact(_end_payload(cwd), cfg_tmp, popen=popen) is None
    assert popen.calls == []


def test_save_hooks_fail_open(cfg_tmp, monkeypatch):
    cwd = next(p for p in cfg_tmp.folders if p.endswith("Work"))
    assert session_end(_end_payload(cwd), cfg_tmp, popen=FakePopen(OSError("x"))) is None
    gate = threading.Event()
    t0 = time.monotonic()
    try:
        assert session_end(_end_payload(cwd), cfg_tmp, popen=FakePopen(gate=gate)) is None
        assert time.monotonic() - t0 < 1.0
    finally:
        gate.set()  # let the abandoned worker thread finish instead of outliving the test

    import treant_client.config as c

    def boom(*a, **k):
        raise ValueError("bad toml")

    monkeypatch.setattr(c, "load_config", boom)
    assert session_end(_end_payload(cwd), None, popen=FakePopen()) is None


def test_save_hook_fails_open_when_the_state_dir_cannot_be_made(cfg_tmp, tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("TREANT_STATE_DIR", str(blocker / "state"))
    cwd = next(p for p in cfg_tmp.folders if p.endswith("Work"))
    popen = FakePopen()
    assert session_end(_end_payload(cwd), cfg_tmp, popen=popen) is None
    assert popen.calls == []
    last = _log_lines()[-1]
    assert (last["event"], last["status"], last["category"]) == ("save_hook", "error", "error")


def test_detach_argv(tmp_path):
    popen = FakePopen()
    detach(["save-worker", "--x"], popen=popen)
    [(argv, kw)] = popen.calls
    assert argv == [sys.executable, "-P", "-m", "treant_client.cli", "save-worker", "--x"]
    assert kw["start_new_session"] is True
    assert kw["cwd"] == str(tmp_path / "_state")


# ---- workspace from CLAUDE_PROJECT_DIR (the session's project), not the drifting payload cwd ----


def _work_project_personal_cwd(cfg, monkeypatch):
    work = next(p for p in cfg.folders if p.endswith("Work"))
    personal = next(p for p, w in cfg.folders.items() if w == "personal")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", work)
    return work, personal


def test_start_uses_claude_project_dir(cfg_tmp, monkeypatch):
    _, personal = _work_project_personal_cwd(cfg_tmp, monkeypatch)
    asked = []
    s = FakeSession({"brief": ""})
    out = session_start(
        {"cwd": personal},
        cfg_tmp,
        token_getter=lambda ws: asked.append(ws) or "tok-test",
        session_factory=s.factory,
        run=no_git,
    )
    assert asked == ["work"]
    assert _ctx(out).endswith(start_text("work", ""))
    assert _ctx(out).startswith("Workspace: work (folder ")


def test_guard_uses_claude_project_dir_but_checks_payload_cwd(cfg_tmp, guard_cfg, monkeypatch):
    _, personal = _work_project_personal_cwd(cfg_tmp, monkeypatch)
    asked = []
    s = FakeSession({"warnings": []})
    payload = {"cwd": personal, "tool_name": "Bash", "tool_input": {"command": "terraform plan"}}
    out = pre_tool_use(
        payload,
        cfg_tmp,
        guard_config_loader=lambda: guard_cfg,
        token_getter=lambda ws: asked.append(ws) or "tok-test",
        session_factory=s.factory,
        run=no_git,
    )
    assert asked == ["work"]
    # the workspace comes from the project; terraform_dir from the command's own folder
    assert _reason(out) == f"Treant guard (work): not allowed in work: terraform_dir={personal}"


def test_session_end_uses_claude_project_dir(cfg_tmp, monkeypatch):
    work, personal = _work_project_personal_cwd(cfg_tmp, monkeypatch)
    popen = FakePopen()
    session_end(_end_payload(personal), cfg_tmp, popen=popen)
    assert [argv for argv, _ in popen.calls] == [_worker_argv(work, "session_end")]


def test_pre_compact_uses_claude_project_dir(cfg_tmp, monkeypatch):
    work, personal = _work_project_personal_cwd(cfg_tmp, monkeypatch)
    popen = FakePopen()
    pre_compact(_end_payload(personal, event="PreCompact"), cfg_tmp, popen=popen)
    assert [argv for argv, _ in popen.calls] == [_worker_argv(work, "pre_compact")]


def test_save_hooks_unmapped_project_dir_wins_over_mapped_cwd(cfg_tmp, monkeypatch, tmp_path):
    personal = next(p for p, w in cfg_tmp.folders.items() if w == "personal")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    popen = FakePopen()
    session_end(_end_payload(personal), cfg_tmp, popen=popen)
    assert popen.calls == []


def _deny_cfg(guard_cfg):
    from dataclasses import replace

    return replace(guard_cfg, deny={"work": {"kube_context": ["prod"]}})


DENIED = "Treant guard (work): denied in work: kube_context=prod"


def test_deny_returns_deny_json(cfg_tmp, guard_cfg):
    out = _guard(cfg_tmp, _work(cfg_tmp), FakeSession({"warnings": []}), gcfg=_deny_cfg(guard_cfg))
    assert _reason(out, "deny") == DENIED


def test_warnings_skipped_on_deny(cfg_tmp, guard_cfg):
    s = FakeSession({"warnings": ["w1"]})
    out = _guard(cfg_tmp, _work(cfg_tmp), s, gcfg=_deny_cfg(guard_cfg))
    assert _reason(out, "deny") == DENIED  # no "; warnings: w1"
    assert s.calls == []


def test_deny_is_logged_as_deny(cfg_tmp, guard_cfg):
    _guard(cfg_tmp, _work(cfg_tmp), FakeSession({"warnings": []}), gcfg=_deny_cfg(guard_cfg))
    assert [r["status"] for r in _log_lines() if r.get("workspace") == "work"][-1] == "deny"


# --- workspace note at session start ---------------------------------------------------------


def _note(cfg, ws="work", suffix="Work"):
    return f"Workspace: {ws} (folder {next(p for p in cfg.folders if p.endswith(suffix))})."


def _ctx(out):
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_start_note_without_treant(cfg_tmp):
    cwd = next(p for p in cfg_tmp.folders if p.endswith("Work"))
    s = FakeSession({"brief": "x"})
    out = _start(cfg_tmp, cwd, s, getter=lambda ws: None)
    assert _ctx(out) == _note(cfg_tmp)
    assert s.calls == []


def test_start_note_with_treant_brief(cfg_tmp):
    cwd = next(p for p in cfg_tmp.folders if p.endswith("Work"))
    out = _start(cfg_tmp, cwd, FakeSession({"brief": "BRIEF"}))
    assert _ctx(out) == _note(cfg_tmp) + "\n\n" + start_text("work", "BRIEF")


def test_start_note_server_down(cfg_tmp):
    cwd = next(p for p in cfg_tmp.folders if p.endswith("Work"))
    out = _start(cfg_tmp, cwd, FakeSession(raises=TreantError("unreachable")))
    assert _ctx(out) == _note(cfg_tmp)


def test_start_note_server_slow(cfg_tmp):
    cwd = next(p for p in cfg_tmp.folders if p.endswith("Work"))
    out = session_start(
        {"cwd": cwd},
        cfg_tmp,
        token_getter=tok,
        session_factory=FakeSession({"brief": "x"}, delay=2).factory,
        run=no_git,
        budget=0.3,
    )
    assert _ctx(out) == _note(cfg_tmp)


def test_start_note_with_about(cfg_tmp):
    from dataclasses import replace

    cfg = replace(cfg_tmp, about={"work": "Work. Never touch prod."})
    cwd = next(p for p in cfg.folders if p.endswith("Work"))
    out = _start(cfg, cwd, FakeSession({"brief": "B"}), getter=lambda ws: None)
    assert _ctx(out) == _note(cfg) + "\nWork. Never touch prod."
    out = _start(cfg, cwd, FakeSession({"brief": "B"}))
    assert _ctx(out) == (_note(cfg) + "\nWork. Never touch prod.\n\n" + start_text("work", "B"))


def test_start_note_unmapped_is_empty(cfg_tmp, tmp_path):
    assert _start(cfg_tmp, tmp_path, FakeSession({"brief": "x"}), getter=lambda ws: None) is None


def test_start_note_shows_home_as_tilde(cfg_tmp, monkeypatch):
    from dataclasses import replace

    home = Path.home()
    cfg = replace(cfg_tmp, folders={str(home / "Documents/Projects/Work"): "work"})
    out = session_start({"cwd": "/nonexistent"}, cfg, token_getter=lambda ws: None, run=no_git)
    assert out is None  # unmapped cwd
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(home / "Documents/Projects/Work/x"))
    out = session_start({"cwd": "/nonexistent"}, cfg, token_getter=lambda ws: None, run=no_git)
    assert _ctx(out) == "Workspace: work (folder ~/Documents/Projects/Work)."


def test_start_note_with_invalid_guard_config(tmp_path, monkeypatch):
    from treant_client import config as cc

    gdir = tmp_path / "g"
    gdir.mkdir()
    (gdir / "guard.toml").write_text("not = [toml\n")
    home = tmp_path / "home"
    (home / "work").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    own = tmp_path / "c.toml"
    own.write_text('[folders]\n"~/work" = "work"\n')
    cfg = cc.load_config(own, gdir)
    assert cfg.about == {}
    out = session_start({"cwd": str(home / "work")}, cfg, token_getter=lambda ws: None, run=no_git)
    assert _ctx(out) == "Workspace: work (folder ~/work)."


def test_start_with_empty_folder_map_is_silent(tmp_path):
    from treant_client import config as cc

    gdir = tmp_path / "g"
    gdir.mkdir()
    (gdir / "guard.toml").write_text("not = [toml\n")
    cfg = cc.load_config(tmp_path / "none.toml", gdir)
    assert cfg.folders == {}
    assert session_start({"cwd": str(tmp_path)}, cfg, token_getter=tok, run=no_git) is None


def test_start_slow_config_step_stays_within_budget(cfg_tmp, monkeypatch):
    from treant_client import hooks_claude

    def slow_cfg(cfg):
        time.sleep(2)
        return cfg

    monkeypatch.setattr(hooks_claude, "_cfg", slow_cfg)
    cwd = next(p for p in cfg_tmp.folders if p.endswith("Work"))
    t0 = time.monotonic()
    out = session_start(
        {"cwd": cwd},
        cfg_tmp,
        token_getter=tok,
        run=no_git,
        budget=0.3,
        session_factory=FakeSession({"brief": "x"}).factory,
    )
    assert out is None and time.monotonic() - t0 < 1.0


def test_deny_without_a_reason_still_denies(cfg_tmp, guard_cfg, monkeypatch):
    monkeypatch.setattr("treant_client.hooks_claude.guard_command", lambda *a, **k: ("deny", None))
    out = _guard(cfg_tmp, _work(cfg_tmp), FakeSession(), gcfg=guard_cfg)
    assert _reason(out, "deny") == "Treant guard: denied"


def test_guard_protect_mode_asks_only_for_protected_and_other_workspace(cfg_tmp, guard_cfg):
    from dataclasses import replace

    gcfg = replace(guard_cfg, mode="protect", protect={"*": {"kube_context": ["prod"]}})
    work = _work(cfg_tmp)
    out = _guard(cfg_tmp, work, FakeSession(), gcfg=gcfg)
    assert _reason(out) == "Treant guard (work): protected: kube_context=prod"
    out = _guard(cfg_tmp, work, FakeSession(), command="kubectl --context home get po", gcfg=gcfg)
    assert _reason(out) == "Treant guard (work): belongs to personal: kube_context=home"
    for command in ("kubectl --context scratch get po", "kubectl get po", "kubectx dev"):
        assert _guard(cfg_tmp, work, FakeSession(), command=command, gcfg=gcfg) is None, command
    out = _guard(cfg_tmp, work, FakeSession(), command="kubectx prod", gcfg=gcfg)
    assert _reason(out) == "Treant guard (work): protected: kube_context=prod"


def test_switch_asks_when_guard_config_never_loads(cfg_tmp):
    def slow_loader():
        time.sleep(1.5)

    payload = {
        "cwd": _work(cfg_tmp),
        "tool_name": "Bash",
        "tool_input": {"command": "kubectx prod"},
    }
    out = pre_tool_use(payload, cfg_tmp, guard_config_loader=slow_loader, budget=0.3)
    assert _reason(out) == "Treant guard: guard timed out"


def test_switch_asks_on_invalid_guard_config(cfg_tmp, tmp_path):
    from treant_guard.config import load_config as load_guard_config

    gdir = tmp_path / "g"
    gdir.mkdir()
    (gdir / "guard.toml").write_text("[workspaces\n")
    gcfg = load_guard_config(gdir)
    out = _guard(cfg_tmp, _work(cfg_tmp), FakeSession(), command="kubectx prod", gcfg=gcfg)
    assert _reason(out) == f"Treant guard: guard config invalid: {gdir / 'guard.toml'}"


def test_guard_timeout_log_names_the_workspace(cfg_tmp, guard_cfg):
    def slow_run(argv, **kw):
        time.sleep(1.5)
        return subprocess.CompletedProcess(argv, 0, "ops\n", "")

    _guard(
        cfg_tmp,
        _work(cfg_tmp),
        FakeSession(),
        command="kubectl get pods",
        gcfg=guard_cfg,
        run=slow_run,
        budget=0.3,
    )
    last = _log_lines()[-1]
    assert (last["event"], last["status"], last["workspace"]) == ("guard_hook", "timeout", "work")


def test_deny_decided_just_before_the_budget_runs_out(cfg_tmp, guard_cfg, monkeypatch):
    # the deadline fires after check() decided deny: the deny stands, it never turns into an ask
    from treant_client import hooks_claude

    def run_then_time_out(fn, seconds):
        fn()
        return False, "timeout"

    monkeypatch.setattr(hooks_claude, "_with_deadline", run_then_time_out)
    out = _guard(cfg_tmp, _work(cfg_tmp), FakeSession(), gcfg=_deny_cfg(guard_cfg))
    assert _reason(out, "deny").startswith("Treant guard (work): denied")
    last = _log_lines()[-1]
    assert (last["event"], last["status"]) == ("guard_hook", "deny")


def test_engine_deny_with_empty_reason_denies(cfg_tmp, guard_cfg, monkeypatch):
    from treant_guard.engine import Decision

    d = Decision("deny", "work", [], [], [("kube_context", "prod", False)], True, "")
    monkeypatch.setattr("treant_client.hooks_claude.check", lambda *a, **k: d)
    s = FakeSession({"warnings": ["w"]})
    out = _guard(cfg_tmp, _work(cfg_tmp), s, gcfg=guard_cfg)
    assert _reason(out, "deny") == "Treant guard: denied"
    assert s.calls == []


def test_start_without_cfg_survives_a_broken_guard_dir(tmp_path, monkeypatch):
    # cfg=None: the hook loads both configs itself; a broken guard dir falls back to [folders]
    from functools import partial

    from treant_client import config as cc

    gdir = tmp_path / "g"
    (gdir / "guard.toml").mkdir(parents=True)  # unreadable as a file
    home = tmp_path / "home"
    (home / "work").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    own = tmp_path / "c.toml"
    own.write_text('[folders]\n"~/work" = "work"\n')
    monkeypatch.setattr(cc, "load_config", partial(cc.load_config, own, gdir))
    out = session_start({"cwd": str(home / "work")}, None, token_getter=lambda ws: None, run=no_git)
    assert _ctx(out) == "Workspace: work (folder ~/work)."


def test_ask_json_shape_matches_the_cli_fallback(cfg_tmp, guard_cfg, monkeypatch, capsys):
    # the CLI builds its own ask JSON (it runs when hooks_claude cannot import): pin both
    import io
    import sys

    from treant_client import cli, hooks_claude

    hook = json.loads(
        _guard(cfg_tmp, _work(cfg_tmp), FakeSession({"warnings": []}), gcfg=guard_cfg)
    )
    monkeypatch.setattr(hooks_claude, "pre_tool_use", lambda p: 1 / 0)
    payload = {"cwd": "/x", "tool_name": "Bash", "tool_input": {"command": "kubectl get po"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert cli.main(["hook", "claude-pre-tool-use"]) == 0
    fallback = json.loads(capsys.readouterr().out)
    for out in (hook, fallback):
        assert set(out) == {"hookSpecificOutput"}
        h = out["hookSpecificOutput"]
        assert set(h) == {"hookEventName", "permissionDecision", "permissionDecisionReason"}
        assert (h["hookEventName"], h["permissionDecision"]) == ("PreToolUse", "ask")
        assert isinstance(h["permissionDecisionReason"], str)


def test_off_mode_never_turns_allow_into_ask(cfg_tmp, guard_cfg):
    """`off` means nothing asks: memory warnings are not even looked up."""
    from dataclasses import replace

    off = replace(guard_cfg, modes={"work": "off"})
    s = FakeSession({"warnings": ["do not deploy"]})
    work = _work(cfg_tmp)
    assert _guard(cfg_tmp, work, s, command=LISTED, gcfg=off) is None
    assert _guard(cfg_tmp, work, s, gcfg=off) is None  # an unlisted prod context too
    assert s.calls == []
