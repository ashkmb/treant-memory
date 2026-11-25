"""Where a workspace's allow, deny and protect lists come from."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from treant_guard.config import GuardConfig

Lists = dict[str, list[str]]  # kind -> values, for one workspace


class TargetSource(Protocol):
    """Where ``engine.check`` reads a workspace's lists. The engine reads allow from
    ``lists`` and deny only from ``deny_parts``; ``lists``' deny is the merged view for other
    callers."""

    def lists(self, workspace: str) -> tuple[Lists, Lists]:
        """(allow, deny) for the workspace; deny already includes the ``"*"`` entries."""
        ...

    def deny_parts(self, workspace: str) -> tuple[Lists, Lists]:
        """(workspace deny, ``"*"`` deny) unmerged, so a hit can be told apart; the engine's
        only source of deny."""
        ...

    def protected(self, workspace: str) -> Lists:
        """Production targets for the workspace, ``"*"`` entries merged in."""
        ...

    def others(self, workspace: str) -> dict[str, Lists]:
        """Every other workspace's allow lists: a value listed there belongs to it."""
        ...


class LocalTargets:
    """Lists read from the user's guard config."""

    def __init__(self, cfg: GuardConfig) -> None:
        self._cfg = cfg

    def lists(self, workspace: str) -> tuple[Lists, Lists]:
        allow = {k: list(v) for k, v in self._cfg.allow.get(workspace, {}).items()}
        return allow, _merged(self._cfg.deny, workspace)

    def protected(self, workspace: str) -> Lists:
        return _merged(self._cfg.protect, workspace)

    def others(self, workspace: str) -> dict[str, Lists]:
        return {
            ws: {k: list(v) for k, v in kinds.items()}
            for ws, kinds in self._cfg.allow.items()
            if ws != workspace
        }

    def deny_parts(self, workspace: str) -> tuple[Lists, Lists]:
        own = {k: list(v) for k, v in self._cfg.deny.get(workspace, {}).items()}
        star = {k: list(v) for k, v in self._cfg.deny.get("*", {}).items()}
        return own, star


def _merged(table: dict[str, Lists], workspace: str) -> Lists:
    out: Lists = {}
    for key in (workspace, "*"):
        for kind, values in table.get(key, {}).items():
            bucket = out.setdefault(kind, [])
            bucket.extend(v for v in values if v not in bucket)
    return out
