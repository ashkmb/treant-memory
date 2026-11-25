"""Client configuration: server URL, folder-to-workspace map, scrub terms, Kiro agent.

Every key is optional and the defaults are neutral; set them in ~/.config/treant/config.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from treant_guard.config import DEFAULT_CONFIG_DIR as GUARD_CONFIG_DIR
from treant_guard.config import GuardConfig
from treant_guard.config import load_config as load_guard_config

DEFAULT_CONFIG_PATH = Path("~/.config/treant/config.toml").expanduser()


@dataclass(frozen=True)
class Config:
    url: str | None = None  # None: memory is off
    folders: dict[str, str] = field(default_factory=dict)
    scrub_host_terms: dict[str, list[str]] = field(default_factory=dict)
    kiro_agent: str | None = None  # None: Kiro is not installed
    about: dict[str, str] = field(default_factory=dict)  # guard config [about]; {} if invalid


def _expand(folders: dict[str, str]) -> dict[str, str]:
    return {str(Path(p).expanduser()): w for p, w in folders.items()}


def load_config(
    path: Path = DEFAULT_CONFIG_PATH,
    guard_dir: Path = GUARD_CONFIG_DIR,
    *,
    guard: GuardConfig | None = None,
) -> Config:
    """``folders`` is the guard config's ``[workspaces]`` when that config is valid and has any;
    else ``config.toml``'s ``[folders]``, else none (no workspace, so no note and no memory).
    ``about`` is the guard config's ``[about]``, ``{}`` when that config is invalid. ``guard``
    is an already-loaded guard config, so a hook reads it only once."""
    data: dict = {}
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        pass
    g = guard if guard is not None else load_guard_config(guard_dir)
    if g.error is None and g.workspaces:
        folders = dict(g.workspaces)  # already expanded by the guard config
    else:
        folders = _expand(dict(data.get("folders", {})))
    terms = data.get("scrub_host_terms", {})
    return Config(
        url=data.get("url") or None,
        folders=folders,
        scrub_host_terms={k: list(v) for k, v in terms.items()},
        kiro_agent=data.get("kiro_agent") or None,
        about=dict(g.about) if g.error is None else {},
    )
