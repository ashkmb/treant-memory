"""Kiro CLI adapters: start hook, block-once shell guard, idle-conversation sweep.

The start hook and the sweep fail open: any problem means no output / exit 0. The guard fails
closed for commands its rules would check (blocks once, like any other finding).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from treant_guard.config import load_config as load_guard_config

from treant_client import log
from treant_client.hooks_claude import (
    GUARD_BUDGET,
    HTTP_TIMEOUT,
    START_BUDGET,
    START_HTTP_TIMEOUT,
    _cfg,
    guard_command,
    start_context,
)
from treant_client.keychain import get_token
from treant_client.mcp_http import TreantSession
from treant_client.save import save_worker
from treant_client.summarize import default_state_dir
from treant_client.transcript import RESUME_MARKER, kiro_text, kiro_v3_text
from treant_client.workspaces import resolve

KIRO_DB = Path("~/Library/Application Support/kiro-cli/data.sqlite3").expanduser()
# Kiro 2.x with the v3 engine keeps each chat as <hash>/<id>/session.json + messages.jsonl here.
KIRO_SESSIONS = Path("~/.kiro/sessions").expanduser()
GUARD_TTL = 15 * 60.0
IDLE_MS = 30 * 60 * 1000
SHELL_TOOLS = ("shell", "execute_bash")  # Kiro 2.32 reports "shell"; the agent matcher accepts both
RETRY_HINT = " Re-run the same command to proceed."
DENY_SUFFIX = " Denied by the guard config; run it yourself if you mean it."
STATE_LOCK_WAIT = 0.5  # seconds; past it the ask blocks without being recorded
SINCE_FILE = "kiro-since"  # epoch ms; conversations last updated before it are never saved


def _read_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError, RecursionError):  # RecursionError: a deeply nested file
        return {}
    return data if isinstance(data, dict) else {}


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_state(path: Path, data: dict) -> None:
    _write_text(path, json.dumps(data))


def read_since(state_dir: str | Path) -> int | None:
    try:
        return int((Path(state_dir) / SINCE_FILE).read_text().strip())
    except (OSError, ValueError):
        return None


def ensure_since(state_dir: str | Path, now_ms: int) -> int:
    """The sweep watermark; written as ``now_ms`` when missing or unreadable, so saving starts
    now and history is never bulk-sent. Writing 0 into the file backfills."""
    since = read_since(state_dir)
    if since is None:
        _write_text(Path(state_dir) / SINCE_FILE, f"{now_ms}\n")
        since = now_ms
    return since


def resume_brief(cwd: str) -> str:
    """The start text for a resumed Kiro chat, which Kiro v3 opens without running agentSpawn.
    It goes in as the chat's first message, behind RESUME_MARKER so the sweep leaves it out.
    "" when the folder is unmapped or there is nothing to say."""
    text = kiro_start({}, cwd)
    if not text:
        return ""
    return (
        f"{RESUME_MARKER} This chat was resumed, so Kiro did not run the Treant start hook. "
        "This is context only: reply with one short line and wait for my next message."
        f"\n\n{text}"
    )


def kiro_start(
    payload: dict,
    cwd: str,
    cfg=None,
    *,
    token_getter=get_token,
    session_factory=TreantSession,
    run=subprocess.run,
    budget: float = START_BUDGET,
    http_timeout: float = START_HTTP_TIMEOUT,
) -> str:
    """Text for stdout (Kiro adds exit-0 stdout to the context): the workspace note, plus the Treant
    text when it answers; "" when unmapped."""
    if os.environ.get("TREANT_SUMMARIZER") == "1":
        return ""
    text = start_context(
        cwd,
        cwd,
        cfg,
        event="kiro_start",
        token_getter=token_getter,
        session_factory=session_factory,
        run=run,
        budget=budget,
        http_timeout=http_timeout,
    )
    return text or ""


def kiro_guard(
    payload: dict,
    cwd: str,
    now: float,
    cfg=None,
    *,
    guard_config_loader=load_guard_config,
    token_getter=get_token,
    session_factory=TreantSession,
    run=subprocess.run,
    budget: float = GUARD_BUDGET,
    http_timeout: float = HTTP_TIMEOUT,
    state_dir: str | Path | None = None,
) -> tuple[int, str]:
    """(exit code, stderr). The engine runs first. A deny blocks (2) every time. An ask blocks the
    first time a (cwd, command) gets it, then lets the identical retry through for 15 minutes;
    a guard timeout or error is an ask too."""
    if os.environ.get("TREANT_SUMMARIZER") == "1":
        return 0, ""
    if payload.get("tool_name") not in SHELL_TOOLS:
        return 0, ""
    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str):
        return 0, ""
    state_path = Path(state_dir or default_state_dir()) / "kiro-guard.json"
    key = cwd + "\0" + hashlib.sha256(command.encode("utf-8", "replace")).hexdigest()

    def fresh() -> bool:
        # the identical retry of an ask passes whatever the server says: no warnings lookup
        return key not in _live_state(state_path, now)

    action, reason = guard_command(
        command,
        cwd=cwd,
        project=cwd,
        cfg=cfg,
        event="kiro_guard",
        guard_config_loader=guard_config_loader,
        token_getter=token_getter,
        session_factory=session_factory,
        run=run,
        budget=budget,
        http_timeout=http_timeout,
        want_warnings=fresh,
    )
    if action == "allow":
        return 0, ""
    if not reason:
        reason = "Treant guard: denied" if action == "deny" else "Treant guard: check this command"
    if action == "deny":  # never stored, never passed on retry
        return 2, reason + DENY_SUFFIX
    try:
        if _record_ask(state_path, key, now):
            return 0, ""  # the identical retry of an ask
    except OSError as e:  # still block: without the state the retry blocks again
        ws = None
        with contextlib.suppress(Exception):
            ws = resolve(cwd, _cfg(cfg))
        log.event("kiro_guard_state", workspace=ws, status="error", category=type(e).__name__)
    return 2, reason + RETRY_HINT


def _live_state(path: Path, now: float) -> dict:
    """The block-once state without expired or malformed entries."""
    return {
        k: v
        for k, v in _read_state(path).items()
        if isinstance(v, (int, float)) and now - v < GUARD_TTL
    }


def _record_ask(state_path: Path, key: str, now: float) -> bool:
    """True when ``key`` is a live retry; else records it and returns False. Holds a lock so two
    hooks at once do not drop each other's entries; waits at most STATE_LOCK_WAIT (OSError)."""
    state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(state_path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + STATE_LOCK_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("state lock") from None
                time.sleep(0.02)
        state = _live_state(state_path, now)
        if key in state:
            return True
        state[key] = now
        _write_state(state_path, state)
        return False
    finally:
        os.close(fd)  # releases the lock


def _open_ro(path: str | Path) -> sqlite3.Connection:
    # as_uri() percent-escapes ?, # and %, which a plain f"file:{path}" would read as URI syntax
    return sqlite3.connect(f"{Path(path).absolute().as_uri()}?mode=ro", uri=True, timeout=5)


def _load_value(db: str | Path, key: str, cid: str):
    """One conversation's value, read only for a row that is about to be saved."""
    conn = _open_ro(db)
    try:
        row = conn.execute(
            "SELECT value FROM conversations_v2 WHERE key = ? AND conversation_id = ?",
            (key, cid),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _iso_ms(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    return int(when.timestamp() * 1000)


def _v3_sessions(root: Path) -> list[tuple[str, str, int, Path]]:
    """(id, folder, last modified ms, messages path) of each readable v3 session under ``root``;
    a session whose session.json is unreadable or lacks a folder or time is skipped and logged."""
    out = []
    for meta in sorted(root.glob("*/*/session.json")):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            sid, paths = data.get("id") or meta.parent.name, data.get("workspacePaths")
            updated = _iso_ms(data.get("lastModifiedAt"))
            if not (isinstance(sid, str) and isinstance(paths, list) and paths):
                raise ValueError("session")
            if not isinstance(paths[0], str) or updated is None:
                raise ValueError("session")
        except Exception as e:  # noqa: BLE001
            log.event("kiro_sweep", status="error", category=type(e).__name__)
            continue
        out.append((sid, paths[0], updated, meta.parent / "messages.jsonl"))
    return out


def kiro_sweep(
    now_ms: int,
    cfg=None,
    *,
    db_path: str | Path | None = None,
    sessions_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
    save=save_worker,
    **save_kwargs,
) -> list[str]:
    """Save Kiro conversations idle for 30 minutes, last updated at or after the kiro-since
    watermark, whose folder maps to a workspace. Both stores are read: the old
    ``conversations_v2`` table and the v3 engine's session folders; one failing does not stop
    the other. Returns the statuses; a failed save (or a row that could not be processed) is
    retried by the next sweep."""
    if os.environ.get("TREANT_SUMMARIZER") == "1":
        return []
    try:
        c = _cfg(cfg)
    except Exception:  # noqa: BLE001
        return []
    if c.url is None:
        return []
    state = Path(state_dir or default_state_dir())
    saved_path = state / "kiro-saved.json"
    saved = {k: v for k, v in _read_state(saved_path).items() if isinstance(v, int)}
    db = db_path or KIRO_DB
    try:
        since = ensure_since(state, now_ms)
    except OSError as e:
        log.event("kiro_sweep", status="error", category=type(e).__name__)
        return []
    idle_before = now_ms - IDLE_MS
    # (id, folder, updated ms, prepare); prepare() reads what it needs and returns the loader
    # save_worker calls for (text, user turns). It runs only for a conversation being saved.
    found: list[tuple[object, object, object, Callable[[], Callable[[], tuple[str, int]]]]] = []
    try:
        conn = _open_ro(db)
        try:
            rows = conn.execute(
                "SELECT key, conversation_id, updated_at FROM conversations_v2 "
                "WHERE updated_at >= ? AND updated_at <= ?",
                (since, idle_before),
            ).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as e:
        rows = []
        if Path(db).exists():
            log.event("kiro_sweep", status="error", category=type(e).__name__)
    for key, cid, updated in rows:
        found.append((cid, key, updated, _v2_loader(db, key, cid)))
    for sid, folder, updated, messages in _v3_sessions(Path(sessions_dir or KIRO_SESSIONS)):
        if since <= updated <= idle_before:
            found.append((sid, folder, updated, _v3_loader(messages)))

    def one(cid, key, updated, prepare) -> str | None:
        if not isinstance(key, str) or not isinstance(cid, str):
            raise TypeError("key")
        if type(updated) is not int:
            raise TypeError("updated_at")
        if updated < since or updated <= saved.get(cid, 0) or not resolve(key, c):
            return None
        status = save(
            session_id=cid, cwd=key, load=prepare(), trigger="kiro_idle", cfg=c, **save_kwargs
        )
        if status == "saved" or status.startswith("skipped_"):
            saved[cid] = updated
            _write_state(saved_path, saved)
        return status

    statuses: list[str] = []
    for item in found:
        # Each one on its own: one odd row (bad types, a NUL in the folder, a JSON bomb) is
        # logged by category, left unrecorded so it is retried, and never stops the others.
        try:
            status = one(*item)
        except Exception as e:  # noqa: BLE001
            log.event("kiro_sweep", status="error", category=type(e).__name__)
            continue
        if status is not None:
            statuses.append(status)
    return statuses


def _v2_loader(db, key, cid):
    """prepare() for a conversations_v2 row: reads and parses the value (raising if it is bad)."""

    def prepare():
        parsed = json.loads(_load_value(db, key, cid))
        return lambda: kiro_text(parsed)

    return prepare


def _v3_loader(messages: Path):
    """prepare() for a v3 session: the messages file is read by the loader itself."""
    return lambda: lambda: kiro_v3_text(messages)
