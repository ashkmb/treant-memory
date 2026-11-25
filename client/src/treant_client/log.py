"""Privacy-preserving event log: whitelisted fields only, never raises."""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

DEFAULT_LOG_DIR = "~/Library/Logs/treant"
ALLOWED = ("workspace", "status", "duration_ms", "category", "trigger", "count")


def _log_dir() -> Path:
    return Path(os.environ.get("TREANT_CLIENT_LOG_DIR") or DEFAULT_LOG_DIR).expanduser()


def event(name: str, **fields) -> None:
    with contextlib.suppress(Exception):  # logging must never break a hook
        record = {"ts": int(time.time()), "event": name}
        record.update({k: v for k, v in fields.items() if k in ALLOWED})
        d = _log_dir()
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(d / "client.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            fh = os.fdopen(fd, "a")
        except BaseException:
            os.close(fd)
            raise
        with fh:
            fh.write(json.dumps(record) + "\n")
