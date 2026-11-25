"""Claude Code SessionStart, PreToolUse, SessionEnd and PreCompact hooks.

Memory hooks fail open: any problem means no output. The guard (PreToolUse) decides locally
with treant-guard and fails closed: a command it would check asks when anything goes wrong.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace

from treant_guard.config import load_config as load_guard_config
from treant_guard.engine import Decision, check, default_prefilter, prefilter_for
from treant_guard.lexer import words

from treant_client import config as _config
from treant_client import log
from treant_client.keychain import get_token
from treant_client.mcp_http import TreantError, TreantSession
from treant_client.summarize import default_state_dir
from treant_client.workspaces import match_folder, resolve

START_BUDGET = 5.0
GUARD_BUDGET = 3.0
HTTP_TIMEOUT = 2.0
START_HTTP_TIMEOUT = 4.0
SAVE_HOOK_BUDGET = 0.8  # SessionEnd/PreCompact must return in under 1 s
WARNINGS_MIN_LEFT = 0.2  # skip the warnings lookup with less budget than this left
WARNINGS_MARGIN = 0.1
GUARD_TIMED_OUT = "Treant guard: guard timed out"
GUARD_ERROR = "Treant guard: guard error"


def start_text(ws: str, brief: str) -> str:
    text = (
        f"Treant memory is on for workspace {ws}. Use the treant MCP tools: "
        "search before acting on a named system, save_session when asked to save."
    )
    return text + "\n\n" + brief if brief else text


def _tilde(path: str) -> str:
    home = os.path.expanduser("~")
    if path == home or path.startswith(home.rstrip("/") + "/"):
        return "~" + path[len(home) :]
    return path


def workspace_note(ws: str, folder: str, cfg) -> str:
    """The session-start note: workspace, matched folder key (home as ``~``) and [about] text."""
    note = f"Workspace: {ws} (folder {_tilde(folder)})."
    about = cfg.about.get(ws)
    return f"{note}\n{about}" if about else note


def _with_deadline(fn, seconds: float):
    """Run fn in a daemon thread; (True, value) or (False, None) on timeout or exception."""
    box: dict = {}

    def work():
        try:
            box["v"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        return False, "timeout"
    if "e" in box:
        return False, box["e"]
    return True, box.get("v")


def _category(err) -> str:
    if isinstance(err, TreantError):
        return err.category
    return err if isinstance(err, str) else "error"


def _cfg(cfg):
    return cfg if cfg is not None else _config.load_config()


def _project_dir(payload: dict) -> str:
    """The folder that picks the workspace: the session's project (Claude Code sets
    CLAUDE_PROJECT_DIR for hooks), else the payload cwd. The payload cwd follows every ``cd``
    in the session and could otherwise switch the session into another workspace."""
    cwd = payload.get("cwd")
    return os.environ.get("CLAUDE_PROJECT_DIR") or (cwd if isinstance(cwd, str) else "")


def git_toplevel(cwd: str, run=subprocess.run) -> str | None:
    try:
        r = run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip() or None


def _warnings(ws, command, c, token_getter, session_factory, timeout) -> list[str]:
    """Memory warnings for the command's words; [] on any problem (the local decision stands)."""
    try:
        if c.url is None:
            return []
        token = token_getter(ws)
        if not token:
            return []
        with session_factory(c.url, token, timeout) as s:
            res = s.call_tool("warnings", {"names": words(command)})
    except Exception:  # noqa: BLE001
        return []
    found = res.get("warnings") if isinstance(res, dict) else None
    return [w for w in found if isinstance(w, str)] if isinstance(found, list) else []


def _with_warnings(d: Decision, warnings: list[str]) -> tuple[str, str | None]:
    if not warnings:
        return (d.action, d.reason) if d.action != "allow" else ("allow", None)
    w = "warnings: " + " | ".join(warnings)
    if d.action != "allow" and d.reason:
        return d.action, f"{d.reason}; {w}"
    return "ask", f"Treant guard ({d.workspace}): {w}"


