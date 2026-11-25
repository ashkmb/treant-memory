"""User guard config: folder->workspace map, per-workspace allow/deny lists, about and rules."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from treant_guard.rules import (
    Rule,
    RuleError,
    Switch,
    default_rules,
    default_switches,
    merge_rules,
    parse_rules,
    parse_switches,
)

DEFAULT_CONFIG_DIR = Path("~/.config/treant").expanduser()
_TOP_LEVEL = {"guard", "workspaces", "allow", "deny", "protect", "about", "rule", "switch"}
MODES = ("protect", "allowlist", "production", "off")
_ABOUT_MAX = 2000
_ANY_WORKSPACE = "*"

# Old name -> new name, for the hint when a config still uses one; import translates kinds.
RENAMED_KINDS = {
    "git_host": "git_remote_host",
    "tf_root": "terraform_dir",
    "cf_account": "cloudflare_account",
}
_RENAMED_FIELDS = {
    "fallback": "default_from",
    "unresolved_if": "unknown_if",
    "multiple": "all_values",
    "short_attached": "short_flag",
}
_RENAMED_RESOLVERS = {"tf_root": "terraform_dir"}

ByWorkspace = dict[str, dict[str, list[str]]]  # workspace -> kind -> values


@dataclass(frozen=True)
class GuardConfig:
    rules: list[Rule]
    workspaces: dict[str, str]
    allow: ByWorkspace
    deny: ByWorkspace  # ``"*"`` stays its own key here
    about: dict[str, str]
    error: str | None = None  # the bare file path (or config dir) to blame
    error_detail: str | None = None  # a hint such as "tf_root was renamed to terraform_dir"
    switches: list[Switch] = field(default_factory=default_switches)
    protect: ByWorkspace = field(default_factory=dict)  # ``"*"`` stays its own key here
    # "protect": ask on protected targets and on another workspace's targets; "allowlist": ask
    # on unlisted ones too; "production": ask on protected targets only
    mode: str = "protect"
    modes: dict[str, str] = field(default_factory=dict)  # per-workspace override of ``mode``


class _Invalid(Exception):
    def __init__(self, path: Path, detail: str | None = None) -> None:
        super().__init__(str(path))
        self.path = str(path)
        self.detail = detail


class _Bad(Exception):
    """A validation failure; ``detail`` is the hint for the user, when there is one."""

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail


def is_trusted(path: Path, uid: int) -> bool:
    """Owned by `uid` and not group/world-writable, for the path and its parent (symlinks followed)."""
    for p in (path, path.parent):
        try:
            st = os.stat(p)
        except OSError:
            return False
        if st.st_uid != uid or st.st_mode & 0o022:
            return False
    return True


def _is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _valid_folder(key: str) -> bool:
    """No NUL, and ``~`` only as the user's own home (``~`` or ``~/...``), never ``~user``."""
    if "\x00" in key:
        return False
    return not key.startswith("~") or key == "~" or key.startswith("~/")


def _absolute(value: str) -> bool:
    return os.path.isabs(os.path.expanduser(value.strip()))


def _validate_lists(table: object, name: str) -> None:
    if not isinstance(table, dict):
        raise _Bad
    for ws, kinds in table.items():
        if not isinstance(kinds, dict) or (ws == _ANY_WORKSPACE and name == "allow"):
            raise _Bad
        for kind, values in kinds.items():
            if kind in RENAMED_KINDS:
                raise _Bad(f"{kind} was renamed to {RENAMED_KINDS[kind]}")
            if not _is_str_list(values):
                raise _Bad


def _validate_rules(raw: object) -> None:
    """Old rule field, kind and resolver names get the rename hint; the rest is parse_rules'."""
    if not isinstance(raw, list):
        return
    for rule in raw:
        if not isinstance(rule, dict):
            continue
        for old, new in _RENAMED_FIELDS.items():
            if old in rule:
                raise _Bad(f"{old} was renamed to {new}")
        for key, renamed in (("resolver", _RENAMED_RESOLVERS), ("kind", RENAMED_KINDS)):
            if rule.get(key) in renamed:
                raise _Bad(f"{rule[key]} was renamed to {renamed[rule[key]]}")


def _validate(data: dict) -> None:
    """Raises _Bad for anything that makes the file invalid."""
    if "targets" in data:
        raise _Bad("[targets] was renamed to [allow]")
    if set(data) - _TOP_LEVEL:
        raise _Bad
    ws = data.get("workspaces", {})
    if not isinstance(ws, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and _valid_folder(k) for k, v in ws.items()
    ):
        raise _Bad
    if _ANY_WORKSPACE in ws.values():
        raise _Bad(f'"{_ANY_WORKSPACE}" is not a workspace name: it means every workspace')
    _validate_lists(data.get("allow", {}), "allow")
    _validate_lists(data.get("deny", {}), "deny")
    _validate_lists(data.get("protect", {}), "protect")
    guard = data.get("guard", {})
    modes = guard.get("modes", {}) if isinstance(guard, dict) else None
    if (
        not isinstance(guard, dict)
        or set(guard) - {"mode", "modes"}
        or guard.get("mode", MODES[0]) not in MODES
        or not isinstance(modes, dict)
        or not all(isinstance(m, str) and m in MODES for m in modes.values())
        or _ANY_WORKSPACE in modes
    ):
        raise _Bad(
            f"[guard] takes mode = {' or '.join(map(repr, MODES))} and a [guard.modes] table"
            " of workspace = mode"
        )
    about = data.get("about", {})
    if not isinstance(about, dict) or not all(
        isinstance(v, str) and len(v) <= _ABOUT_MAX for v in about.values()
    ):
        raise _Bad
    _validate_rules(data.get("rule"))


