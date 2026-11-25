"""Pre-flight check for risky shell commands.

``analyse`` is a best-effort reader of a shell command line: it finds the
kube context, AWS profile, git host, terraform root or ansible inventory a risky
command would act on, and the target kinds it could not determine (``"command"``
when the line does not parse). A kind the line itself changes (``cd``, ``export AWS_PROFILE``,
``kubectl config use-context``, ``source``, ...) is unresolved for the rest of the line instead
of falling back to the client's value; ``sh -c`` and ``eval`` scripts are read too, and a
command run by another (``xargs``, ``find -exec``, a substitution inside double quotes) is the
unresolved kind ``"command"``. ``parse_targets`` is the same without the unresolved kinds.
``guard_check`` then reports which targets the caller's workspace does not list,
the unresolved kinds, plus current warnings on workspace nodes the command names.

Nothing outside the caller's workspace is read, and ``unlisted`` only ever echoes
values parsed from the request.
"""

import logging
import os
import re
import shlex
from dataclasses import dataclass, field
from urllib.parse import urlparse

import psycopg
from psycopg import sql

from treant import graph
from treant.db import Database
from treant.tokens import TokenContext

log = logging.getLogger(__name__)

Target = tuple[str, str]

MAX_WARNINGS = 5
MAX_WARNING_CHARS = 300
MAX_NAMES = 200

_SEP = "\x00"
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_SCP_URL = re.compile(r"(?:[^@/:\s]+@)?([^@/:\s]+):(?!//)")
_WORD_SPLIT = re.compile(r"[/=:@\s]+")
# Wrapper commands that run the next command; value is the options that take an argument.
_WRAPPERS = {
    "env": {"-u", "--unset", "-C", "--chdir"},
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U"},
    "command": set(),
    "time": set(),
    "nohup": set(),
    "exec": set(),
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
}
# Wrappers that take positional arguments before the command (``timeout DURATION cmd``).
_WRAPPER_POSITIONALS = {"timeout": 1}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "mksh", "fish"}
# Commands that run another command from their arguments or input; their targets are not read.
_RUNNERS = {
    "xargs", "parallel", "watch", "find", "ssh", "su", "doas", "flock", "nice", "ionice",
    "setsid", "stdbuf", "chroot", "unbuffer", "script", "systemd-run", "chrt", "taskset",
}  # fmt: skip
_RISKY = {"kubectl", "helm", "aws", "git", "terraform", "terragrunt", "ansible-playbook"}
_RISKY_WORD = re.compile(
    r"\b(?:kubectl|helm|aws|terraform|terragrunt|ansible-playbook)\b|\bgit\b.*\bpush\b"
)
# Variables whose change on the line makes a target kind unknown for the rest of it.
_STATE_VARS = {
    "AWS_PROFILE": "aws_profile",
    "AWS_DEFAULT_PROFILE": "aws_profile",
    "AWS_CONFIG_FILE": "aws_profile",
    "AWS_SHARED_CREDENTIALS_FILE": "aws_profile",
    "KUBECONFIG": "kube_context",
    "ANSIBLE_INVENTORY": "ansible_inventory",
    "ANSIBLE_CONFIG": "ansible_inventory",
    "GIT_DIR": "git_host",
    "GIT_WORK_TREE": "git_host",
    "GIT_CONFIG": "git_host",
    "HOME": "tf_root",
    "PWD": "tf_root",
}
_ALL_KINDS = frozenset(("kube_context", "aws_profile", "git_host", "tf_root", "ansible_inventory"))
_MAX_DEPTH = 3
_GIT_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
_PUSH_WITH_VALUE = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}


@dataclass(frozen=True)
class GuardEnv:
    cwd: str
    kube_context: str | None
    aws_profile: str | None
    git_remotes: dict[str, str] = field(default_factory=dict)
    # the inventory ansible-playbook falls back to without -i (ANSIBLE_INVENTORY or ansible.cfg)
    ansible_inventory: str | None = None


