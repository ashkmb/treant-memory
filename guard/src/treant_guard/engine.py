"""Decision engine: deny what the config denies, ask what it protects, else allow.

Protect mode (the default) asks for a protected target and for a target another workspace's
allow list names; anything else runs, a target it can't tell included. Production mode asks
only for a protected target. Off mode allows everything, deny lists included. A workspace can
override the mode ([guard.modes]). Allowlist mode asks for every target the workspace does not
allow and for one it can't tell. The other modes deny what the config denies and ask for what
it protects.

Failure is closed: an invalid config or an internal error asks for every command the
prefilter matches.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from treant_guard import workspaces
from treant_guard.config import GuardConfig
from treant_guard.resolve import host_of, targets_for
from treant_guard.rules import Rule, Switch, default_rules, default_switches
from treant_guard.targets import LocalTargets, TargetSource


@dataclass(frozen=True)
class Decision:
    action: str  # "allow" | "ask" | "deny"
    workspace: str | None
    not_allowed: list[tuple[str, str]]
    unknown: list[str]
    denied: list[tuple[str, str, bool]]  # (kind, value, everywhere)
    matched: bool
    reason: str | None
    protected: list[tuple[str, str]] = field(default_factory=list)
    foreign: list[tuple[str, str, tuple[str, ...]]] = field(default_factory=list)  # + owners


def prefilter(rules: list[Rule], switches: Iterable[Switch] = ()) -> Callable[[str], bool]:
    """Cheap check: does the line mention any rule (or switch) command as a whole shell word?"""
    names = sorted(
        {os.path.basename(c) for r in rules for c in r.command}
        | {os.path.basename(c) for sw in switches for c in sw.command}
    )
    if not names:
        return lambda command: False
    pattern = re.compile(r"(?<![\w.-])(?:" + "|".join(map(re.escape, names)) + r")(?![\w.-])")
    return lambda command: pattern.search(command) is not None


def default_prefilter() -> Callable[[str], bool]:
    """The prefilter for when the config is invalid or never loaded: the shipped pack."""
    return prefilter(default_rules(), default_switches())


def prefilter_for(config: GuardConfig) -> Callable[[str], bool]:
    """The prefilter ``check`` uses for ``config``: rule and switch commands (``kubectx prod``
    alone is checked); the shipped pack when the config is invalid."""
    if config.error:
        return default_prefilter()
    return prefilter(config.rules, config.switches)


def format_reason(
    ws: str,
    denied: list[tuple[str, str, bool]],
    not_allowed: list[tuple[str, str]],
    unknown: list[str],
    *,
    protected: list[tuple[str, str]] = (),
    foreign: list[tuple[str, str, tuple[str, ...]]] = (),
) -> str | None:
    parts = []
    here = ", ".join(f"{k}={v}" for k, v, everywhere in denied if not everywhere)
    if here:
        parts.append(f"denied in {ws}: {here}")
    star = ", ".join(f"{k}={v}" for k, v, everywhere in denied if everywhere)
    if star:
        parts.append(f"denied everywhere: {star}")
    if protected:
        parts.append("protected: " + ", ".join(f"{k}={v}" for k, v in protected))
    by_owner: dict[tuple[str, ...], list[str]] = {}
    for k, v, owners in foreign:
        by_owner.setdefault(owners, []).append(f"{k}={v}")
    for owners, listed in by_owner.items():
        parts.append(f"belongs to {', '.join(owners)}: {', '.join(listed)}")
    if not_allowed:
        listed = ", ".join(f"{k}={v}" for k, v in not_allowed)
        parts.append(f"not allowed in {ws}: {listed}")
    if unknown:
        parts.append(f"can't tell which {', '.join(unknown)} this uses")
    if not parts:
        return None
    return f"Treant guard ({ws}): " + "; ".join(parts)


def _expand(path: str, home: str) -> str:
    return home + path[1:] if path == "~" or path.startswith("~/") else path


def _norm(path: str, home: str) -> str:
    return os.path.normpath(_expand(path, home))


def _real(path: str, home: str) -> str:
    return os.path.realpath(_expand(path, home))


def _under(value: str, listed: list[str], home: str) -> bool:
    """``value`` is a listed folder or inside one, by whole path components."""
    v = _norm(value, home)
    for entry in listed:
        base = _norm(entry, home)
        if v == base or v.startswith(base if base.endswith("/") else base + "/"):
            return True
    return False


def _is_in(value: str, listed: list[str], prefix: bool, home: str) -> bool:
    """Allowed: listed; for a path, inside a listed folder both as written and as a real
    path, so a link inside an allowed folder is judged by where it leads."""
    if not prefix:
        return value in listed
    return _under(value, listed, home) and _under(
        _real(value, home), [_real(e, home) for e in listed], home
    )


def _is_denied(value: str, listed: list[str], prefix: bool, home: str) -> bool:
    """Like `_is_in`, but for a path either the plain or the resolved form matching is
    enough, so a symlink cannot hide a denied folder."""
    if not prefix:
        return value in listed
    values = {value, _real(value, home)}
    entries = listed + [_real(e, home) for e in listed]
    return any(_under(v, entries, home) for v in values)


def _folder(kinds: set[str]) -> Callable[[Mapping[str, list[str]]], dict[str, list[str]]]:
    """A function that lower-cases the values of ``kinds`` in a lists table."""

    def fold(lists: Mapping[str, list[str]]) -> dict[str, list[str]]:
        return {k: [v.lower() for v in vs] if k in kinds else list(vs) for k, vs in lists.items()}

    return fold


def check(
    command: str,
    *,
    cwd: str,
    workspace_dir: str,
    environ: Mapping[str, str],
    config: GuardConfig,
    source: TargetSource | None = None,
    run=subprocess.run,
    home: str | None = None,
) -> Decision:
    if config.error:
        if prefilter_for(config)(command):
            reason = f"Treant guard: guard config invalid: {config.error}"
            return Decision("ask", None, [], [], [], True, reason)
        return Decision("allow", None, [], [], [], False, None)
    if not prefilter_for(config)(command):
        return Decision("allow", None, [], [], [], False, None)
    ws: str | None = None
    home = os.path.expanduser("~") if home is None else home
    try:
        ws = workspaces.resolve(workspace_dir, config.workspaces)
        if ws is None:
            return Decision("allow", None, [], [], [], True, None)
        mode = config.modes.get(ws, config.mode)
        if mode == "off":  # the owner turned the guard off here: nothing asks or refuses
            return Decision("allow", ws, [], [], [], True, None)
        protect_mode = mode in ("protect", "production")  # can't tell and unlisted run
        cross_workspace = mode == "protect"  # another workspace's target asks
        words: list[tuple[str, str]] = []
        matched, targets, unresolved = targets_for(
            command,
            cwd=cwd,
            environ=environ,
            rules=config.rules,
            switches=tuple(config.switches),
            run=run,
            home=home,
            switch_words=words,
        )
        if unresolved == ["command"]:
            if protect_mode:  # can't tell: runs
                return Decision("allow", ws, [], ["command"], [], True, None)
            return Decision(
                "ask", ws, [], [], [], True, f"Treant guard ({ws}): could not parse the command"
            )
        src = source or LocalTargets(config)
        host_kinds = {r.kind for r in config.rules if r.resolver == "git_push_host"}
        # hosts are read in lower case: fold the lists that refuse or ask, never the allow list
        fold = _folder(host_kinds)
        allow = src.lists(ws)[0]
        own, star = map(fold, src.deny_parts(ws))  # the one source of deny
        deny = {k: own.get(k, []) + star.get(k, []) for k in own.keys() | star.keys()}
        protect = fold(src.protected(ws))
        others = {o: fold(lists) for o, lists in src.others(ws).items()} if cross_workspace else {}
        prefix_kinds = {r.kind for r in config.rules if r.match == "path_prefix"}
        denied: list[tuple[str, str, bool]] = []
        protected: list[tuple[str, str]] = []
        foreign: list[tuple[str, str, tuple[str, ...]]] = []
        not_allowed: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for kind, value in targets:
            seen.add((kind, value))
            prefix = kind in prefix_kinds
            if _is_denied(value, deny.get(kind, []), prefix, home):
                in_ws = _is_denied(value, own.get(kind, []), prefix, home)
                denied.append(
                    (kind, value, not in_ws and _is_denied(value, star.get(kind, []), prefix, home))
                )
            elif _is_denied(value, protect.get(kind, []), prefix, home):
                protected.append((kind, value))
            elif _is_in(value, allow.get(kind, []), prefix, home):
                continue
            elif not protect_mode:
                not_allowed.append((kind, value))
            else:
                owners = tuple(
                    o
                    for o, lists in others.items()
                    if _is_denied(value, lists.get(kind, []), prefix, home)
                )
                if owners:
                    foreign.append((kind, value, owners))
        # words given to a switch (``kubectx prod``, a remote URL): they can only add a finding;
        # an unlisted word is none in either mode, the command reading the kind decides that
        for kind, word in words:
            values = [word]
            host = host_of(word) if kind in host_kinds else None
            if host and host != word:
                values.append(host)
            for value in values:
                if (kind, value) in seen:
                    continue
                seen.add((kind, value))
                prefix = kind in prefix_kinds
                if _is_denied(value, deny.get(kind, []), prefix, home):
                    in_ws = _is_denied(value, own.get(kind, []), prefix, home)
                    everywhere = not in_ws and _is_denied(value, star.get(kind, []), prefix, home)
                    denied.append((kind, value, everywhere))
                elif _is_denied(value, protect.get(kind, []), prefix, home):
                    protected.append((kind, value))
                elif protect_mode and not _is_in(value, allow.get(kind, []), prefix, home):
                    owners = tuple(
                        o
                        for o, lists in others.items()
                        if _is_denied(value, lists.get(kind, []), prefix, home)
                    )
                    if owners:
                        foreign.append((kind, value, owners))
        unknown = [] if protect_mode else unresolved
        reason = format_reason(
            ws, denied, not_allowed, unknown, protected=protected, foreign=foreign
        )
        action = "deny" if denied else "ask" if reason else "allow"
        return Decision(
            action, ws, not_allowed, unresolved, denied, matched, reason, protected, foreign
        )
    except Exception:  # noqa: BLE001 - fail closed: any internal error asks
        return Decision("ask", ws, [], [], [], True, "Treant guard: guard error")