def _files(config_dir: Path) -> list[Path]:
    files = [config_dir / "guard.toml"]
    guard_d = config_dir / "guard.d"
    if os.path.lexists(guard_d) and not guard_d.is_dir():  # a dangling link or a file
        raise _Invalid(guard_d)
    if guard_d.is_dir():
        files.append(guard_d)
        files.extend(sorted(guard_d.glob("*.toml")))
    return files


def _load_file(
    path: Path,
    uid: int,
    workspaces: dict[str, str],
    raw_allow: ByWorkspace,
    raw_deny: ByWorkspace,
    raw_protect: ByWorkspace,
    settings: dict[str, object],
    about: dict[str, str],
    user_rules: list[Rule],
    user_switches: list[Switch],
    seen: list[tuple[Path, str, str]],
    top: bool = True,
) -> None:
    if not os.path.lexists(path):
        return
    if not os.path.exists(path):  # dangling symlink: never a silent skip
        raise _Invalid(path)
    if not is_trusted(path, uid):
        raise _Invalid(path)
    if path.is_dir():
        return
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise _Invalid(path) from exc
    try:
        _validate(data)
    except _Bad as exc:
        raise _Invalid(path, exc.detail) from exc
    if "guard" in data and not top:  # one place decides the mode
        raise _Invalid(path, "[guard] belongs in guard.toml only")
    # guard.toml is read first, so its [guard.modes] is known here. A drop-in mapping a folder
    # into a workspace with its own mode would change that folder's mode from outside guard.toml.
    modes = settings.get("modes", {})
    if not top and any(ws in modes for ws in data.get("workspaces", {}).values()):
        raise _Invalid(path, "folders of a workspace in [guard.modes] belong in guard.toml only")
    try:
        user_rules.extend(parse_rules(data, str(path)))
        user_switches.extend(parse_switches(data))
    except (RuleError, ValueError) as exc:
        raise _Invalid(path) from exc
    for folder, ws in data.get("workspaces", {}).items():
        workspaces[str(Path(folder).expanduser())] = ws
    about.update(data.get("about", {}))
    if "mode" in data.get("guard", {}):
        settings["mode"] = data["guard"]["mode"]
    if "modes" in data.get("guard", {}):
        settings["modes"] = dict(data["guard"]["modes"])
    for raw, table in ((raw_allow, "allow"), (raw_deny, "deny"), (raw_protect, "protect")):
        for ws, kinds in data.get(table, {}).items():
            into = raw.setdefault(ws, {})
            for kind, vals in kinds.items():
                bucket = into.setdefault(kind, [])
                for value in vals:
                    seen.append((path, kind, value))
                    value = value.strip()
                    if value not in bucket:
                        bucket.append(value)


def _load(config_dir: Path, uid: int) -> GuardConfig:
    workspaces: dict[str, str] = {}
    raw_allow: ByWorkspace = {}
    raw_deny: ByWorkspace = {}
    raw_protect: ByWorkspace = {}
    settings: dict[str, object] = {}
    about: dict[str, str] = {}
    user_rules: list[Rule] = []
    user_switches: list[Switch] = []
    seen: list[tuple[Path, str, str]] = []  # (file, kind, value), checked once rules are final
    for path in _files(config_dir):
        try:
            _load_file(
                path,
                uid,
                workspaces,
                raw_allow,
                raw_deny,
                raw_protect,
                settings,
                about,
                user_rules,
                user_switches,
                seen,
                top=path == config_dir / "guard.toml",
            )
        except _Invalid:
            raise
        except Exception as exc:  # anything unexpected makes the file invalid
            raise _Invalid(path) from exc
    rules = merge_rules(default_rules(), user_rules)
    path_kinds = {r.kind for r in rules if r.match == "path_prefix"}
    bad: dict[tuple[Path, str], list[str]] = {}  # (file, kind) -> relative values, in order
    for path, kind, value in seen:
        if kind in path_kinds and not _absolute(value):
            values = bad.setdefault((path, kind), [])
            values += [] if value in values else [value]
    if bad:  # the first file and kind with one
        (path, kind), values = next(iter(bad.items()))
        raise _Invalid(path, f"{kind} values must be absolute paths: {', '.join(values)}")

    def expand(raw: ByWorkspace) -> ByWorkspace:
        """``~`` expanded in path values, then duplicates dropped (``~/x`` is ``$HOME/x``)."""
        return {
            ws: {
                kind: list(
                    dict.fromkeys(
                        os.path.expanduser(v) if kind in path_kinds else v for v in values
                    )
                )
                for kind, values in kinds.items()
            }
            for ws, kinds in raw.items()
        }

    return GuardConfig(
        rules=rules,
        workspaces=workspaces,
        allow=expand(raw_allow),
        deny=expand(raw_deny),
        about=about,
        protect=expand(raw_protect),
        mode=settings.get("mode", "protect"),
        modes=settings.get("modes", {}),
        switches=[*default_switches(), *user_switches],
    )


def load_config(config_dir: Path = DEFAULT_CONFIG_DIR, uid: int | None = None) -> GuardConfig:
    """Never raises: an invalid file, or any unexpected error, gives the default rules, no
    workspaces and ``error`` set to the file (or the config dir when no file is to blame);
    ``error_detail`` carries a hint when there is one."""
    detail = None
    try:
        return _load(config_dir, os.getuid() if uid is None else uid)
    except _Invalid as exc:
        error, detail = exc.path, exc.detail
    except Exception:  # noqa: BLE001 - a broken config must never break the hooks
        error = str(config_dir)
    return GuardConfig(
        rules=default_rules(),
        workspaces={},
        allow={},
        deny={},
        about={},
        error=error,
        error_detail=detail,
    )