def _scan(command: str) -> tuple[str, bool]:
    """Replace unquoted ``; & | ( )``, backticks and newlines with a sentinel word; also say
    whether a double-quoted string holds a command substitution (``"$(..)"`` or a backtick),
    which this reader cannot split out.

    Quote-aware, so ``echo "a;b"`` stays whole; fused runs such as ``;(`` or ``&&\\n``
    become several sentinels. Backslash-newline outside single quotes is a line continuation
    and is dropped; an escaped backslash before a newline (``\\\\<newline>``) is not.
    """
    command = command.replace(_SEP, "")
    out: list[str] = []
    quote = ""
    hidden = False
    i = 0
    while i < len(command):
        ch = command[i]
        if quote != "'" and command[i : i + 2] == "\\\n":
            i += 2
            continue
        if quote == "'":
            quote = "" if ch == "'" else quote
        elif quote == '"':
            if ch == "\\" and i + 1 < len(command):
                out.append(ch)
                i += 1
                ch = command[i]
            elif ch == '"':
                quote = ""
            elif ch == "`" or command[i : i + 2] == "$(":
                hidden = True
        elif ch == "\\" and i + 1 < len(command):
            out.append(ch)
            i += 1
            ch = command[i]
        elif ch in "'\"":
            quote = ch
        elif ch in ";|()`\n" or (ch == "&" and not _is_redirect_amp(command, i)):
            ch = f" {_SEP} "
        out.append(ch)
        i += 1
    return "".join(out), hidden


def _mark_separators(command: str) -> str:
    return _scan(command)[0]


def _is_redirect_amp(command: str, i: int) -> bool:
    return (i > 0 and command[i - 1] in "<>") or command[i + 1 : i + 2] == ">"


def _lex(command: str) -> list[str]:
    return shlex.split(_mark_separators(command))


def _segments(tokens: list[str]) -> list[list[str]]:
    out: list[list[str]] = [[]]
    for tok in tokens:
        if tok == _SEP:
            out.append([])
        else:
            out[-1].append(tok)
    return [s for s in out if s]


def _option_values(args: list[str], long_names: set[str], short: str | None = None) -> list[str]:
    values: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":  # the rest are operands (kubectl exec pod -- cmd --context=x)
            break
        hit = False
        for name in long_names:
            if a == name:
                if i + 1 < len(args):
                    values.append(args[i + 1])
                    i += 1
                hit = True
                break
            if a.startswith(name + "="):
                values.append(a[len(name) + 1 :])
                hit = True
                break
        if not hit and short and a.startswith(short) and not a.startswith("--") and len(a) > 2:
            values.append(a[2:])
        i += 1
    return values


def _unwrap(seg: list[str]) -> tuple[dict[str, str], list[str]]:
    """Strip leading assignments and wrapper commands; return (assignments, argv)."""
    assigns: dict[str, str] = {}
    i = 0
    while i < len(seg):
        tok = seg[i]
        if _ASSIGN.match(tok):
            key, _, val = tok.partition("=")
            assigns[key] = val
            i += 1
            continue
        name = os.path.basename(tok)
        wrapper = _WRAPPERS.get(name)
        if wrapper is None:
            break
        i += 1
        while i < len(seg) and (seg[i].startswith("-") or _ASSIGN.match(seg[i])):
            opt = seg[i]
            if _ASSIGN.match(opt):
                key, _, val = opt.partition("=")
                assigns[key] = val
                i += 1
                continue
            i += 2 if opt in wrapper else 1
        i += _WRAPPER_POSITIONALS.get(name, 0)
    return assigns, seg[i:]


def _host_of(url: str) -> str | None:
    try:
        if "://" in url:
            host = urlparse(url).hostname
        else:
            m = _SCP_URL.match(url)
            host = m.group(1) if m else None
    except ValueError:
        return None
    return host.lower() if host else None


Analysis = tuple[bool, list[Target], list[str]]  # (risky, targets, unresolved kinds)


def _git_push_target(args: list[str], env: GuardEnv) -> Analysis:
    """``args`` follows ``git``."""
    i = 0
    while i < len(args):
        a = args[i]
        if not a.startswith("-"):
            break
        i += 2 if a in _GIT_GLOBAL_WITH_VALUE else 1
    if i >= len(args) or args[i] != "push":
        return False, [], []
    rest = args[i + 1 :]
    remote = "origin"
    j = 0
    while j < len(rest):
        a = rest[j]
        if a.startswith("-"):
            j += 2 if a in _PUSH_WITH_VALUE else 1
            continue
        remote = a
        break
    url = env.git_remotes.get(remote)
    if url is None and ("://" in remote or "@" in remote):
        url = remote
    host = _host_of(url) if url else None
    return _one("git_host", host)


def _one(kind: str, value: str | None) -> Analysis:
    return (True, [(kind, value)], []) if value else (True, [], [kind])


