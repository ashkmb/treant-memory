"""`treant-guard` command line: check-config, check, init, import."""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import tomllib
from pathlib import Path

from treant_guard.config import DEFAULT_CONFIG_DIR, RENAMED_KINDS, GuardConfig, load_config
from treant_guard.engine import _is_denied, check
from treant_guard.rules import default_rules

_WS_NAME = re.compile(r"[a-z][a-z0-9_-]*")
_KIND = re.compile(r"[a-z][a-z0-9_]*")
_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\t": "\\t",
    "\r": "\\r",
    "\b": "\\b",
    "\f": "\\f",
}


def _quote(value: str) -> str:
    out = []
    for ch in value:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _ensure_dir(path: Path) -> None:
    """Create ``path`` (and missing parents) private to the user; an existing one is kept."""
    if not path.exists():
        for parent in reversed(path.parents):
            if not parent.exists():
                parent.mkdir(mode=0o700, exist_ok=True)
        path.mkdir(mode=0o700, exist_ok=True)
        path.chmod(0o700)


def _check_config(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config_dir))
    if cfg.error:
        detail = f": {cfg.error_detail}" if cfg.error_detail else ""
        print(f"invalid: {cfg.error}{detail}", file=sys.stderr)
        return 1
    overrides = "".join(f", {ws} {m}" for ws, m in sorted(cfg.modes.items()))
    print(
        f"ok: {len(cfg.rules)} rules, {len(cfg.workspaces)} workspaces, mode {cfg.mode}{overrides}"
    )
    rule_kinds = {r.kind for r in cfg.rules}
    tables = (cfg.allow, cfg.deny, cfg.protect)
    listed_kinds = {k for table in tables for kinds in table.values() for k in kinds}
    for kind in sorted(listed_kinds - rule_kinds):
        print(f"warning: kind {kind} is in allow/deny/protect but no rule uses it")
    for kind in sorted({k for s in cfg.switches for k in s.kinds} - rule_kinds):
        print(f"warning: [[switch]] kind {kind} is used by no rule")
    _warn_shadowed(cfg)
    for table, lists in (("allow", cfg.allow), ("deny", cfg.deny), ("protect", cfg.protect)):
        for ws in sorted(set(lists) - set(cfg.workspaces.values()) - {"*"}):
            print(f"warning: [{table}.{ws}] names a workspace not in [workspaces]")
    for key in sorted(set(cfg.modes) - set(cfg.workspaces.values())):
        print(f"warning: [guard.modes] key {key} is not a workspace in [workspaces]")
    for key in sorted(set(cfg.about) - set(cfg.workspaces.values())):
        print(f"warning: [about] key {key} is not a workspace in [workspaces]")
    return 0


def _warn_shadowed(cfg: GuardConfig) -> None:
    """Allow entries that never take effect: denied (in the workspace or everywhere, a path
    also inside a denied folder), or a host with capitals (hosts are read in lower case)."""
    home = os.path.expanduser("~")
    paths = {r.kind for r in cfg.rules if r.match == "path_prefix"}
    hosts = {r.kind for r in cfg.rules if r.resolver == "git_push_host"}
    star = cfg.deny.get("*", {})
    for ws in sorted(cfg.allow):
        own = cfg.deny.get(ws, {})
        for kind, values in sorted(cfg.allow[ws].items()):
            fold = str.lower if kind in hosts else str
            prefix = kind in paths
            mine = [fold(v) for v in own.get(kind, [])]
            every = [fold(v) for v in star.get(kind, [])]
            for value in values:
                if value in own.get(kind, []):
                    note = f"is in both allow and deny for {ws} (deny wins)"
                elif prefix and _is_denied(value, mine, prefix, home):
                    note = f"is in allow for {ws} but inside a denied folder (deny wins)"
                elif _is_denied(fold(value), every, prefix, home):
                    note = f"is in allow for {ws} but denied everywhere (deny wins)"
                elif fold(value) != value:
                    note = f"in [allow.{ws}] never matches: hosts are read in lower case"
                else:
                    continue
                print(f"warning: {kind}={value} {note}")


def _check(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config_dir))
    cwd = args.cwd or os.getcwd()
    command = " ".join(args.command)
    decision = check(command, cwd=cwd, workspace_dir=cwd, environ=os.environ, config=cfg)
    print(decision.action)
    if decision.reason:
        print(decision.reason)
    return {"allow": 0, "ask": 1, "deny": 2}[decision.action]


def _init(args: argparse.Namespace) -> int:
    config_dir = Path(args.config_dir)
    target = config_dir / "guard.toml"
    if os.path.lexists(target):
        print(f"refusing to overwrite {target}", file=sys.stderr)
        return 1
    _ensure_dir(config_dir)
    kinds = sorted({r.kind for r in default_rules()})
    lines = [
        "# treant-guard config. Map folders to workspaces, then list the targets that matter.",
        "#",
        '# Mode "protect" (the default) asks only for a protected target or one another',
        '# workspace allows; anything else runs. Mode "allowlist" asks for every target the',
        "# workspace does not allow, and whenever it can't tell the target.",
        "# [guard]",
        '# mode = "protect"',
        "#",
        "# [workspaces]",
        '# "~/work/example" = "example"',
        "#",
        '# Production targets: always ask (in every workspace with "*", or in one).',
        '# [protect."*"]',
        '# kube_context = ["prod"]',
        "#",
        "# The workspace's own targets: protect mode asks when another workspace uses them;",
        "# allowlist mode asks for anything not listed here.",
        "# [allow.example]",
        *(f'# {kind} = ["..."]' for kind in kinds),
        "#",
        "# Targets that are always refused in this workspace (deny wins over allow).",
        "# [deny.example]",
        '# kube_context = ["prod"]',
        "#",
        "# Targets refused in every workspace.",
        '# [deny."*"]',
        '# kube_context = ["prod"]',
        "#",
        "# Commands that change a tool's state on disk (the shipped pack covers kubectx,",
        "# kubectl config use-context, aws configure, git remote add/set-url, git config): their",
        '# kinds are "can\'t tell" for the rest of the line. Yours are added to the shipped ones.',
        "# In protect mode a word they are given (kubectx prod) is checked like a target.",
        "# [[switch]]",
        '# command = ["kctx"]                 # e.g. a helper script that switches context',
        '# kinds = ["kube_context"]',
        "#",
        "# A short description (<= 2000 characters) shown at session start for each workspace.",
        "# [about]",
        '# example = "What this workspace is for."',
        "",
    ]
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    target.chmod(0o600)
    print(f"wrote {target}")
    return 0