def guard_command(
    command: str,
    *,
    cwd: str,
    project: str,
    cfg,
    event: str,
    guard_config_loader,
    token_getter,
    session_factory,
    run,
    budget: float,
    http_timeout: float,
    want_warnings=None,
) -> tuple[str, str | None]:
    """``(action, reason)``: ``("allow", None)``, ``("ask", reason)`` or ``("deny", reason)``. The
    workspace comes from ``project``; targets (terraform_dir, git remotes, project config files)
    from ``cwd``. Needs no token: only the optional warnings lookup does (skipped for a deny, and
    when ``want_warnings()`` says False). Fails closed for commands the rules would check."""
    t0 = time.monotonic()
    box: dict = {}

    def work():
        g = guard_config_loader()
        box["matched"] = prefilter_for(g)(command)
        if not box["matched"]:
            return "allow", None
        c = cfg if cfg is not None else _config.load_config(guard=g)
        if g.error is None:
            g = replace(g, workspaces=c.folders)  # one map for the guard and the memory hooks
        box["ws"] = resolve(project, c)  # for the log when check() does not finish
        d = check(command, cwd=cwd, workspace_dir=project, environ=os.environ, config=g, run=run)
        box["decision"] = d
        if d.action == "deny":
            return "deny", d.reason  # nothing for the server to add
        warnings: list[str] = []
        left = budget - (time.monotonic() - t0)
        # `off` means nothing asks: a memory warning must not turn its allow into an ask
        off = d.workspace is not None and g.modes.get(d.workspace, g.mode) == "off"
        wanted = d.matched and d.workspace and not off and left > WARNINGS_MIN_LEFT
        if wanted and (want_warnings is None or want_warnings()):
            timeout = min(http_timeout, left - WARNINGS_MARGIN)
            warnings = _warnings(d.workspace, command, c, token_getter, session_factory, timeout)
        return _with_warnings(d, warnings)

    ok, val = _with_deadline(work, budget)
    d = box.get("decision")
    if ok:
        if d is not None:  # unguarded commands (ls, cat, ...) are not logged
            log.event(event, workspace=d.workspace, status=val[0])
        return val
    if d is not None:  # the warnings lookup ran out of time or failed: the local decision stands
        log.event(event, workspace=d.workspace, status=d.action)
        return (d.action, d.reason) if d.action != "allow" else ("allow", None)
    matched = box.get("matched")
    if matched is None:  # the config never loaded: decide with the shipped rules
        try:
            matched = default_prefilter()(command)
        except Exception:  # noqa: BLE001
            matched = True
    status = "timeout" if val == "timeout" else "error"
    log.event(event, workspace=box.get("ws"), status=status, category=_category(val))
    if not matched:
        return "allow", None
    return "ask", GUARD_TIMED_OUT if status == "timeout" else GUARD_ERROR


def start_context(
    project: str,
    git_dir,
    cfg,
    *,
    event: str,
    token_getter,
    session_factory,
    run,
    budget: float,
    http_timeout: float,
) -> str | None:
    """The session-start text for Claude Code and Kiro: the workspace note for ``project``, plus
    the Treant brief (repo from ``git_dir``) when it answers; None when unmapped. Fails open."""
    box: dict = {}

    def work():
        # config load and realpath can stall (a hung mount), so they run inside the budget too
        c = _cfg(cfg)
        m = match_folder(project, c)
        if not m:
            return None
        ws = box["ws"] = m[0]
        box["note"] = workspace_note(ws, m[1], c)
        if c.url is None:
            return None  # memory is off; the note stands
        token = token_getter(ws)
        if not token:
            log.event(event, workspace=ws, status="no_token")
            return None
        top = git_toplevel(git_dir, run)
        args = {"repo": os.path.basename(top)} if top else {}
        with session_factory(c.url, token, http_timeout) as s:
            res = s.call_tool("brief", args)
        brief = res.get("brief") if isinstance(res, dict) else ""
        return start_text(ws, brief if isinstance(brief, str) else "")

    ok, val = _with_deadline(work, budget)
    ws = box.get("ws")
    if not ok:
        log.event(event, workspace=ws, status="error", category=_category(val))
        val = None
    elif val:
        log.event(event, workspace=ws, status="ok")
    note = box.get("note")
    if not note:
        return None
    return note + "\n\n" + val if val else note