def _blur(env: GuardEnv, blurred: frozenset[str]) -> GuardEnv:
    """The client's environment minus what the line has changed before this segment."""
    return GuardEnv(
        env.cwd,
        None if "kube_context" in blurred else env.kube_context,
        None if "aws_profile" in blurred else env.aws_profile,
        {} if "git_host" in blurred else env.git_remotes,
        None if "ansible_inventory" in blurred else env.ansible_inventory,
    )


def _changes(seg: list[str]) -> set[str]:
    """Target kinds this segment may change for the segments after it."""
    assigns, argv = _unwrap(seg)
    if not argv:  # a bare assignment (it updates an exported variable too)
        return {_STATE_VARS[k] for k in assigns if k in _STATE_VARS}
    cmd, args = os.path.basename(argv[0]), argv[1:]
    if cmd in ("cd", "pushd", "popd"):
        return {"tf_root"}
    if cmd in ("source", "."):
        return set(_ALL_KINDS)
    if cmd == "set":  # only allexport (-a) changes what later commands see
        flags = [a for a in args if a[:1] in "-+" and not a.startswith("--")]
        if "allexport" in args or any("a" in f[1:] for f in flags):
            return set(_ALL_KINDS)
        return set()
    if cmd in ("export", "declare", "typeset", "local", "readonly", "unset"):
        out: set[str] = set()
        for a in args:
            if a.startswith("-"):
                continue
            name = a.partition("=")[0]
            if not _ASSIGN.match(name + "="):
                return set(_ALL_KINDS)  # a computed name: anything may change
            if name in _STATE_VARS:
                out.add(_STATE_VARS[name])
        return out
    if cmd in ("kubectx", "kubens"):
        return {"kube_context"}
    if cmd == "kubectl" and "config" in args:
        return {"kube_context"}
    if cmd == "git" and ("remote" in args or "config" in args):
        return {"git_host"}
    return set()


def _shell_script(args: list[str]) -> str | None:
    """The script of ``sh -c SCRIPT`` (``-c`` alone or in a flag run like ``-ec``)."""
    for i, a in enumerate(args):
        if a.startswith("-") and not a.startswith("--") and "c" in a[1:]:
            return args[i + 1] if i + 1 < len(args) else None
        if not a.startswith("-"):
            return None
    return None


def _segment_targets(
    seg: list[str], env: GuardEnv, blurred: frozenset[str] = frozenset(), depth: int = 0
) -> Analysis:
    env = _blur(env, blurred)
    assigns, argv = _unwrap(seg)
    if not argv:
        return False, [], []
    cmd, args = os.path.basename(argv[0]), argv[1:]
    if cmd in _SHELLS or cmd == "eval":
        script = " ".join(args) if cmd == "eval" else _shell_script(args)
        if script is None:
            return False, [], []
        if depth >= _MAX_DEPTH:
            return True, [], ["command"]
        return _analyse(script, env, blurred, depth + 1)
    if cmd in _RUNNERS:
        if any(os.path.basename(a) in _RISKY for a in args):
            return True, [], ["command"]
        return False, [], []
    if cmd in ("kubectl", "helm"):
        names = {"--context"} | ({"--kube-context"} if cmd == "helm" else set())
        values = _option_values(args, names)
        return _one("kube_context", values[-1] if values else env.kube_context)
    if cmd == "aws":
        values = _option_values(args, {"--profile"})
        value = values[-1] if values else assigns.get("AWS_PROFILE") or env.aws_profile
        return _one("aws_profile", value)
    if cmd == "git":
        return _git_push_target(args, env)
    if cmd in ("terraform", "terragrunt"):
        flags = {"-chdir"} if cmd == "terraform" else {"--working-dir", "--terragrunt-working-dir"}
        chdir = _option_values(args, flags)
        if "tf_root" in blurred and not (chdir and os.path.isabs(chdir[-1])):
            return True, [], ["tf_root"]
        root = os.path.normpath(os.path.join(env.cwd, chdir[-1])) if chdir else env.cwd
        return True, [("tf_root", root)], []
    if cmd == "ansible-playbook":
        values = _option_values(args, {"--inventory", "-i"}, short="-i")
        if not values:
            default = assigns.get("ANSIBLE_INVENTORY") or env.ansible_inventory or ""
            values = [v.strip() for v in default.split(",") if v.strip()]
        if not values:
            return True, [], ["ansible_inventory"]
        return True, [("ansible_inventory", v) for v in values], []
    return False, [], []


