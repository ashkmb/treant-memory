"""Detached save worker: load, scrub, summarize, scrub, save_session. Never raises."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import time
from collections.abc import Callable
from pathlib import Path

from treant_client import config as _config
from treant_client import log
from treant_client import stale as _stale
from treant_client.keychain import get_token
from treant_client.mcp_http import TreantError, TreantSession
from treant_client.scrub import sanitize
from treant_client.summarize import SummarizerFailed, summarize
from treant_client.workspaces import resolve

DEFAULT_LOCK_DIR = "~/.local/state/treant/locks"
MIN_USER_TURNS = 2
MIN_CHARS = 1500
MAX_SESSION_ID = 200
SAVE_HTTP_TIMEOUT = 30.0
# Another worker for the same session holds the lock for one summarize + save (5 min summarizer
# timeout plus HTTP); past this the holder is stuck and this save gives up as failed_lock.
LOCK_WAIT = 600.0
LOCK_POLL = 0.2


def _lock_dir(lock_dir: str | Path | None) -> Path:
    return Path(lock_dir or os.environ.get("TREANT_LOCK_DIR") or DEFAULT_LOCK_DIR).expanduser()


def _acquire(session_id: str, lock_dir: str | Path | None, wait: float = LOCK_WAIT) -> int:
    """Wait up to ``wait`` seconds for this session's exclusive flock; returns the descriptor
    (TimeoutError, an OSError, when the wait runs out). Lock files are never deleted: removing
    one while another worker waits on it breaks the lock."""
    d = _lock_dir(lock_dir)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = hashlib.sha256(session_id.encode("utf-8", "replace")).hexdigest() + ".lock"
    fd = os.open(d / name, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + wait
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("lock") from None
                time.sleep(LOCK_POLL)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _pipeline(
    ws,
    terms,
    url,
    load,
    session_id,
    token_getter,
    session_factory,
    summarizer,
    sanitizer,
    http_timeout,
) -> str:
    token = token_getter(ws)
    if not token:
        return "skipped_no_token"
    try:
        text, user_turns = load()
    except Exception:  # noqa: BLE001
        return "failed_load"
    if user_turns < MIN_USER_TURNS or len(text) < MIN_CHARS:
        return "skipped_small"
    # The transcript is always scrubbed before the summarizer sees it, but a long session
    # trips the removal threshold easily, so only the summary is held to the confidence gate.
    try:
        sections = summarizer(sanitizer(text, terms).text)
    except SummarizerFailed:
        return "failed_summarize"  # not "nothing to save": the next attempt may succeed
    if not sections:
        return "skipped_empty_summary"
    scrubbed: dict[str, str] = {}
    unsure = False
    for key, value in sections.items():
        s = sanitizer(value, terms)
        unsure = unsure or not s.confident  # the server quarantines it for the owner to review
        scrubbed[key] = s.text
    args: dict = {"client_session_id": session_id, "sections": scrubbed}
    if unsure:
        args["unsure"] = True
    with session_factory(url, token, http_timeout) as sess:
        res = sess.call_tool("save_session", args)
    if not isinstance(res, dict) or "revision" not in res:
        return "failed_protocol"
    return "saved_unsure" if unsure else "saved"


def save_worker(
    session_id: str,
    cwd: str,
    load: Callable[[], tuple[str, int]],
    trigger: str,
    cfg=None,
    *,
    token_getter=get_token,
    session_factory=TreantSession,
    summarizer=summarize,
    sanitizer=sanitize,
    lock_dir: str | Path | None = None,
    http_timeout: float = SAVE_HTTP_TIMEOUT,
    lock_wait: float = LOCK_WAIT,
) -> str:
    """Save one session as a Treant revision. Returns a status string, logs only that."""
    t0 = time.monotonic()
    ws: str | None = None
    conf = None  # the loaded config, for the stale check after a save

    def run() -> str:
        nonlocal ws, conf
        if not isinstance(session_id, str) or not 0 < len(session_id) <= MAX_SESSION_ID:
            return "failed_invalid"
        try:
            c = cfg if cfg is not None else _config.load_config()
        except Exception:  # noqa: BLE001
            return "failed_config"
        conf = c
        if c.url is None:
            return "skipped_no_url"
        ws = resolve(cwd, c)
        if not ws:
            return "skipped_unmapped"
        try:
            fd = _acquire(session_id, lock_dir, lock_wait)
        except OSError:
            return "failed_lock"
        try:
            return _pipeline(
                ws,
                c.scrub_host_terms.get(ws, []),
                c.url,
                load,
                session_id,
                token_getter,
                session_factory,
                summarizer,
                sanitizer,
                http_timeout,
            )
        finally:
            os.close(fd)  # closing the descriptor releases the lock

    try:
        status = run()
    except TreantError as e:
        status = f"failed_{e.category}"
    except Exception:  # noqa: BLE001 - the worker must never raise
        status = "failed_error"
    log.event(
        "save_worker",
        workspace=ws,
        status=status,
        trigger=trigger,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )
    if status in ("saved", "saved_unsure"):
        with contextlib.suppress(Exception):  # the check never changes the save's status
            _stale.check_after_save(
                cwd, conf, token_getter=token_getter, session_factory=session_factory
            )
    return status