def session_start(
    payload: dict,
    cfg=None,
    *,
    token_getter=get_token,
    session_factory=TreantSession,
    run=subprocess.run,
    budget: float = START_BUDGET,
    http_timeout: float = START_HTTP_TIMEOUT,
) -> str | None:
    if os.environ.get("TREANT_SUMMARIZER") == "1":
        return None
    project = _project_dir(payload)
    text = start_context(
        project,
        payload.get("cwd") or project,
        cfg,
        event="start_hook",
        token_getter=token_getter,
        session_factory=session_factory,
        run=run,
        budget=budget,
        http_timeout=http_timeout,
    )
    if not text:
        return None
    return json.dumps(
        {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
    )


def pre_tool_use(
    payload: dict,
    cfg=None,
    *,
    guard_config_loader=load_guard_config,
    token_getter=get_token,
    session_factory=TreantSession,
    run=subprocess.run,
    budget: float = GUARD_BUDGET,
    http_timeout: float = HTTP_TIMEOUT,
) -> str | None:
    if os.environ.get("TREANT_SUMMARIZER") == "1":
        return None
    if payload.get("tool_name") != "Bash":
        return None
    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str):
        return None
    project = _project_dir(payload)
    cwd = payload.get("cwd")
    action, reason = guard_command(
        command,
        # the command's own folder still decides terraform_dir, git remotes and the like
        cwd=cwd if isinstance(cwd, str) and cwd else project,
        project=project,
        cfg=cfg,
        event="guard_hook",
        guard_config_loader=guard_config_loader,
        token_getter=token_getter,
        session_factory=session_factory,
        run=run,
        budget=budget,
        http_timeout=http_timeout,
    )
    if action == "allow":
        return None
    if not reason:
        reason = "Treant guard: denied" if action == "deny" else "Treant guard: check this command"
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny" if action == "deny" else "ask",
                "permissionDecisionReason": reason,
            }
        }
    )


def detach(args: list[str], *, popen=subprocess.Popen) -> None:
    """Start ``python -P -m treant_client.cli <args>`` in its own session, fully detached.

    Never from the project folder: ``python -m`` would put it first on sys.path and a repo's
    own ``json.py`` or ``hashlib.py`` would run inside the worker (which reads the token).
    ``-P`` (safe path) and a neutral cwd each prevent that; the worker gets ``--cwd`` in argv.
    """
    folder = default_state_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    popen(
        [sys.executable, "-P", "-m", "treant_client.cli", *args],
        cwd=str(folder),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def _detach_save(payload: dict, cfg, trigger: str, popen, budget: float) -> None:
    if os.environ.get("TREANT_SUMMARIZER") == "1":
        return

    def work():
        sid = payload.get("session_id")
        path = payload.get("transcript_path")
        cwd = _project_dir(payload)
        if not all(isinstance(v, str) and v for v in (sid, path, cwd)):
            return
        c = _cfg(cfg)
        if c.url is None or not resolve(cwd, c):
            return
        detach(
            [
                "save-worker",
                "--source",
                "claude",
                "--session-id",
                sid,
                "--cwd",
                cwd,
                "--path",
                path,
                "--trigger",
                trigger,
            ],
            popen=popen,
        )

    ok, val = _with_deadline(work, budget)
    if not ok:
        log.event("save_hook", status="error", trigger=trigger, category=_category(val))


def session_end(
    payload: dict, cfg=None, *, popen=subprocess.Popen, budget: float = SAVE_HOOK_BUDGET
) -> None:
    return _detach_save(payload, cfg, "session_end", popen, budget)


def pre_compact(
    payload: dict, cfg=None, *, popen=subprocess.Popen, budget: float = SAVE_HOOK_BUDGET
) -> None:
    return _detach_save(payload, cfg, "pre_compact", popen, budget)
