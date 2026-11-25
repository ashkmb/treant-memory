"""Generic target resolution: which targets a command line acts on, according to the rules.

For each segment of the line and each rule whose ``command`` and subcommand filter match,
the value comes from ``flags``, then ``env``, then ``config``, then ``default_from`` (or from a
built-in ``resolver``). Without a flag value, an ``unknown_if`` option in the arguments or
variable in the command's prefix skips the rest. A matched rule with no value reports its
kind as unresolved.

A variable in the command's prefix (``VAR=...``, ``env -u VAR``) hides the hook environment's
value for that variable, even when the prefix value is empty.

Same-line state (``cd``, ``export``, ``unset``, ...) is followed where it can be read exactly,
else the values that depend on it are unresolved; see ``line.Line``.

Project files named in ``config`` are parsed as data (at most 64 KiB each), never executed.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import subprocess
import tomllib
from collections.abc import Callable, Iterator, Mapping
from urllib.parse import urlparse

from treant_guard.lexer import REDIRECT, option_values, parse
from treant_guard.line import Line, Scope, Target, flag_values
from treant_guard.rules import ConfigLookup, Rule, Switch
from treant_guard.state import opaque, physical

MAX_CONFIG_BYTES = 64 * 1024
FALLBACK_TIMEOUT = 1

_SCP_URL = re.compile(r"(?:[^@/:\s]+@)?([^@/:\s]+):(?!//)")
_USERINFO_RE = re.compile(r"^([a-z][a-z0-9+.-]*://)[^/@\s]+@", re.IGNORECASE)
_PUSH_WITH_VALUE = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
# Variables that point git at another repository or config, so the remotes it pushes to may not
# be the folder's (``GIT_CONFIG_KEY_<n>``/``GIT_CONFIG_VALUE_<n>`` too).
_GIT_ENV = {
    "GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS",
}  # fmt: skip
_EXT_FORMATS = {
    ".ini": "ini",
    ".cfg": "ini",
    ".conf": "ini",
    ".toml": "toml",
    ".json": "json",
    ".jsonc": "jsonc",
}
# a JSON string (an unterminated one runs to the end of the line, which keeps the scan
# linear), or the start of a line or block comment
_JSONC_TOKEN = re.compile(r'"(?:[^"\\\n]|\\.)*"?|//|/\*')


def _stdout(run: Callable, argv: list[str], **kw: object) -> str | None:
    """Output of a successful run; None on any error or a non-zero exit."""
    try:
        r = run(argv, capture_output=True, text=True, timeout=FALLBACK_TIMEOUT, **kw)
    except Exception:  # noqa: BLE001 - any failure means no value (unresolved)
        return None
    if r.returncode != 0:
        return None
    return r.stdout or ""


class _Check:
    """External lookups for one ``targets_for`` call, each run at most once."""

    def __init__(self, environ: Mapping[str, str], run: Callable, home: str):
        self.environ = environ
        self.home = home
        self._run = run
        self._fallbacks: dict[tuple, str | None] = {}
        self._remotes: dict[str, dict[str, str]] = {}

    def fallback(self, argv: tuple[str, ...], cwd: str, env: Mapping[str, str]) -> str | None:
        """First line of ``argv``'s output, run in ``cwd`` with ``env``; once per all three."""
        key = (argv, cwd, frozenset(env.items()))
        if key not in self._fallbacks:
            out = _stdout(self._run, list(argv), cwd=cwd, env=dict(env))
            lines = (out or "").strip().splitlines()
            self._fallbacks[key] = lines[0].strip() if lines else None
        return self._fallbacks[key]

    def remotes(self, folder: str) -> dict[str, str]:
        """Push URLs of the repo at ``folder``, credentials stripped."""
        if folder not in self._remotes:
            found: dict[str, str] = {}
            out = _stdout(self._run, ["git", "-C", folder, "remote", "-v"])
            for line in (out or "").splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[2] == "(push)":
                    found[parts[0]] = _USERINFO_RE.sub(r"\1", parts[1])
            self._remotes[folder] = found
        return self._remotes[folder]


def _command_env(scope: Scope) -> dict[str, str]:
    """The environment the command runs with: the hook's, then exported changes and its
    prefix (an empty value: unset)."""
    env = dict(scope.environ)
    for name, value in scope.assigns.items():
        if value:
            env[name] = value
        else:
            env.pop(name, None)
    return env


# --- config files -----------------------------------------------------------


def _expand_home(path: str, home: str) -> str:
    if path == "~" or path.startswith("~/"):
        return home + path[1:]
    return os.path.expanduser(path)


def _is_bare(entry: str) -> bool:
    return not entry.startswith(("$", "~", "./")) and not os.path.isabs(entry)


