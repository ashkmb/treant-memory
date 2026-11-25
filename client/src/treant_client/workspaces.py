"""Map a working directory to a Treant workspace by longest folder match."""

from __future__ import annotations

from treant_guard import workspaces as _guard_workspaces

from treant_client.config import Config


def resolve(cwd: str, cfg: Config) -> str | None:
    return _guard_workspaces.resolve(cwd, cfg.folders)


def match_folder(cwd: str, cfg: Config) -> tuple[str, str] | None:
    """(workspace, matched folder key); the same match the guard enforces."""
    return _guard_workspaces.match(cwd, cfg.folders)
