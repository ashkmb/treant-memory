"""Tool-independent shell lexing: separators, words, wrappers, option values."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Self

SEP = "\x00"
# One sentinel word per separator kind (SEP + a tag); shlex keeps each as its own token.
_KINDS = {"s": "seq", "a": "and", "o": "or", "p": "pipe", "b": "bg", "(": "open", ")": "close"}
# Separator text -> tag, longest first: ``;;&``, ``;&`` (bash 4+) and ``;|`` (zsh) end a
# ``case`` item like ``;;`` (``;&>`` is ``;`` and a redirect).
_SEPARATORS = (
    (";;&", "s"), (";;", "s"), (";|", "s"), ("&&", "a"), ("||", "o"), ("|&", "p"),
    (";", "s"), ("\n", "s"), ("|", "p"), ("&", "b"), ("(", "("), (")", ")"),
)  # fmt: skip
# ``NAME=value``, or ``NAME+=value`` (appends to the old value)
ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
# A redirect token (``2>&1``, ``>``, ``>|``, ``&>``, ``<<EOF``); a bare operator takes the
# next word.
REDIRECT = re.compile(r"(?:\d+|&)?[<>][<>&|]*")
_WORD_SPLIT = re.compile(r"[/=:@\s]+")
# Wrapper commands that run the next command; value is the options that take an argument.
_TIMEOUT = {"-s", "--signal", "-k", "--kill-after"}
_WRAPPERS = {
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string", "-P"},
    "sudo": {
        "-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U",
        "--user", "--group", "--host", "--prompt", "--close-from", "--chdir", "--chroot",
        "--command-timeout", "--other-user",
    },
    "doas": {"-u", "-C"},
    "command": set(),
    "time": {"-o", "-f", "--output", "--format"},
    "nohup": set(),
    "exec": {"-a"},
    "timeout": _TIMEOUT,
    "gtimeout": _TIMEOUT,
    "nice": {"-n", "--adjustment"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "caffeinate": {"-t", "-w"},
    "watch": {"-n", "--interval"},
    "coproc": set(),
    "setsid": set(),
    "arch": {"-arch", "-d", "-e"},  # macOS: ``arch -arm64 cmd``
    # util-linux: ``ionice [-c C] [-n N] [-t] cmd``, ``chrt [policy] PRIO cmd`` (``-T``/``-P``/
    # ``-D`` take a value), ``taskset [-c] MASK|LIST cmd``; ``cpulimit -l N [--] cmd`` (3.1)
    "ionice": {"-c", "-n", "-p", "-P", "-u", "--class", "--classdata", "--pid", "--pgid", "--uid"},
    "chrt": {"-T", "-P", "-D", "--sched-runtime", "--sched-period", "--sched-deadline"},
    "taskset": set(),
    "cpulimit": {
        "-l", "-p", "-e", "-P", "-c", "-s",
        "--limit", "--pid", "--exe", "--path", "--cpu", "--signal",
    },
    # GNU and BSD; GNU ``-i``/``-e``/``-l`` take only an attached value
    "xargs": {
        "-a", "-d", "-E", "-I", "-J", "-L", "-n", "-P", "-R", "-s", "-S",
        "--arg-file", "--delimiter", "--max-args", "--max-procs", "--max-chars",
        "--process-slot-var",
    },
    # zsh precommand modifiers
    "noglob": set(),
    "nocorrect": set(),
    "-": set(),
}  # fmt: skip
# Wrappers that take positional arguments before the command (``timeout DURATION cmd``).
_WRAPPER_POSITIONALS = {"timeout": 1, "gtimeout": 1, "chrt": 1, "taskset": 1}
# Wrapper options after which the program is unreadable: ``env -S STRING`` splits a string into
# the command, BSD ``env -P PATH`` searches another path.
_HIDING_OPTIONS = {"env": {"-S", "--split-string", "-P"}}
# The long options GNU xargs takes; any other ``--name`` (an abbreviation such as ``--max-a``
# may take the next word) leaves the program unreadable.
_XARGS_LONG = {
    "--null", "--arg-file", "--delimiter", "--eof", "--replace", "--max-lines", "--max-args",
    "--max-procs", "--interactive", "--verbose", "--exit", "--no-run-if-empty", "--max-chars",
    "--show-limits", "--open-tty", "--process-slot-var", "--help", "--version",
}  # fmt: skip
# Wrapper options that run the command in another folder.
_CHDIR_OPTIONS = {"env": {"-C", "--chdir"}, "sudo": {"-D", "--chdir", "-R", "--chroot"}}


def _mark_separators(command: str) -> str:
    """Replace unquoted ``; & | ( )`` and newlines with a kind-tagged sentinel word.

    Quote-aware, so ``echo "a;b"`` stays whole; fused runs such as ``;(`` or ``&&\\n``
    become several sentinels. Backslash-newline outside single quotes is a line continuation
    and is dropped (an escaped backslash before a newline is not). Comments are not recognised
    (the resolver distrusts a line that may contain one).
    """
    command = command.replace(SEP, "")
    out: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if quote == "'":
            quote = "" if ch == "'" else quote
        elif ch == "\\" and command[i + 1 : i + 2] == "\n":
            i += 2
            continue
        elif quote == '"':
            if ch == "\\" and i + 1 < len(command):
                out.append(ch)
                i += 1
                ch = command[i]
            elif ch == '"':
                quote = ""
        elif ch == "\\" and i + 1 < len(command):
            out.append(ch)
            i += 1
            ch = command[i]
        elif ch in "'\"":
            quote = ch
        elif ch in ";|()\n&" and not _is_redirect_char(command, i):
            text, tag = next((t, g) for t, g in _SEPARATORS if command.startswith(t, i))
            if command.startswith(";&", i) and command[i + 2 : i + 3] != ">":
                text = ";&"
            i += len(text) - 1
            ch = f" {SEP}{tag} "
        out.append(ch)
        i += 1
    return "".join(out)


def _is_redirect_char(command: str, i: int) -> bool:
    """An ``&`` of ``>&``/``<&``/``&>``, or the ``|`` of ``>|`` (clobber): part of a redirect."""
    if command[i] == "&":
        return (i > 0 and command[i - 1] in "<>") or command[i + 1 : i + 2] == ">"
    return command[i] == "|" and i > 0 and command[i - 1] == ">"


def lex(command: str) -> list[str]:
    """Split a command line into tokens; raises ValueError on unbalanced quotes."""
    return shlex.split(_mark_separators(command))


class Word(str):
    """A token as the shell reads it (quotes removed); ``raw`` is how it was written."""

    raw: str

    def __new__(cls, value: str, written: str) -> Self:
        word = super().__new__(cls, value)
        word.raw = written
        return word


def raw(word: str) -> str:
    """How a token was written, quotes and backslashes kept (the token itself if unknown)."""
    return getattr(word, "raw", word)


def _written(marked: str) -> list[str]:
    """The tokens of a separator-marked line as written: the same split as ``shlex``."""
    out: list[str] = []
    cur: list[str] = []
    quote = ""
    i = 0
    while i < len(marked):
        ch = marked[i]
        if quote == "'":
            quote = "" if ch == "'" else quote
        elif ch == "\\" and i + 1 < len(marked):
            cur.append(ch)
            i += 1
            ch = marked[i]
        elif quote == '"':
            quote = "" if ch == '"' else quote
        elif ch in "'\"":
            quote = ch
        elif ch in " \t\r\n":
            if cur:
                out.append("".join(cur))
                cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    if cur:
        out.append("".join(cur))
    return out


def _words(command: str) -> list[str]:
    """``lex`` with each token a ``Word`` that keeps its written form; ValueError when the two
    splits disagree (never expected), so the caller asks."""
    marked = _mark_separators(command)
    tokens = shlex.split(marked)
    written = _written(marked)
    if len(written) != len(tokens) or any(shlex.split(w) != [t] for w, t in zip(written, tokens)):
        raise ValueError("tokens do not line up with the written words")
    return [Word(t, w) for t, w in zip(tokens, written)]


def parse(command: str) -> list[tuple[str, list[str] | str]]:
    """Split a command line into ``("cmd", tokens)`` and ``("sep", kind)`` items.

    Kinds: ``seq`` (``; \\n``), ``and`` (``&&``), ``or`` (``||``), ``pipe`` (``| |&``),
    ``bg`` (``&``), ``open``/``close`` (``(`` ``)``). A ``seq`` right after a ``seq``, ``and``,
    ``or`` or ``pipe`` (``;;``, ``&&\\n``, ``||\\n``) is dropped. Each token is a ``Word``
    (see ``raw``). Raises ValueError on unbalanced quotes.
    """
    items: list[tuple[str, list[str] | str]] = []
    cur: list[str] = []
    for tok in _words(command):
        if not tok.startswith(SEP):
            cur.append(tok)
            continue
        if cur:
            items.append(("cmd", cur))
            cur = []
        kind = _KINDS[tok[len(SEP) :]]
        if (
            kind == "seq"
            and items
            and items[-1] in (("sep", "seq"), ("sep", "and"), ("sep", "or"), ("sep", "pipe"))
        ):
            continue
        items.append(("sep", kind))
    if cur:
        items.append(("cmd", cur))
    return items


_SKIP_KEYWORDS = {
    "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "{", "}", "!", "esac",
}  # fmt: skip
_LOOPS = {"for", "while", "until", "select"}
# Loops whose header holds no command: ``for x in …``, ``select …``, zsh ``foreach x (…)``.
_HEADER_LOOPS = {"for", "select", "foreach"}
# Every word that opens, continues or closes a compound command or group.
KEYWORDS = frozenset(_SKIP_KEYWORDS | _HEADER_LOOPS | {"case", "function", "repeat"})


def strip_keywords(seg: list[str]) -> tuple[str | None, list[str]]:
    """Skip leading shell keywords; return (first loop keyword seen, the rest).

    A segment that starts (after keywords) with ``for``/``select``/``foreach`` yields that
    loop keyword and no command; one starting with ``case`` yields ``"case"`` and no command.
    zsh ``repeat N`` is a loop keyword followed by its count.
    """
    loop: str | None = None
    i = 0
    while i < len(seg):
        tok = seg[i]
        if tok in _HEADER_LOOPS:
            return loop or tok, []
        if tok == "case":
            return "case", []
        if tok == "repeat":
            loop = loop or tok
            i += 2
            continue
        if tok not in _SKIP_KEYWORDS:
            return loop, seg[i:]
        if tok in _LOOPS and loop is None:
            loop = tok
        i += 1
    return loop, []


def loop_names(seg: list[str]) -> list[str]:
    """The words a loop header assigns: ``for A B in …``, ``select A``, zsh ``foreach A`` and
    ``for A`` (its ``(…)`` list is lexed apart). Empty when the segment opens no such loop."""
    i = 0
    while i < len(seg) and seg[i] in _SKIP_KEYWORDS:
        i += 1
    if i == len(seg) or seg[i] not in _HEADER_LOOPS:
        return []
    names: list[str] = []
    for tok in seg[i + 1 :]:
        if tok == "in":
            break
        names.append(tok)
    return names


def option_values(args: list[str], long_names: set[str], short: str | None = None) -> list[str]:
    """Every value of the ``long_names`` options in order: ``--opt v`` and ``--opt=v``, plus the
    attached ``-iVALUE`` form of ``short``. An option with nothing after it gives no value; a
    blank one (``--opt=``) is kept for the caller to judge."""
    values: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
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


def _options(seg: list[str], i: int, with_value: set[str]) -> tuple[list[tuple[str, str]], int]:
    """Read one option word at ``seg[i]``: ([(option, value or "")], next index). A short
    cluster (``-iu NAME``, ``-oL``) gives each letter; the first letter that takes a value takes
    the rest of the word or the next word, like ``getopt``."""
    word = seg[i]
    k = i + 1  # the value's index: redirects before it are skipped (``nice -n >/dev/null 5``)
    while k < len(seg) and (width := redirect_width(seg, k)):
        k += width
    nxt = seg[k] if k < len(seg) else ""
    if word in with_value and not word.startswith("--"):
        return [(word, nxt)], k + 1  # also a one-dash long option (``arch -arch NAME``)
    if word.startswith("--") or word == "-":
        name, eq, value = word.partition("=")
        if name in with_value and not eq:
            return [(name, nxt)], k + 1
        return [(name, value)], i + 1
    found: list[tuple[str, str]] = []
    for j, letter in enumerate(word[1:], start=2):
        opt = "-" + letter
        if opt in with_value:
            if j < len(word):
                return [*found, (opt, word[j:])], i + 1
            return [*found, (opt, nxt)], k + 1
        found.append((opt, ""))
    return found, i + 1


def _assignment(word: str) -> tuple[str, str]:
    """(name, value) of ``NAME=value``; ``NAME+=b`` gives ``${NAME}b`` (a computed value)."""
    key, _, val = word.partition("=")
    if key.endswith("+"):
        key = key[:-1]
        val = "${" + key + "}" + val
    return key, val


def program(word: str) -> str:
    """The program a command word names: its basename; zsh ``=cmd`` is ``cmd``."""
    if word.startswith("=") and len(word) > 1:
        word = word[1:]
    return os.path.basename(word)


def redirect_width(seg: list[str], i: int) -> int:
    """How many words the redirect at ``seg[i]`` takes as written (2 for a bare operator and its
    target), or 0 when ``seg[i]`` is no redirect (a quoted ``'>'`` is a plain word)."""
    m = REDIRECT.match(raw(seg[i]))
    if not m:
        return 0
    return 2 if m.end() == len(raw(seg[i])) else 1


@dataclass
class Unwrapped:
    """A segment with its prefix assignments and wrapper commands taken off."""

    assigns: dict[str, str]
    argv: list[str]
    clear_env: bool = False  # ``env -i``, ``exec -c``: the hook environment is not passed on
    hidden: bool = False  # ``env -S``/``-P``: what runs can't be read
    moved: bool = False  # ``env -C``, ``sudo -D``: it runs in another folder
    appended: bool = False  # ``xargs``: words from stdin (a flag among them) are appended
    wrappers: list[str] = field(default_factory=list)  # the wrapper names, in order
    placeholders: set[str] = field(default_factory=set)  # xargs ``-I``/``-J``/``-i`` strings
    runner_words: list[str] = field(default_factory=list)  # every word after xargs's options


def _xargs_word(word: str, out: Unwrapped) -> None:
    """An xargs option word: GNU ``-i[R]``/``--replace[=R]`` set a replace string (``{}`` by
    default); a long option it does not spell out exactly hides the program."""
    if not word.startswith("--") and "i" in word[1:]:
        # GNU ``-i[R]``, also inside a cluster (``-ri``, ``-0iR``); a value-taking letter
        # before it may hold an ``i`` too: then ``{}`` and the rest both count (asks more)
        out.placeholders.update({"{}", word.split("i", 1)[1] or "{}"})
    elif word.startswith("--r"):  # ``--replace`` and its abbreviations
        out.placeholders.add(word.partition("=")[2] or "{}")
    if word.startswith("--"):
        out.hidden = out.hidden or word.partition("=")[0] not in _XARGS_LONG


def unwrap_all(seg: list[str]) -> Unwrapped:
    """Strip leading redirects, assignments and wrapper commands (with their options).

    A variable unset with ``env -u NAME`` is recorded as an empty assignment ``NAME: ""``.
    """
    out = Unwrapped({}, [])
    i = 0
    while i < len(seg):
        tok = seg[i]
        if width := redirect_width(seg, i):  # ``>/dev/null cmd``: the redirect is no command
            i += width
            continue
        if ASSIGN.match(tok):
            key, val = _assignment(tok)
            out.assigns[key] = val
            i += 1
            continue
        name = program(tok)
        wrapper = _WRAPPERS.get(name)
        if wrapper is None:
            break
        out.appended = out.appended or name == "xargs"
        out.wrappers.append(name)
        i += 1
        while i < len(seg) and (
            seg[i].startswith("-") or ASSIGN.match(seg[i]) or redirect_width(seg, i)
        ):
            if width := redirect_width(seg, i):  # ``nice >/dev/null -n 5 cmd``
                i += width
                continue
            if ASSIGN.match(seg[i]):
                key, val = _assignment(seg[i])
                out.assigns[key] = val
                i += 1
                continue
            if seg[i] == "--":
                i += 1
                break
            if name == "xargs":
                _xargs_word(seg[i], out)
            found, i = _options(seg, i, wrapper)
            for opt, value in found:
                if name == "env" and opt in ("-i", "-", "--ignore-environment"):
                    out.clear_env = True
                elif name == "env" and opt in ("-u", "--unset") and value:
                    out.assigns[value] = ""
                elif name == "exec" and opt == "-c":
                    out.clear_env = True
                if name == "xargs" and opt in ("-I", "-J") and value:
                    out.placeholders.add(value)
                out.hidden = out.hidden or opt in _HIDING_OPTIONS.get(name, ())
                out.moved = out.moved or opt in _CHDIR_OPTIONS.get(name, ())
        if name == "xargs":
            out.runner_words = list(seg[i:])
        for _ in range(_WRAPPER_POSITIONALS.get(name, 0)):
            while i < len(seg) and (width := redirect_width(seg, i)):
                i += width  # ``timeout >/dev/null 5 cmd``
            if name == "chrt" and not (i < len(seg) and seg[i].isdigit()):
                break  # ``chrt -o cmd``: the priority is optional (digits only)
            i += 1
    out.argv = seg[i:]
    return out


def unwrap(seg: list[str]) -> tuple[dict[str, str], list[str], bool]:
    """``unwrap_all`` as (assignments, argv, clear_env)."""
    out = unwrap_all(seg)
    return out.assigns, out.argv, out.clear_env


def words(command: str) -> list[str]:
    """Lower-cased, de-duplicated, sorted words of a command, split on ``/ = : @``."""
    try:
        tokens = lex(command)
    except ValueError:
        tokens = command.split()
    found = {w.lower() for t in tokens for w in _WORD_SPLIT.split(t) if w}
    return sorted(found)