def _candidates(
    lookup: ConfigLookup,
    cwd: str,
    environ: Mapping[str, str],
    home: str,
    assigns: Mapping[str, str],
) -> Iterator[str]:
    """Paths in lookup order: every entry at cwd, then bare entries in each parent (upward).

    ``$VAR`` takes the command's own prefix assignment first, then the hook environment.
    """
    default_name = next(
        (os.path.basename(f) for f in lookup.files if f.startswith("./") or _is_bare(f)), None
    )
    for entry in lookup.files:
        if entry.startswith("$"):
            name, sep, rest = entry[1:].partition("/")
            value = assigns[name] if name in assigns else environ.get(name)
            if not value:
                continue
            path = os.path.join(cwd, _expand_home(value + sep + rest, home))
            if default_name and os.path.isdir(path):
                path = os.path.join(path, default_name)
            yield path
        elif entry.startswith("~"):
            yield _expand_home(entry, home)
        else:
            yield os.path.join(cwd, entry)
    if lookup.search == "upward":
        bare = [f for f in lookup.files if _is_bare(f)]
        d = os.path.abspath(cwd)
        while (parent := os.path.dirname(d)) != d:
            d = parent
            for entry in bare:
                yield os.path.join(d, entry)


def _strip_jsonc(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments outside strings."""
    out: list[str] = []
    pos = 0
    while m := _JSONC_TOKEN.search(text, pos):
        tok = m.group()
        if tok == "//":
            out.append(text[pos : m.start()])
            end = text.find("\n", m.end())
            pos = len(text) if end < 0 else end
        elif tok == "/*":
            out.append(text[pos : m.start()] + " ")
            end = text.find("*/", m.end())
            pos = len(text) if end < 0 else end + 2
        else:
            out.append(text[pos : m.end()])
            pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def _read_key(path: str, lookup: ConfigLookup) -> str | None:
    fmt = lookup.format
    if fmt == "auto":
        fmt = _EXT_FORMATS.get(os.path.splitext(path)[1].lower())
    try:
        with open(path, "rb") as f:
            text = f.read(MAX_CONFIG_BYTES).decode("utf-8", errors="replace")
        if fmt == "ini":
            cp = configparser.ConfigParser(
                interpolation=None, strict=False, inline_comment_prefixes=(";",)
            )
            cp.read_string(text)
            section, _, option = lookup.key.partition(".")
            value = cp.get(section, option, fallback=None)
        elif fmt in ("toml", "json", "jsonc"):
            if fmt == "toml":
                value = tomllib.loads(text)
            else:
                value = json.loads(_strip_jsonc(text) if fmt == "jsonc" else text)
            for part in lookup.key.split("."):
                value = value.get(part) if isinstance(value, dict) else None
        else:
            return None
    except Exception:  # noqa: BLE001 - a project file never breaks the check
        return None
    if not isinstance(value, str):
        return None
    return value.strip() or None


def read_config_value(
    lookup: ConfigLookup,
    cwd: str,
    environ: Mapping[str, str],
    home: str,
    assigns: Mapping[str, str] | None = None,
) -> tuple[bool, str | None]:
    """(a config file was found, the key's value). Parse errors give no value.

    ``assigns`` are the command's ``VAR=value`` prefix; they win over ``environ`` for ``$VAR``.
    """
    found = False
    for path in _candidates(lookup, cwd, environ, home, assigns or {}):
        if not os.path.isfile(path):
            continue
        found = True
        value = _read_key(path, lookup)
        if value is not None or lookup.first_found_only:
            return True, value
    return found, None


# --- built-in resolvers -----------------------------------------------------


def host_of(url: str) -> str | None:
    """Lower-cased host of a git URL (``scheme://`` or scp-like ``user@host:path``), else None."""
    try:
        if "://" in url:
            host = urlparse(url).hostname
        else:
            m = _SCP_URL.match(url)
            host = m.group(1) if m else None
    except ValueError:
        return None
    return host.lower() if host else None


# ``git -c`` keys that can change the remote or its URL (or pull in more config).
_PUSH_CONFIG = ("remote.", "url.", "branch.", "include")


def _redirects_push(setting: str) -> bool:
    key = setting.partition("=")[0]
    return opaque(key) or key.lower().startswith(_PUSH_CONFIG)


def _git_folder(rule: Rule, pre: list[str], scope: Scope, home: str) -> str | None:
    """The repo folder after the global options ``pre`` (``-C`` values stack, relative ones
    against the previous folder); None with ``--git-dir``/``--work-tree``, ``--config-env``, a
    ``-c`` that may change where pushes go, or a computed ``-C``.
    """
    folder = scope.cwd
    i = 0
    while i < len(pre):
        a = pre[i]
        if a in ("--git-dir", "--work-tree", "--config-env") or a.startswith(
            ("--git-dir=", "--work-tree=", "--config-env=")
        ):
            return None
        if a == "-c" and _redirects_push(pre[i + 1] if i + 1 < len(pre) else ""):
            return None
        if a == "-C" and i + 1 < len(pre):
            if opaque(pre[i + 1]):
                return None
            folder = physical(os.path.join(folder, _expand_home(pre[i + 1], home)))
        i += 2 if a in rule.global_flags_with_value else 1
    return folder


def _git_env(scope: Scope) -> bool:
    """A variable that redirects git's repository or config is set, unknown, or changed on the
    line."""
    environ = {k for k, v in scope.environ.items() if v}
    names = environ | set(scope.assigns) | set(scope.unknown)
    return any(
        n in _GIT_ENV or n.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")) for n in names
    )


def _git_push_host(
    rule: Rule, cmd: str, args: list[str], sub: int | None, scope: Scope, check: _Check
) -> list[str]:
    pre, rest = (args[:sub], args[sub + 1 :]) if sub is not None else ([], args)
    folder = _git_folder(rule, pre, scope, check.home)
    if folder is None or _git_env(scope):
        return []
    remote = "origin"
    j = 0
    while j < len(rest):
        a = rest[j]
        if a == "--repo" or a.startswith("--repo="):
            return []  # the push goes where the option says, not to a named remote
        if a.startswith("-"):
            j += 2 if a in _PUSH_WITH_VALUE else 1
            continue
        if m := REDIRECT.match(a):
            j += 2 if m.end() == len(a) else 1  # ``2>&1``, ``> log``: not a remote
            continue
        remote = a
        break
    url = check.remotes(folder).get(remote)
    if url is None and ("://" in remote or "@" in remote):
        url = remote
    host = host_of(url) if url else None
    return [host] if host else []


def _terraform_dir(
    rule: Rule, cmd: str, args: list[str], sub: int | None, scope: Scope, check: _Check
) -> list[str]:
    root = scope.cwd
    if cmd == "terraform":
        chdir = option_values(args, {"-chdir"})
        if chdir:
            if opaque(chdir[-1]):
                return []
            root = physical(os.path.join(scope.cwd, chdir[-1]))
    elif cmd == "terragrunt":
        # the folder terragrunt acts on: its flag, else its environment variable
        given = option_values(args, _TG_DIR_FLAGS)
        if not given:
            for name in _TG_DIR_ENV:
                if name in scope.unknown:
                    return []
                value = scope.assigns.get(name)
                if value is None and not scope.clear_env:
                    value = scope.environ.get(name)
                if value is not None:
                    given = [value]
                    break
            else:
                if scope.env_unknown:
                    return []  # one of them may be exported with a value that can't be read
        if given:
            if not given[-1] or opaque(given[-1]):
                return []
            root = physical(os.path.join(scope.cwd, _expand_home(given[-1], check.home)))
    return [root]


_TG_DIR_FLAGS = {"--working-dir", "--terragrunt-working-dir"}
_TG_DIR_ENV = ("TG_WORKING_DIR", "TERRAGRUNT_WORKING_DIR")


_RESOLVERS = {"git_push_host": _git_push_host, "terraform_dir": _terraform_dir}


# --- one rule ---------------------------------------------------------------


def _subcommand(args: list[str], with_value: tuple[str, ...]) -> int | None:
    """Index of the first non-option argument, skipping the values of ``with_value`` options."""
    i = 0
    while i < len(args):
        if not args[i].startswith("-"):
            return i
        i += 2 if args[i] in with_value else 1
    return None


def _filter_passes(rule: Rule, args: list[str], sub: int | None) -> bool:
    if rule.subcommands is not None:
        return sub is not None and args[sub] in rule.subcommands
    if rule.skip_subcommands is not None:
        return sub is not None and args[sub] not in rule.skip_subcommands
    return True


def _env_values(rule: Rule, assigns: Mapping[str, str], environ: Mapping[str, str]) -> list[str]:
    """Values of the ``env`` vars; a var in the prefix hides the environment's value.

    ``all_values``: every set var in rule order, de-duplicated. Otherwise the first set one,
    looking at the prefix before the environment.
    """
    if rule.all_values:
        out: list[str] = []
        for var in rule.env:
            value = (assigns[var] if var in assigns else environ.get(var) or "").strip()
            if value and value not in out:
                out.append(value)
        return out
    for var in rule.env:
        value = (assigns.get(var) or "").strip()
        if value:
            return [value]
    for var in rule.env:
        value = "" if var in assigns else (environ.get(var) or "").strip()
        if value:
            return [value]
    return []


def _switched(rule: Rule, args: list[str], assigns: Mapping[str, str]) -> bool:
    """An ``unknown_if`` option is in the arguments or variable is in the prefix."""
    for entry in rule.unknown_if:
        if entry.startswith("$"):
            if entry[1:] in assigns:
                return True
        elif any(a == entry or a.startswith(entry + "=") for a in args):
            return True
    return False


def _reads_unknown(rule: Rule, unknown: frozenset[str]) -> bool:
    """The rule reads a variable whose value can't be read (env, ``$VAR`` entries)."""
    if not unknown:
        return False
    names = set(rule.env) | {e[1:] for e in rule.unknown_if if e.startswith("$")}
    if rule.config is not None:
        names |= {f[1:].partition("/")[0] for f in rule.config.files if f.startswith("$")}
    return not names.isdisjoint(unknown)


def _resolve(
    rule: Rule, cmd: str, args: list[str], sub: int | None, scope: Scope, check: _Check
) -> list[str]:
    if rule.resolver is not None:
        resolver = _RESOLVERS.get(rule.resolver)
        if resolver is None:
            raise ValueError(f"unknown resolver {rule.resolver!r}")
        return [] if scope.ambient else resolver(rule, cmd, args, sub, scope, check)
    given, blank = flag_values(rule, args)
    if given and not blank:
        return given
    # no flag, or a blank one (the tool then uses its default): read the default too
    return given + [v for v in _unflagged(rule, args, scope, check) if v not in given]


def _unflagged(rule: Rule, args: list[str], scope: Scope, check: _Check) -> list[str]:
    """The value the command uses without a flag: env, then config, then ``default_from``."""
    if scope.ambient or _reads_unknown(rule, scope.unknown):
        return []
    if _switched(rule, args, scope.assigns):
        return []
    values = _env_values(rule, scope.assigns, scope.environ)
    if not values and rule.config is not None:
        found = read_config_value(rule.config, scope.cwd, scope.environ, check.home, scope.assigns)
        value = found[1]
        values = [value] if value is not None else []
    if values:
        if rule.split:
            return [p.strip() for v in values for p in v.split(rule.split) if p.strip()]
        return values
    # the default command runs in the command's folder with its environment; ``env -i`` passes
    # no environment on, and one with a value the guard can't read can't tell
    if rule.default_from and not scope.clear_env and not scope.env_unknown:
        fallback = check.fallback(rule.default_from, scope.cwd, _command_env(scope))
        return [fallback] if fallback else []
    return []


class _Reader:
    """Rule matching and value lookup for one ``targets_for`` call."""

    def __init__(self, check: _Check):
        self.check = check

    def subcommand(self, rule: Rule, args: list[str]) -> tuple[bool, int | None]:
        sub = _subcommand(args, rule.global_flags_with_value)
        return _filter_passes(rule, args, sub), sub

    def values(
        self, rule: Rule, cmd: str, args: list[str], sub: int | None, scope: Scope
    ) -> list[str]:
        values = _resolve(rule, cmd, args, sub, scope, self.check)
        if rule.match != "path_prefix" or rule.resolver is not None or scope.ambient:
            return values
        return _with_folders(values, scope.cwd, self.check.home)


def _with_folders(values: list[str], cwd: str, home: str) -> list[str]:
    """Each relative folder value, and after it the folder it names from ``cwd``: the folder
    is what deny and protect see, the value as written is never allowed."""
    out: list[str] = []
    for value in values:
        out.append(value)
        path = _expand_home(value, home)
        if not os.path.isabs(path) and not opaque(value):
            folder = physical(os.path.join(cwd, path))
            if folder not in out:
                out.append(folder)
    return out


def targets_for(
    command: str,
    *,
    cwd: str,
    environ: Mapping[str, str],
    rules: list[Rule],
    switches: tuple[Switch, ...] = (),
    run: Callable = subprocess.run,
    home: str | None = None,
    switch_words: list[Target] | None = None,
) -> tuple[bool, list[Target], list[str]]:
    """(matched any rule, targets in first-seen order, unresolved kinds in first-seen order).

    A command that does not lex is matched with the single unresolved kind ``"command"``.
    ``switches`` (``[[switch]]`` entries) make their kinds unresolved for later segments;
    when ``switch_words`` is a list, the literal words given to them are appended to it.
    """
    try:
        items = parse(command)
    except ValueError:
        return True, [], ["command"]
    home = os.path.expanduser("~") if home is None else home
    reader = _Reader(_Check(environ, run, home))
    return Line(rules, reader, cwd, command, environ, home, switches, switch_words).walk(items)