_HEADER = "# written by treant-guard import; comments in this file are not kept"


_KEPT = ("deny", "protect")  # tables of the workspace that import keeps as they are


def _existing_kept(path: Path, ws: str) -> dict[str, dict[str, list[str]]] | None:
    """The file's `[deny.<ws>]` and `[protect.<ws>]` lists by table, or None if it holds
    anything import does not own."""
    if not path.exists():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        print(f"cannot read {path}: {exc}; not overwriting it", file=sys.stderr)
        return None
    allow = data.get("allow", {})
    kept: dict[str, dict[str, list[str]]] = {}
    foreign = (
        bool(set(data) - {"allow", *_KEPT}) or not isinstance(allow, dict) or set(allow) - {ws}
    )
    for name in _KEPT:
        table = data.get(name, {})
        lists = table.get(ws, {}) if isinstance(table, dict) else None
        if (
            not isinstance(table, dict)
            or set(table) - {ws}
            or not isinstance(lists, dict)
            or any(
                not _KIND.fullmatch(k)
                or not isinstance(v, list)
                or not all(isinstance(x, str) for x in v)
                for k, v in lists.items()
            )
        ):
            foreign = True
        elif lists:
            kept[name] = {k: list(v) for k, v in lists.items()}
    if foreign:
        print(
            f"{path} holds more than [allow.{ws}], [deny.{ws}] and [protect.{ws}]; "
            "move the rest to guard.toml and retry",
            file=sys.stderr,
        )
        return None
    if allow.get(ws):
        kept["allow"] = allow[ws]  # only to tell an empty import from a wanted one
    return kept


def _read_stdin() -> str | None:
    buffer = getattr(sys.stdin, "buffer", None)
    try:
        return buffer.read().decode("utf-8") if buffer is not None else sys.stdin.read()
    except UnicodeDecodeError:
        print("input is not UTF-8; nothing imported", file=sys.stderr)
        return None


def _import(args: argparse.Namespace) -> int:
    ws = args.workspace
    if not _WS_NAME.fullmatch(ws):
        print(f"invalid workspace name: {ws!r} (must match {_WS_NAME.pattern})", file=sys.stderr)
        return 2
    config_dir = Path(args.config_dir)
    guard_d = config_dir / "guard.d"
    dest = guard_d / f"{ws}.toml"
    kept = _existing_kept(dest, ws)
    if kept is None:
        return 1
    text = _read_stdin()
    if text is None:
        return 1
    kinds: dict[str, list[str]] = {}
    count = 0
    for n, line in enumerate(text.splitlines(), 1):
        if "\t" not in line:
            continue
        kind, _, value = line.partition("\t")
        kind, value = kind.strip(), value.strip()
        if not kind or not value:
            continue
        if not _KIND.fullmatch(kind):
            print(f"skipped line {n}: invalid kind", file=sys.stderr)
            continue
        kind = RENAMED_KINDS.get(kind, kind)
        bucket = kinds.setdefault(kind, [])
        if value not in bucket:
            bucket.append(value)
            count += 1
    if not count and "allow" in kept:
        # an empty export must not wipe the list: protect mode reads it from other workspaces
        print(f"nothing to import; {dest} left as is", file=sys.stderr)
        return 1
    lines = [_HEADER, f"[allow.{ws}]"]
    for kind in sorted(kinds):
        lines.append(f"{kind} = [{', '.join(_quote(v) for v in kinds[kind])}]")
    for name in _KEPT:
        lists = kept.get(name, {})
        if lists:
            lines.append(f"[{name}.{ws}]")
            for kind in sorted(lists):
                lines.append(f"{kind} = [{', '.join(_quote(v) for v in lists[kind])}]")
    _ensure_dir(config_dir)
    _ensure_dir(guard_d)
    fd, tmp = tempfile.mkstemp(dir=guard_d, prefix=f".{ws}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    print(f"imported {count} targets for {ws}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="treant-guard")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn) -> argparse.ArgumentParser:
        p = sub.add_parser(name)
        p.add_argument("--config-dir", default=str(DEFAULT_CONFIG_DIR))
        p.set_defaults(fn=fn)
        return p

    add("check-config", _check_config)
    p = add("check", _check)
    p.add_argument("--cwd")
    p.add_argument("command", nargs=argparse.REMAINDER)
    add("init", _init)
    p = add("import", _import)
    p.add_argument("workspace")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "check" and args.command[:1] == ["--"]:
        args.command = args.command[1:]
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
