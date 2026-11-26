"""The ``treant-client`` command line: hooks, proxy, save worker, sweep, tokens, installer.

Also runnable as ``python -m treant_client.cli`` (the detached save worker uses that form).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path

CLAUDE_HOOKS = {
    "claude-session-start": "session_start",
    "claude-pre-tool-use": "pre_tool_use",
    "claude-session-end": "session_end",
    "claude-pre-compact": "pre_compact",
}
KIRO_HOOKS = ("kiro-start", "kiro-guard")

# Mirrors treant_guard's default pack; used only when the engine cannot run (it raised, or
# treant_guard / the hook modules failed to import), so a guarded command still asks.
_FALLBACK_GUARD_WORDS = (
    "kubectl",
    "helm",
    "aws",
    "terraform",
    "terragrunt",
    "git",
    "ansible-playbook",
    "wrangler",
)
_FALLBACK_RE = re.compile(
    r"(?<![\w.-])(?:" + "|".join(map(re.escape, _FALLBACK_GUARD_WORDS)) + r")(?![\w.-])"
)
_GUARD_ERROR = "Treant guard: guard error"
_RETRY_HINT = " Re-run the same command to proceed."  # kiro.RETRY_HINT, without the import
_KIRO_SHELL_TOOLS = ("shell", "execute_bash")  # kiro.SHELL_TOOLS, without the import


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="treant-client", description="Treant memory client adapters")
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser(
        "hook", help="run a hook (stdin JSON in; the guard fails closed, the rest fail open)"
    )
    # Not argparse choices: an unknown name would exit 2, which the host reads as "block".
    h.add_argument("name", help=", ".join([*CLAUDE_HOOKS, *KIRO_HOOKS]))

    mp = sub.add_parser("mcp-proxy", help="stdio MCP proxy to the Treant server")
    mp.add_argument(
        "--client",
        choices=["claude", "kiro"],
        default="claude",
        help="kiro: ignore an inherited CLAUDE_PROJECT_DIR (Kiro never sets it)",
    )
    sub.add_parser("kiro-sweep", help="save idle Kiro conversations")
    sub.add_parser("kiro-brief", help="print the start briefing for a resumed Kiro chat")

    w = sub.add_parser("save-worker", help="(internal) summarize and save one session")
    w.add_argument("--source", required=True, choices=["claude"])
    w.add_argument("--session-id", required=True)
    w.add_argument("--cwd", required=True)
    w.add_argument("--path", required=True)
    w.add_argument("--trigger", required=True)

    t = sub.add_parser("token", help="manage workspace tokens in the Keychain")
    tsub = t.add_subparsers(dest="token_cmd", required=True)
    ts = tsub.add_parser("set", help="prompt for and store a workspace token")
    ts.add_argument("workspace")
    tsub.add_parser("check", help="show which workspace tokens are set and whether they work")

    cs = sub.add_parser(
        "check-stale", help="flag repository facts whose files or names are gone from HEAD"
    )
    cs.add_argument("--repo", metavar="PATH", help="a folder in the repository (default: here)")
    cs.add_argument("--dry-run", action="store_true", help="print what would be flagged")

    for name in ("install", "uninstall"):
        i = sub.add_parser(name, help=f"{name} the hooks, MCP entries and launchd job")
        i.add_argument("--dry-run", action="store_true", help="print diffs, change nothing")
    return p


def _read_payload() -> dict | None:
    try:
        data = json.loads(sys.stdin.read())
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _guard_fallback(name: str, payload: dict) -> int:
    """The guard engine could not run: fail closed for a command it would have checked."""
    if name not in ("claude-pre-tool-use", "kiro-guard"):
        return 0
    if os.environ.get("TREANT_SUMMARIZER") == "1":
        return 0
    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or not _FALLBACK_RE.search(command):
        return 0
    tool = payload.get("tool_name")
    if name == "claude-pre-tool-use" and tool == "Bash":
        sys.stdout.write(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "ask",
                        "permissionDecisionReason": _GUARD_ERROR,
                    }
                }
            )
        )
        sys.stdout.flush()
    elif name == "kiro-guard" and tool in _KIRO_SHELL_TOOLS:
        sys.stderr.write(_GUARD_ERROR + _RETRY_HINT)
        sys.stderr.flush()
        return 2
    return 0


def _hook(name: str) -> int:
    """Never raises. Claude hooks return 0, kiro-guard returns kiro_guard's code. The memory
    hooks fail open; the two guard hooks fail closed when the engine cannot run. An unknown
    name (settings written by a newer or older binary) does nothing and returns 0."""
    if name not in CLAUDE_HOOKS and name not in KIRO_HOOKS:
        with contextlib.suppress(BaseException):
            sys.stdin.read()  # drain what the host sends, as a known hook would
            from treant_client import log

            log.event("hook_unknown", status="unknown_name")
        return 0
    code = 0
    try:
        payload = _read_payload()  # before the guarded try: the fallback needs the command
    except BaseException:  # noqa: BLE001
        return 0
    if payload is None:
        return 0
    try:
        if name in CLAUDE_HOOKS:
            from treant_client import hooks_claude

            out = getattr(hooks_claude, CLAUDE_HOOKS[name])(payload)
            if out:
                sys.stdout.write(out)
        else:
            from treant_client import kiro

            cwd = payload.get("cwd")
            if not isinstance(cwd, str) or not cwd:
                cwd = os.getcwd()
            if name == "kiro-start":
                out = kiro.kiro_start(payload, cwd)
                if out:
                    sys.stdout.write(out)
            else:
                code, err = kiro.kiro_guard(payload, cwd, time.time())
                if err:
                    sys.stderr.write(err)
        sys.stdout.flush()
        sys.stderr.flush()
    except BaseException:  # noqa: BLE001 - a hook must never break the host tool
        if code == 2 and name == "kiro-guard":
            return 2  # kiro_guard decided; only writing its message failed
        try:
            return _guard_fallback(name, payload)
        except BaseException:  # noqa: BLE001
            return 0
    return code


def _save_worker(a: argparse.Namespace) -> int:
    with contextlib.suppress(Exception):
        from treant_client.save import save_worker
        from treant_client.transcript import claude_text

        path = a.path
        save_worker(a.session_id, a.cwd, lambda: claude_text(Path(path)), a.trigger)
    return 0


def _token_set(ws: str) -> int:
    from treant_client.config import load_config
    from treant_client.keychain import set_token

    known = sorted(set(load_config().folders.values()))
    if ws not in known:
        print(f"unknown workspace {ws!r}; known: {', '.join(known)}", file=sys.stderr)
        return 1
    return 0 if set_token(ws) else 1


def _token_check() -> int:
    from treant_client.config import load_config
    from treant_client.keychain import get_token
    from treant_client.mcp_http import TreantError, TreantSession

    cfg = load_config()
    if cfg.url is None:
        print("no url configured in ~/.config/treant/config.toml", file=sys.stderr)
        return 1
    for ws in sorted(set(cfg.folders.values())):
        token = get_token(ws)
        if not token:
            print(f"{ws}: missing")
            continue
        try:
            with TreantSession(cfg.url, token, 5.0) as s:
                s.call_tool("brief", {})
            status = "ok"
        except TreantError as e:
            status = str(e.category)
        except Exception as e:  # noqa: BLE001
            status = type(e).__name__
        print(f"{ws}: set {status}")
    return 0


def _check_stale(a: argparse.Namespace) -> int:
    from treant_client import stale
    from treant_client.config import load_config

    path = a.repo or os.getcwd()
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001
        print("check-stale: failed_config", file=sys.stderr)
        return 1
    res = stale.run(path, cfg, dry_run=a.dry_run, use_memo=False)
    if res.status not in ("ok", "stopped_budget"):
        print(f"check-stale: {res.status}", file=sys.stderr)
        return 1
    if a.dry_run:
        for doubt in res.in_doubt:
            for m in doubt.missing:
                why = (
                    f"removed in {m['commit']}: {m['subject'] or ''}".rstrip(": ")
                    if m["commit"]
                    else "not found"
                )
                print(f"would flag {doubt.fact_id}: {m['token']} ({why})")
        print(f"checked {res.checked} facts, {len(res.in_doubt)} in doubt (dry run)")
    else:
        print(f"checked {res.checked} facts, flagged {res.flagged}")
    if res.status == "stopped_budget":
        print("check-stale: stopped_budget (not every fact was checked)", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    a = _build_parser().parse_args(argv)
    if a.cmd == "hook":
        return _hook(a.name)
    if a.cmd == "mcp-proxy":
        from treant_client.proxy import main as proxy_main

        return proxy_main(a.client)
    if a.cmd == "save-worker":
        return _save_worker(a)
    if a.cmd == "kiro-sweep":
        from treant_client.kiro import kiro_sweep

        with contextlib.suppress(Exception):
            kiro_sweep(int(time.time() * 1000))
        return 0
    if a.cmd == "kiro-brief":
        with contextlib.suppress(Exception):  # fail open: the chat starts without a briefing
            from treant_client import kiro

            sys.stdout.write(kiro.resume_brief(os.getcwd()))
            sys.stdout.flush()
        return 0
    if a.cmd == "check-stale":
        return _check_stale(a)
    if a.cmd == "token":
        return _token_set(a.workspace) if a.token_cmd == "set" else _token_check()
    from treant_client import installer

    fn = installer.install if a.cmd == "install" else installer.uninstall
    return fn(dry_run=a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