def analyse(command: str, env: GuardEnv) -> Analysis:
    """Return (risky, targets, unresolved): the kinds of target the guard could not determine.

    An unparsable command is risky with the single unresolved kind ``"command"``.
    """
    return _analyse(command, env, frozenset(), 0)


def _analyse(command: str, env: GuardEnv, blurred: frozenset[str], depth: int) -> Analysis:
    try:
        text, hidden = _scan(command)
        tokens = shlex.split(text)
    except ValueError:
        return True, [], ["command"]
    risky = False
    targets: list[Target] = []
    unresolved: list[str] = []
    if hidden and _RISKY_WORD.search(command):
        risky, unresolved = True, ["command"]
    for seg in _segments(tokens):
        seg_risky, seg_targets, seg_unresolved = _segment_targets(seg, env, blurred, depth)
        # A change of state (cd, export, use-context, ...) makes that kind unknown afterwards,
        # whether or not it ran (``a || cd x``): never fall back to the client's value.
        blurred = blurred | _changes(seg)
        risky = risky or seg_risky
        for t in seg_targets:
            if t not in targets:
                targets.append(t)
        for kind in seg_unresolved:
            if kind not in unresolved:
                unresolved.append(kind)
    return risky, targets, unresolved


def parse_targets(command: str, env: GuardEnv) -> tuple[bool, list[Target]]:
    risky, targets, _ = analyse(command, env)
    return risky, targets


def _under(path: str, root: str) -> bool:
    path, root = os.path.normpath(path), os.path.normpath(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


def _words(command: str) -> list[str]:
    try:
        tokens = _lex(command)
    except ValueError:
        tokens = command.replace(_SEP, "").split()
    words = {w.lower() for t in tokens for w in _WORD_SPLIT.split(t) if w}
    return sorted(words)


def _warnings(conn: psycopg.Connection, schema: sql.Identifier, words: list[str]) -> list[str]:
    if not words:
        return []
    rows = conn.execute(
        sql.SQL(
            "SELECT f.text FROM {s}.facts f JOIN {s}.nodes n ON n.id = f.node_id "
            "WHERE f.type = 'warning' AND f.status = 'current' AND n.status = 'current' "
            "AND (lower(n.name) = ANY(%s) OR EXISTS "
            "(SELECT 1 FROM unnest(n.aliases) a WHERE lower(a) = ANY(%s))) "
            "ORDER BY f.created_at, f.id LIMIT %s"
        ).format(s=schema),
        (words, words, MAX_WARNINGS),
    ).fetchall()
    return [r[0][:MAX_WARNING_CHARS] for r in rows]


def warnings_for(db: Database, ctx: TokenContext, names: list[str]) -> list[str]:
    """Current warning facts on current workspace nodes whose name or alias is in ``names``
    (the first ``MAX_NAMES`` entries)."""
    words = list(dict.fromkeys(n.lower() for n in names[:MAX_NAMES] if isinstance(n, str) and n))
    if not words:
        return []
    with db.ws_tx(ctx.workspace) as conn:
        return _warnings(conn, graph.schema_of(ctx.workspace), words)


def guard_check(db: Database, ctx: TokenContext, command: str, env: GuardEnv) -> dict:
    """Deprecated: the client decides locally; use warnings."""
    risky, targets, unresolved = analyse(command, env)
    if not risky:
        return {"risky": False, "unlisted": [], "warnings": [], "unresolved": []}
    schema = graph.schema_of(ctx.workspace)
    words = _words(command)
    with db.ws_tx(ctx.workspace) as conn:
        listed = {
            (k, v)
            for k, v in conn.execute(
                sql.SQL("SELECT kind, value FROM {}.targets").format(schema)
            ).fetchall()
        }
        # Every word of the command, not capped like a client's name list.
        warnings = _warnings(conn, schema, words)
    tf_roots = [v for k, v in listed if k == "tf_root"]
    unlisted = []
    for kind, value in targets:
        if kind == "tf_root":
            ok = any(_under(value, root) for root in tf_roots)
        else:
            ok = (kind, value) in listed
        if not ok:
            unlisted.append({"kind": kind, "value": value})
    log.info(
        "guard check: targets=%d unlisted=%d unresolved=%d warnings=%d",
        len(targets),
        len(unlisted),
        len(unresolved),
        len(warnings),
    )
    return {"risky": True, "unlisted": unlisted, "warnings": warnings, "unresolved": unresolved}
