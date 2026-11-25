"""Map a working directory to a Treant workspace by longest folder match."""

from __future__ import annotations

import os
from pathlib import Path


def _parts(path: str) -> tuple[str, ...]:
    return Path(os.path.realpath(path)).parts


def match(cwd: str, folders: dict[str, str]) -> tuple[str, str] | None:
    """(workspace, matched folder key) by longest prefix on whole path components."""
    if not cwd:
        return None
    target = _parts(cwd)
    best: tuple[int, str, str] | None = None
    for folder, workspace in folders.items():
        root = _parts(folder)
        if target[: len(root)] == root and (best is None or len(root) > best[0]):
            best = (len(root), workspace, folder)
    return (best[1], best[2]) if best else None


def resolve(cwd: str, folders: dict[str, str]) -> str | None:
    m = match(cwd, folders)
    return m[0] if m else None
