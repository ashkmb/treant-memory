"""Declarative guard rules: model, loader/validator, merging and the default pack."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from importlib.resources import files

BUILTIN_RESOLVERS = ("git_push_host", "terraform_dir")
_FORMATS = ("auto", "ini", "toml", "json", "jsonc")
_SEARCHES = ("cwd", "upward")
_MATCHES = ("exact", "path_prefix")
_UNKNOWN_IF = re.compile(r"-.+|\$[A-Za-z_][A-Za-z0-9_]*")
_SHORT_FLAG = re.compile(r"-[^-\s]")


class RuleError(ValueError):
    """Invalid rule; the message names the file and the rule index."""


@dataclass(frozen=True)
class ConfigLookup:
    files: tuple[str, ...]
    format: str = "auto"
    key: str = ""
    first_found_only: bool = False
    search: str = "cwd"


@dataclass(frozen=True)
class Rule:
    command: tuple[str, ...]
    kind: str
    subcommands: tuple[str, ...] | None = None
    skip_subcommands: tuple[str, ...] | None = None
    global_flags_with_value: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    short_flag: str | None = None
    all_values: bool = False
    unknown_if: tuple[str, ...] = ()
    env: tuple[str, ...] = ()
    config: ConfigLookup | None = None
    default_from: tuple[str, ...] | None = None
    resolver: str | None = None
    split: str | None = None
    match: str = "exact"
    disabled: bool = False
    source: str = ""


@dataclass(frozen=True)
class Switch:
    """A command that changes a tool's state on disk: its kinds are unknown for the rest of
    the line, whatever the separator."""

    command: tuple[str, ...]
    kinds: tuple[str, ...]
    args: tuple[str, ...] = ()


_SWITCH_FIELDS = {"command", "kinds", "args"}


def parse_switch(raw: object) -> Switch:
    """Validate one ``[[switch]]`` table; ValueError names the problem."""
    if not isinstance(raw, dict):
        raise ValueError("switch must be a table")  # noqa: TRY004
    extra = set(raw) - _SWITCH_FIELDS
    if extra:
        raise ValueError(f"switch: unknown field {min(extra)!r}")
    lists = {}
    for name in _SWITCH_FIELDS:
        value = raw.get(name, [])
        if not _check_type(value, "list"):
            raise ValueError(f"switch: {name} must be a list of strings")
        lists[name] = tuple(value)
    if not lists["command"] or not all(lists["command"]):
        raise ValueError("switch: command must be a non-empty list")
    if not lists["kinds"] or not all(lists["kinds"]):
        raise ValueError("switch: kinds must be a non-empty list")
    if not all(lists["args"]):
        raise ValueError("switch: args must not hold an empty string")
    return Switch(**lists)


def parse_switches(data: dict) -> list[Switch]:
    raw = data.get("switch", [])
    if not isinstance(raw, list):
        raise ValueError("'switch' must be an array of tables")  # noqa: TRY004
    return [parse_switch(s) for s in raw]


def default_switches() -> list[Switch]:
    text = (files("treant_guard.rules") / "default.toml").read_text(encoding="utf-8")
    return parse_switches(tomllib.loads(text))


# field name -> expected kind: "list" (of str), "str", "bool"
_FIELDS = {
    "command": "list",
    "kind": "str",
    "subcommands": "list",
    "skip_subcommands": "list",
    "global_flags_with_value": "list",
    "flags": "list",
    "short_flag": "str",
    "all_values": "bool",
    "unknown_if": "list",
    "env": "list",
    "config": "table",
    "default_from": "list",
    "resolver": "str",
    "split": "str",
    "match": "str",
    "disabled": "bool",
}
_CONFIG_FIELDS = {
    "files": "list",
    "format": "str",
    "key": "str",
    "first_found_only": "bool",
    "search": "str",
}


def _check_type(value: object, kind: str) -> bool:
    if kind == "list":
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    if kind == "str":
        return isinstance(value, str)
    if kind == "bool":
        return isinstance(value, bool)
    return isinstance(value, dict)


def _convert(raw: dict, fields: dict[str, str], where: str) -> dict:
    out: dict = {}
    for name, value in raw.items():
        if name not in fields:
            raise RuleError(f"{where}: unknown field {name!r}")
        if not _check_type(value, fields[name]):
            raise RuleError(f"{where}: field {name!r} must be of type {fields[name]}")
        out[name] = tuple(value) if isinstance(value, list) else value
    return out


def _parse_config(raw: dict, where: str) -> ConfigLookup:
    kwargs = _convert(raw, _CONFIG_FIELDS, f"{where}: config")
    if not kwargs.get("files"):
        raise RuleError(f"{where}: config.files must be a non-empty list of strings")
    if "" in kwargs["files"]:
        raise RuleError(f"{where}: config.files must not hold an empty string")
    if kwargs.get("format", "auto") not in _FORMATS:
        raise RuleError(f"{where}: config.format must be one of {', '.join(_FORMATS)}")
    if kwargs.get("search", "cwd") not in _SEARCHES:
        raise RuleError(f"{where}: config.search must be one of {', '.join(_SEARCHES)}")
    return ConfigLookup(**kwargs)


def _parse_rule(raw: object, index: int, source: str) -> Rule:
    where = f"{source}: rule {index}"
    if not isinstance(raw, dict):
        raise RuleError(f"{where}: must be a table")
    kwargs = _convert(raw, _FIELDS, where)
    if not kwargs.get("command"):
        raise RuleError(f"{where}: command must be a non-empty list")
    if not kwargs.get("kind"):
        raise RuleError(f"{where}: kind must be a non-empty string")
    # an empty word never matches: the rule would silently check nothing
    for name in ("subcommands", "default_from"):
        if name in kwargs and not kwargs[name]:
            raise RuleError(f"{where}: {name} must be a non-empty list")
    for name, kind in _FIELDS.items():
        if kind == "list" and "" in kwargs.get(name, ()):
            raise RuleError(f"{where}: {name} must not hold an empty string")
    if "short_flag" in kwargs and not _SHORT_FLAG.fullmatch(kwargs["short_flag"]):
        raise RuleError(f"{where}: short_flag must be a dash and one letter, like -i")
    if kwargs.get("split") == "":
        raise RuleError(f"{where}: split must not be empty")
    if "subcommands" in kwargs and "skip_subcommands" in kwargs:
        raise RuleError(f"{where}: subcommands and skip_subcommands are mutually exclusive")
    resolver = kwargs.get("resolver")
    if resolver is not None:
        if resolver not in BUILTIN_RESOLVERS:
            raise RuleError(f"{where}: unknown resolver {resolver!r}")
        clash = [k for k in ("flags", "unknown_if", "env", "config", "default_from") if k in kwargs]
        if clash:
            raise RuleError(f"{where}: resolver cannot be combined with {', '.join(clash)}")
    bad = [e for e in kwargs.get("unknown_if", ()) if not _UNKNOWN_IF.fullmatch(e)]
    if bad:
        raise RuleError(f"{where}: unknown_if entries must be -options or $NAME: {bad[0]!r}")
    if kwargs.get("match", "exact") not in _MATCHES:
        raise RuleError(f"{where}: match must be one of {', '.join(_MATCHES)}")
    if "config" in kwargs:
        kwargs["config"] = _parse_config(kwargs["config"], where)
    return Rule(**kwargs, source=source)


def parse_rules(data: dict, source: str) -> list[Rule]:
    """Validate the `rule` array of a parsed TOML document."""
    raw = data.get("rule", [])
    if not isinstance(raw, list):
        raise RuleError(f"{source}: 'rule' must be an array of tables")
    return [_parse_rule(r, i, source) for i, r in enumerate(raw)]


def default_rules() -> list[Rule]:
    text = (files("treant_guard.rules") / "default.toml").read_text(encoding="utf-8")
    return parse_rules(tomllib.loads(text), "default.toml")


def _key(rule: Rule) -> tuple[frozenset[str], str]:
    return (frozenset(rule.command), rule.kind)


def merge_rules(base: list[Rule], overrides: list[Rule]) -> list[Rule]:
    """Overrides replace base rules in place, disabled ones remove them, new ones append."""
    by_key = {_key(r): r for r in overrides}
    base_keys = {_key(r) for r in base}
    merged = [by_key.get(_key(r), r) for r in base]
    merged += [r for k, r in by_key.items() if k not in base_keys]
    return [r for r in merged if not r.disabled]
