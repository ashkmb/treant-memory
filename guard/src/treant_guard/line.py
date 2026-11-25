"""One command line, walked segment by segment: the shell state carried between segments,
the commands it can't read, and which rules each command matches.

A change of folder or environment is followed exactly only through a plain chain (see
``Line``); anything the guard can't read makes the kinds that depend on it unresolved
("can't tell"). Rule matching and value lookup come from a ``RuleReader`` (``resolve``).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Protocol

from treant_guard.lexer import (
    ASSIGN,
    KEYWORDS,
    REDIRECT,
    Unwrapped,
    lex,
    loop_names,
    option_values,
    parse,
    program,
    raw,
    strip_keywords,
    unwrap,
    unwrap_all,
)
from treant_guard.rules import Rule, Switch
from treant_guard.state import (
    CD_LIKE,
    DECLARES,
    NAME,
    STATE_COMMANDS,
    State,
    computed,
    opaque,
)

Target = tuple[str, str]


def flag_values(rule: Rule, args: list[str]) -> tuple[list[str], bool]:
    """(the rule's flag values on the command, a blank one was given). The last value counts
    unless ``all_values``; a blank one (``--context ""``) leaves the tool on its default, so it
    is no value."""
    found = option_values(args, set(rule.flags), short=rule.short_flag)
    picked = found if rule.all_values else found[-1:]
    values = [v for v in picked if v.strip()]
    return values, len(values) < len(picked)


@dataclass(frozen=True)
class Scope:
    """What one command sees: its folder, environment and variables."""

    cwd: str
    environ: Mapping[str, str]
    assigns: Mapping[str, str]  # its own prefix, over variables set earlier on the line
    unknown: frozenset[str] = frozenset()  # variables whose value can't be read
    ambient: bool = False  # the folder or environment can't be read: only flag values count
    clear_env: bool = False  # ``env -i``: the hook environment is empty
    env_unknown: bool = False  # a variable that may be exported can't be read


class RuleReader(Protocol):
    def subcommand(self, rule: Rule, args: list[str]) -> tuple[bool, int | None]:
        """(the rule's subcommand filter passes, the index of the subcommand)."""
        ...

    def values(
        self, rule: Rule, cmd: str, args: list[str], sub: int | None, scope: Scope
    ) -> list[str]:
        """The rule's target values for the command; empty when it can't tell."""
        ...


# Raw text that may hide commands from the lexer: a backtick, ``$'``; a possible comment.
_HIDING = re.compile(r"`|\$'")
_COMMENT = re.compile(r"(?:^|[\s;&|()<>])#")
# Quote structures the comment scan does not read exactly: a here-document (after quoted
# ones are taken out), ``$'``, a backslash.
_AMBIGUOUS_QUOTES = re.compile(r"<<|\$'|\\")
_WORD_BREAK = " \t\n;&|()<>"
_EXPANSION_BREAK = re.compile(r"[\s;&|()<>]")
_HEREDOC = re.compile(r"(?<!<)<<(?!<)")
# Commands after which nothing on the line can be trusted, flags included: they run text
# later or elsewhere, or change how the shell reads later words (bash ``shopt``/``enable``,
# zsh ``setopt``/``unsetopt``/``emulate``/``disable``).
_UNTRUSTING = {
    "source", ".", "eval", "alias", "function", "trap",
    "setopt", "unsetopt", "emulate", "shopt", "enable", "disable",
}  # fmt: skip
# Tests after which zsh runs a command in the same segment: ``if [[ x ]] cmd``.
_TESTS = {"[[": "]]", "[": "]"}
# Keywords that open and close a compound command or group.
_OPENERS = {"if", "while", "until", "for", "select", "case", "{", "repeat", "foreach"}
_CLOSERS = {"fi", "done", "esac", "}"}


def _add(items: list, item: object) -> None:
    if item not in items:
        items.append(item)


def _may_comment(command: str) -> bool:
    """A ``#`` that may start a comment: one at the start of a word outside quotes and outside
    a quoted here-document body. Read from the raw text (any ``#`` after a word break) when
    the line does not lex, holds a quote structure the scan does not read exactly, or a ``$(``
    inside double quotes (its body is read like a line of its own)."""
    stripped = _without_quoted_heredocs(command)  # their bodies are data
    if stripped is None:
        return bool(_COMMENT.search(command))
    command = stripped
    if not _COMMENT.search(command):
        return False
    if _AMBIGUOUS_QUOTES.search(command):
        return True
    try:
        lex(command)
    except ValueError:
        return True
    quote = ""
    prev = " "  # the character before, outside quotes
    i = 0
    while i < len(command):
        ch = command[i]
        if quote == "'":
            quote = "" if ch == "'" else quote
        elif ch == "\\":
            i += 2
            prev = "x"
            continue
        elif quote == '"':
            if command.startswith("$(", i):
                return True
            quote = "" if ch == '"' else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and prev in _WORD_BREAK:
            return True
        prev = ch if not quote else "x"
        i += 1
    return False


def _hides_commands(command: str) -> bool:
    """Raw text the lexer may misread: a possible comment, a backtick, ``$'``, or a ``${…}``
    whose body (to the matching brace) holds whitespace or a separator."""
    if _HIDING.search(command) or _may_comment(command):
        return True
    start = command.find("${")
    while start >= 0:
        depth, end = 0, start + 1
        for end in range(start + 1, len(command)):
            depth += {"{": 1, "}": -1}.get(command[end], 0)
            if depth == 0:
                break
        if _EXPANSION_BREAK.search(command, start, end + 1):
            return True
        start = command.find("${", start + 2)
    return False


def _untrusting(argv: list[str], wrappers: list[str]) -> bool:
    """A command after which nothing on the line can be trusted: ``source``, ``eval``, ...,
    ``exec`` alone, a computed, globbed or brace-expanded name, or a group or keyword behind a
    wrapper."""
    if not argv:
        return "exec" in wrappers
    name = argv[0]
    if name in _UNTRUSTING or (wrappers and name in KEYWORDS):
        return True
    return name not in ("[", "[[") and computed(name)


def _may_split(argv: list[str], bare: bool) -> bool:
    """A word of a state command the shell may split into other words (bash): any ``$`` or
    backtick, except in a ``NAME=`` argument of a declaration written as a bare word."""
    keep = bare and argv[0] in DECLARES | {"export"} and raw(argv[0]) == argv[0]
    return any(opaque(w) and not (keep and ASSIGN.match(raw(w))) for w in argv[1:])


def _expands(word: str) -> bool:
    """A word the shell may turn into other words or a flag: an unquoted ``$`` or
    backtick, one in double quotes at the start of the word, or an unquoted brace expansion."""
    written = raw(word)
    quote = ""
    start = True  # nothing but quotes seen so far
    brace = False
    i = 0
    while i < len(written):
        ch = written[i]
        if quote == "'":
            quote = "" if ch == "'" else quote
            start = start and ch == "'"
        elif ch == "\\":
            i += 1
            start = False
        elif ch in "$`" and (not quote or start):
            return True
        elif ch == '"' or (ch == "'" and not quote):
            quote = "" if quote else ch
        else:
            start = False
            brace = brace or (ch == "{" and not quote)
        i += 1
    return brace and ("," in written or ".." in written)


def _test_tail(argv: list[str]) -> list[str] | None:
    """The words zsh runs after a test in the same segment (``if [[ x ]] cmd``, ``while
    [[ x ]] { cmd }``, ``if [ x ] { cmd }``), or None."""
    if argv[:1] == ["]]"]:  # ``[[ ( x ) ]] cmd``: the ``( … )`` split the test
        return argv[1:] or None
    closer = _TESTS.get(argv[0]) if argv else None
    if closer in argv[1:]:
        return argv[argv.index(closer, 1) + 1 :] or None
    if argv[:1] == ["test"] and "{" in argv:
        return argv[argv.index("{") :]
    return None


def _leading_args(words: list[str], args: tuple[str, ...], with_value: set[str]) -> bool:
    """``args`` are the leading non-option words of ``words``. Options are skipped: one in
    ``with_value`` (or not known to take none) may take the next word as its value, and every
    reading is tried, so a doubtful line matches. A computed word may be any one word."""
    states = {(0, 0)}
    seen: set[tuple[int, int]] = set()
    while states:
        i, k = states.pop()
        if k == len(args):
            return True
        if i >= len(words) or (i, k) in seen:
            continue
        seen.add((i, k))
        w = words[i]
        if w.startswith("-") and w != "-":
            states.add((i + 2, k))
            if w not in with_value:
                states.add((i + 1, k))
        elif w == args[k] or _expands(w):
            states.add((i + 1, k + 1))
    return False


def _switch_args(words: list[str], args: tuple[str, ...], with_value: set[str]) -> list[str]:
    """The literal non-option words of a switch command after its own ``args``, each also
    split at ``=`` (``kubectx new=old``). Values of options in ``with_value`` and computed
    words are left out; the next word after any other option is kept, as it may not be its
    value (an extra word can only make the guard ask)."""
    out: list[str] = []
    rest = list(args)
    skip = False
    for w in words:
        if skip:
            skip = False
            continue
        if w.startswith("-"):
            name, eq, value = w.partition("=")
            if eq and name not in with_value and not (_expands(value) or opaque(value)):
                out.extend(p for p in value.split("=") if p)  # ``--ctx=prod``
            skip = not eq and w in with_value
            continue
        if rest and w == rest[0]:
            rest.pop(0)
            continue
        if _expands(w) or opaque(w):
            continue
        out.extend(p for p in w.split("=") if p)
    return out


def _short_values(args: list[str], letters: str) -> list[tuple[str, str]]:
    """``(letter, value)`` for each ``-L VALUE`` / ``-LVALUE`` among ``args`` (``L`` in
    ``letters``), until the first non-option word."""
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(args) and args[i].startswith("-") and args[i] != "--":
        word = args[i]
        for j, c in enumerate(word[1:], start=2):
            if c in letters:
                value = word[j:] or (args[i + 1] if i + 1 < len(args) else "")
                out.append((c, value))
                i += 0 if word[j:] else 1
                break
        i += 1
    return out


def _body(argv: list[str]) -> list[str] | None:
    """Words of a command hidden in the same segment, or None: after a test (zsh ``if [[ x ]]
    cmd``), after an unquoted ``{`` word past the command word (zsh ``if { x } { cmd }``,
    ``while x { cmd }``, ``coproc N { cmd }``), or after a wrapper (``time { cmd; }``)."""
    tail = _test_tail(argv)
    if tail is not None:
        return tail
    if argv and argv[0] in KEYWORDS and strip_keywords(argv)[1] != argv:
        return argv
    for i in range(1, len(argv)):
        if argv[i] == "{" and raw(argv[i]) == "{":
            return argv[i + 1 :] or None
    return None


# bash ``mapfile``/``readarray``: options that take a value; ``-C`` runs it like ``eval``.
_MAPFILE = {"mapfile", "readarray"}
_MAPFILE_VALUES = set("dnOsuCc")


def _mapfile_words(args: list[str]) -> tuple[list[str], list[str]]:
    """(the words of a ``mapfile`` that are no option or option value: the array name, the
    ``-C`` callbacks)."""
    names: list[str] = []
    callbacks: list[str] = []
    i = 0
    while i < len(args):
        word = args[i]
        if word.startswith("-") and len(word) > 1:
            for j, c in enumerate(word[1:], start=2):
                if c in _MAPFILE_VALUES:
                    value = word[j:] or (args[i + 1] if i + 1 < len(args) else "")
                    callbacks += [value] if c == "C" else []
                    i += 0 if word[j:] else 1  # ``-n 1`` or ``-n1``
                    break
        else:
            names.append(word)
        i += 1
    return names, callbacks


_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "mksh", "yash", "csh", "tcsh"}
# Shells whose options differ from ``sh``'s (``fish -C``, ``nu --commands``, ``pwsh
# -Command``, abbreviated as ``-c``): every word may be command text.
_OTHER_SHELLS = {"fish", "nu", "pwsh", "xonsh"}
_ALL_SHELLS = _SHELLS | _OTHER_SHELLS
# Long options that take the command string, and ones whose value is a file (no script).
_COMMAND_OPTIONS = {"--command", "--init-command"}
_FILE_OPTIONS = {"--rcfile", "--init-file"}
# Commands that run their argument text later: ``eval``, ``trap``, ``alias``, zsh ``emulate
# … -c``.
_TEXT_RUNNERS = {"eval", "trap", "alias", "emulate"}
# ``find`` actions that run a command; GNU ``parallel`` runs its words (or a string) per input.
_FIND_RUNS = {"-exec", "-execdir", "-ok", "-okdir"}
_PARALLEL_SOURCES = {":::", ":::+", "::::", "::::+"}
# GNU ``parallel`` options that take the next word; an unknown ``--option`` hides the command.
_PARALLEL_VALUES = {
    "-a", "-C", "-d", "-E", "-I", "-j", "-J", "-L", "-l", "-n", "-N", "-P", "-S", "-s",
    "--arg-file", "--colsep", "--delimiter", "--jobs", "--max-args", "--max-lines",
    "--max-replace-args", "--sshlogin", "--max-chars", "--joblog", "--results", "--tmpdir",
    "--timeout", "--delay", "--retries", "--memfree", "--load", "--halt", "--tag-string",
    "--workdir", "--env", "--profile",
}  # fmt: skip
_PARALLEL_FLAGS = {
    "--keep-order", "--group", "--ungroup", "--line-buffer", "--tag", "--verbose",
    "--dry-run", "--null", "--quote", "--no-run-if-empty", "--progress", "--bar", "--eta",
}  # fmt: skip


def _parallel_command(args: list[str]) -> list[str] | None:
    """The command words of GNU ``parallel`` (before ``:::``); None for an option it does not
    know. Empty when stdin or the ``:::`` values are the commands."""
    i = 0
    while i < len(args) and args[i] not in _PARALLEL_SOURCES:
        word = args[i]
        if word == "--":
            i += 1
            break
        if not word.startswith("-") or word == "-":
            break
        name, eq, _ = word.partition("=")
        if name.startswith("--") and not eq and name not in _PARALLEL_VALUES | _PARALLEL_FLAGS:
            return None
        if word[:2] in ("-I", "-U") or "replace" in name:
            return None  # a replace string of its own: the guard does not track it
        i += 2 if name in _PARALLEL_VALUES and not eq else 1
    end = next((j for j in range(i, len(args)) if args[j] in _PARALLEL_SOURCES), len(args))
    return args[i:end]


# Programs that never run their arguments as commands: a placeholder for them is only data.
_INERT = {
    "echo", "printf", "cat", "cp", "mv", "rm", "ls", "stat", "file", "wc", "head", "tail",
    "grep", "egrep", "fgrep", "basename", "dirname", "touch", "mkdir", "rmdir", "chmod",
    "chown", "ln", "gzip", "gunzip", "du", "readlink", "realpath",
}  # fmt: skip


def _filled_in(words: list[str], placeholders: set[str], argv: list[str]) -> bool:
    """A placeholder in any of a runner's command words, for a program (after the listed
    wrappers) that may run it: anything but ``_INERT``. A guarded program counts too: the
    value may be a flag. ``_INERT`` holds only when every placeholder comes after the program
    word (BSD ``xargs -J`` fills in several words: in a wrapper's option it can name the
    program) and none is a parallel ``{=…=}`` (Perl code, unquoted shell text)."""
    used = [i for i, w in enumerate(words) if any(p and p in w for p in placeholders)]
    if not used:
        return False
    if any("{=" in w for w in words):
        return True
    first_arg = len(words) - len(argv) + 1  # argv is a suffix of words
    return program(argv[0]) not in _INERT or min(used) < first_arg


def _runs_unreadable(prog: str, placeholders: set[str]) -> bool:
    """A runner's program that the guard can't name: a placeholder (``{}``, ``{1}``, an
    xargs replace string), a shell, or a computed word."""
    hit = any(p and p in prog for p in placeholders)
    return hit or program(prog) in _ALL_SHELLS or computed(prog)


def _backtick_end(text: str, j: int) -> int:
    while j < len(text):
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == "`":
            return j
        j += 1
    return len(text)


def _paren_end(text: str, j: int, quotes: bool = True) -> int:
    """Index of the ``)`` that closes a ``$(`` whose body starts at ``j``; with ``quotes``,
    parens inside quotes do not count. A quote that never closes (an apostrophe in a here-
    document) is read again without quotes."""
    depth, quote, start = 1, "", j
    while j < len(text):
        c = text[j]
        if quote == "'":
            quote = "" if c == "'" else quote
        elif c == "\\":
            j += 2
            continue
        elif quote == '"':
            quote = "" if c == '"' else quote
        elif quotes and c in "'\"":
            quote = c
        elif c in "()":
            depth += 1 if c == "(" else -1
            if depth == 0:
                return j
        j += 1
    return _paren_end(text, start, quotes=False) if quotes else len(text)


_HEREDOC_WORD = re.compile(r"<<(-?)[ \t]*((?:[^\s;&|()<>'\"\\]|'[^']*'|\"[^\"]*\"|\\.)+)")
_CASE_WORD = re.compile(r"(?:^|(?<=[\s;&|(]))case(?![\w-])")


def _heredoc_paren_end(text: str, j: int) -> int | None:
    """Like ``_paren_end``, but a here-document body is data (zsh, bash 4+). None when a
    here-document has no end line or the body holds ``case`` (bash 3.2 ends ``$(`` at a
    pattern's ``)``)."""
    depth, quote = 1, ""
    pending: list[tuple[str, bool]] = []  # here-documents whose body starts at the next line
    while j < len(text):
        c = text[j]
        if quote == "'":
            quote = "" if c == "'" else quote
        elif c == "\\":
            j += 2
            continue
        elif quote == '"':
            quote = "" if c == '"' else quote
        elif c in "'\"":
            quote = c
        elif text.startswith("<<<", j):
            j += 3  # a here-string: no body
            continue
        elif text.startswith("$((", j) and (end := _arith_end(text, j)) is not None:
            j = end  # ``$((1<<2))``: a shift
            continue
        elif m := _HEREDOC_WORD.match(text, j):
            delim = re.sub(r"\\(.)|['\"]", r"\1", m.group(2))
            pending.append((delim, bool(m.group(1))))
            j = m.end()
            continue
        elif c == "\n" and pending:
            j += 1
            for delim, tabs in pending:
                while True:
                    if j >= len(text):
                        return None
                    nl = text.find("\n", j)
                    line = text[j : nl if nl >= 0 else len(text)]
                    j = len(text) if nl < 0 else nl + 1
                    if (line.lstrip("\t") if tabs else line) == delim:
                        break
            pending = []
            continue
        elif _CASE_WORD.match(text, j):
            return None
        elif c in "()":
            depth += 1 if c == "(" else -1
            if depth == 0:
                return j
        j += 1
    return None if pending else len(text)


def _unreliable_substitutions(command: str) -> bool:
    """A ``$( … )`` whose end bash 3.2 (counting parens, even in a here-document body) and
    zsh (reading the body as data) may put in different places."""
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if quote == "'":
            quote = "" if ch == "'" else quote
        elif ch == "\\":
            i += 2
            continue
        elif command.startswith("$(", i) and not command.startswith("$((", i):
            if "<<" in command[i:] or _CASE_WORD.search(command, i + 2):
                end = _heredoc_paren_end(command, i + 2)
                if end is None or end != _paren_end(command, i + 2):
                    return True
        elif ch == '"' or (ch == "'" and not quote):
            quote = "" if quote else ch
        i += 1
    return False


def _hidden_bodies(command: str) -> list[str]:
    """Bodies the lexer reads as plain text: backtick substitutions (outside single quotes) and
    ``$( … )`` inside double quotes. An unquoted ``$( … )`` is lexed as a subshell already."""
    bodies: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if quote == "'":
            quote = "" if ch == "'" else quote
        elif ch == "\\":
            i += 2
            continue
        elif ch == "`":
            end = _backtick_end(command, i + 1)
            bodies.append(command[i + 1 : end])
            i = end + 1
            continue
        elif quote == '"' and command.startswith("$(", i):
            end = _paren_end(command, i + 2)
            bodies.append(command[i + 2 : end])
            i = end + 1
            continue
        elif ch == '"' or (ch == "'" and not quote):
            quote = "" if quote else ch
        i += 1
    return bodies


def _arith_end(text: str, i: int) -> int | None:
    """Index after the ``))`` that closes the ``$((`` at ``i``, read as arithmetic the way
    the shells test it: the matching close is ``))`` with nothing between. None otherwise:
    ``$((cmd) )`` is a substitution of a subshell."""
    depth = 2
    for j in range(i + 3, len(text)):
        if text[j] in "'\"\\`\n":
            return None  # bash reads quotes there, zsh does not: no single reading
        depth += {"(": 1, ")": -1}.get(text[j], 0)
        if depth == 1:
            return j + 2 if text[j + 1 : j + 2] == ")" else None
    return None


class _Unreadable(ValueError):
    """Here-documents the scan can't place exactly."""


def _delimiter(word: str) -> tuple[str, bool]:
    """(the end word of a here-document as the shell reads ``word``, quoted). Any quote or
    backslash in the word makes the body data; ``$`` or a backtick is not read."""
    if "$" in word or "`" in word:
        raise _Unreadable(word)
    return re.sub(r"\\(.)|['\"]", r"\1", word), any(c in word for c in "'\"\\")


def _strip_heredocs(text: str, i: int, nested: bool) -> tuple[str, int]:
    """``text[i:]`` with the bodies of quoted here-documents taken out (their ``<<`` line and
    end line kept); with ``nested``, up to and including the ``)`` that closes a ``$(``. Quote
    aware, and a ``$(`` inside double quotes is read like a line of its own. Raises
    ``_Unreadable`` when a here-document has no end line or a quote or ``$(`` never closes."""
    out: list[str] = []
    quote = ""
    depth = 0
    pending: list[tuple[str, bool, bool]] = []  # (end word, ``<<-``, quoted)
    while i < len(text):
        c = text[i]
        if quote == "'":
            quote = "" if c == "'" else quote
        elif c == "\\":
            out.append(text[i : i + 2])
            i += 2
            continue
        elif text.startswith("$((", i) and (end := _arith_end(text, i)) is not None:
            out.append(text[i:end])  # ``$((1<<2))``: a shift
            i = end
            continue
        elif text.startswith("$(", i) and not text.startswith("$((", i):
            inner, i = _strip_heredocs(text, i + 2, nested=True)
            out.append("$(" + inner)
            continue
        elif quote == '"':
            quote = "" if c == '"' else quote
        elif c in "'\"":
            quote = c
        elif c == "#" and (i == 0 or text[i - 1] in _WORD_BREAK):
            end = text.find("\n", i)  # a comment: ``# <<'Q'`` starts no here-document
            end = len(text) if end < 0 else end
            out.append(text[i:end])
            i = end
            continue
        elif text.startswith("<<<", i):
            out.append("<<<")  # a here-string: no body
            i += 3
            continue
        elif m := _HEREDOC_WORD.match(text, i):
            delim, quoted = _delimiter(m.group(2))
            pending.append((delim, bool(m.group(1)), quoted))
            out.append(m.group())
            i = m.end()
            continue
        elif c == "\n" and pending:
            out.append(c)
            i += 1
            for delim, tabs, quoted in pending:
                while True:
                    if i >= len(text):
                        raise _Unreadable(delim)
                    nl = text.find("\n", i)
                    end = len(text) if nl < 0 else nl + 1
                    line = text[i:end]
                    i = end
                    if (line.lstrip("\t") if tabs else line).rstrip("\n") == delim:
                        out.append(line)
                        break
                    if not quoted:
                        out.append(line)
            pending = []
            continue
        elif nested and c in "()":
            depth += 1 if c == "(" else -1
            if depth < 0:
                out.append(c)
                return "".join(out), i + 1
        out.append(c)
        i += 1
    if quote or pending or nested:
        raise _Unreadable(text[-20:])
    return "".join(out), i


def _without_quoted_heredocs(text: str) -> str | None:
    """``text`` with the bodies of quoted here-documents taken out (they are data), or None
    when the here-documents can't be placed exactly."""
    if "<<" not in text:
        return text
    try:
        return _strip_heredocs(text, 0, nested=False)[0]
    except _Unreadable:
        return None


def _without_redirects(tokens: list[str]) -> list[str]:
    out: list[str] = []
    words = iter(tokens)
    for tok in words:
        if not (m := REDIRECT.match(tok)):
            out.append(tok)
        elif m.end() == len(tok):
            next(words, None)  # a bare operator: its target is the next word
    return out


class Line:
    """Walks one parsed command line, following the shell state between segments.

    A change is followed exactly only through a plain chain: only ``;``/newline/``&&`` from it
    to the reader, no ``||`` in its and-or list, not in a group, compound command or pipeline.
    Any other change, or one in a multi-member ``&&`` list once it ends, blurs every change so
    far for the rest of the line. ``source``, ``eval``, ``alias``, a function, ``exec`` alone,
    ``case`` or a change after a here-document starts: later rules are unresolved. A comment,
    backtick or ``$'`` in the raw text, or a loop with a change: every rule is unresolved.
    The shell may be bash or zsh, so zsh forms (``chdir``, ``AUTO_CD``, ``repeat``, ``if [[ x ]]
    cmd``) count. Words straight after ``)`` make later rules unresolved too. In a matched
    command, a word that may expand into other words (``$X``, ``"$X"``, ``{a,b}``) leaves its
    rules unresolved, flags included.
    """

    def __init__(
        self,
        rules: list[Rule],
        reader: RuleReader,
        cwd: str,
        command: str,
        environ: Mapping[str, str],
        home: str,
        switches: tuple[Switch, ...] = (),
        switch_words: list[Target] | None = None,
    ):
        self.rules = rules
        # literal words a switch command was given (``kubectx prod``): (kind, word) pairs
        self.switch_words = switch_words
        self.switches: tuple[Switch, ...] = tuple(switches)
        names = {os.path.basename(c) for r in rules for c in r.command}
        names |= {os.path.basename(c) for sw in self.switches for c in sw.command}
        alternatives = "|".join(map(re.escape, sorted(names))) or "(?!)"
        # a guarded program named as a word of some text
        self.guarded = re.compile(r"(?<![\w.-])(?:" + alternatives + r")(?![\w.-])")
        self.command = command
        self.switched: set[str] = set()  # kinds a tool-level switch changed earlier
        self.reader = reader
        self.environ = environ
        self.state = State(cwd, home, environ, exported=set(environ))
        self.depth = 0  # unclosed ``(``, ``{`` and compound commands
        self.list_joined = False  # the current and-or list has ``&&`` or ``||``
        self.list_or = False  # ... has ``||``
        self.list_changed = False  # ... made a state change
        self.untrusted = _hides_commands(command)
        self.quoted = any(c in command for c in "'\"\\")  # quotes are gone after lexing
        self.loop = False
        self.effect = False  # any segment changed the state
        self.heredoc = False  # a here-document started earlier on the line
        self.matched = False
        self.kinds: list[str] = []
        self.targets: list[Target] = []
        self.unresolved: list[str] = []

    def walk(
        self, items: list[tuple[str, list[str] | str]]
    ) -> tuple[bool, list[Target], list[str]]:
        bodies = _hidden_bodies(self.command)
        if _unreliable_substitutions(self.command) or any(map(self._body_runs_guarded, bodies)):
            self._match_every()  # ``"$(kubectl …)"``, `` `aws …` ``: not split by the lexer
        for i, (what, value) in enumerate(items):
            if what == "sep":
                self._separator(value)
                continue
            before = items[i - 1] if i else None
            self._segment(value, in_pipe=before == ("sep", "pipe"), after=before)
            if items[i + 1 : i + 3] == [("sep", "open"), ("sep", "close")]:
                self._distrust()  # ``name() { … }``
        if self.loop and self.effect:
            return self.matched, [], list(self.kinds)
        return self.matched, self.targets, self.unresolved

    def _distrust(self) -> None:
        self.untrusted = True
        self.effect = True

    def _blur(self) -> None:
        """Whatever the line changed so far may or may not have happened, from here on."""
        st = self.state
        st.ambient = st.ambient or st.moved
        st.unknown |= st.touched

    def _separator(self, kind: str) -> None:
        if kind not in ("seq", "and") or (kind == "seq" and self.list_joined and self.list_changed):
            self._blur()
        if kind in ("seq", "bg"):
            self.list_joined = self.list_or = self.list_changed = False
        elif kind in ("and", "or"):
            self.list_joined = True
            self.list_or = self.list_or or kind == "or"
        elif kind == "open":
            self.depth += 1
        elif kind == "close":
            self.depth = max(0, self.depth - 1)

    def _segment(self, tokens: list[str], in_pipe: bool, after: tuple | None) -> None:
        keyword, rest = strip_keywords(tokens)
        lead = tokens[: len(tokens) - len(rest)]
        if keyword == "case":
            self._distrust()
        elif keyword is not None:
            self.loop = True
            self._loop_names(tokens)
        self._assigned_names(rest)
        if after == ("sep", "close") and _without_redirects(tokens):
            self._distrust()  # words straight after ``)``: ``if (( x )) cmd``, ``A=$(x) cmd``
        if not self.untrusted:
            self._expand(tokens)
        cmd = unwrap_all(rest)
        while (body := _body(cmd.argv)) is not None:
            # ``if [[ x ]] cmd``, zsh ``if { x } { cmd }``, ``time { cmd; }``, ``coproc N { … }``
            self._distrust()
            cmd = unwrap_all(strip_keywords(body)[1])
        if self._hidden(cmd):
            self._match_every()
        elif cmd.argv:
            expands = any(_expands(w) for w in rest)
            self._match(cmd, expands)
            self._switch(cmd.argv, cmd.appended)
        if not self.untrusted:
            plain = not lead and self.depth == 0 and not self.list_or and not in_pipe
            self._follow(rest, plain)
        opened = sum(t in _OPENERS for t in lead) - sum(t in _CLOSERS for t in lead)
        self.depth = max(0, self.depth + opened)
        self.heredoc = self.heredoc or any(_HEREDOC.search(t) for t in tokens)

    def _loop_names(self, tokens: list[str]) -> None:
        """A loop variable takes values the guard does not follow: unknown from here on.
        Not a state change of its own, so a loop over other names still reads as usual."""
        for name in loop_names(tokens):
            if NAME.match(name):
                self.state.forget(name)
            elif opaque(name):
                self.state.ambient = True

    def _assigned_names(self, rest: list[str]) -> None:
        """``printf -v NAME``, zsh ``print -v NAME``, ``read NAME…``, ``getopts S NAME``, bash
        ``mapfile``/``readarray NAME``: NAME takes a value the guard does not read. Like a loop
        variable, not a state change."""
        argv = unwrap_all(rest).argv
        name = argv[0] if argv else ""
        if name in ("printf", "print"):
            names = [v for o, v in _short_values(argv[1:], "v")]
        elif name in ("read", "getopts"):
            names = [w for w in argv[1:] if not w.startswith("-")]
        elif name in _MAPFILE:
            names = _mapfile_words(argv[1:])[0]
        else:
            return
        for word in names:
            if NAME.match(word):
                self.state.forget(word)

    def _switch(self, argv: list[str], appended: bool = False) -> None:
        """A switch command changes its kinds on disk: unknown for every later segment. With
        words appended from stdin (``xargs``), any switch of the program may be the one."""
        cmd = program(argv[0])
        with_value = {f for r in self.rules if cmd in r.command for f in r.global_flags_with_value}
        for sw in self.switches:
            if cmd in sw.command and (appended or _leading_args(argv[1:], sw.args, with_value)):
                self.switched.update(sw.kinds)
                if self.switch_words is not None and not appended:
                    for word in _switch_args(argv[1:], sw.args, with_value):
                        for kind in sw.kinds:
                            _add(self.switch_words, (kind, word))

    def _expand(self, tokens: list[str]) -> None:
        """``${NAME:=…}`` assigns while the segment's words expand, before its command runs."""
        before = self.state.copy()
        self.state.expansions(tokens)
        if self.state != before:
            self.effect = self.list_changed = True

    def _follow(self, rest: list[str], plain: bool) -> None:
        """Apply the segment's own state change, if any; blur it unless ``plain``."""
        words = _without_redirects(rest)
        plain = plain and (len(words) == len(rest) or not self.quoted)  # ``'>'`` is no redirect
        assigns, argv, _ = unwrap(words)
        n = next((i for i, t in enumerate(words) if not ASSIGN.match(t)), len(words))
        wrappers = words[n : len(words) - len(argv)]
        builtin = argv[:1] == ["builtin"]
        argv = argv[1:] if builtin else argv
        if _untrusting(argv, wrappers):
            self._distrust()
            return
        before = self.state.copy()
        if not argv and not wrappers:
            for name, value in assigns.items():
                self.state.assign(name, value)
        elif argv and argv[0] in STATE_COMMANDS:
            if argv[0] not in CD_LIKE:
                for name in assigns:  # bash keeps ``A=x export B``'s prefix; zsh does not
                    self.state.forget(name)
            split = argv[0] not in CD_LIKE and _may_split(argv, not (builtin or assigns))
            if argv[0] == "set" and split:
                self._distrust()  # ``set $O`` may be ``set -k``
                return
            if wrappers or split:
                self.state.ambient = True  # ``env cd x`` does nothing, ``command cd x`` does
            elif self.state.apply(argv, assigns):
                self._distrust()
                return
        elif len(argv) == 1:
            self.state.autocd(argv[0])
        if self.state == before:
            return
        self.effect = True
        self.list_changed = True
        if self.heredoc:
            self.state = before
            self._distrust()
        elif not plain:
            self._blur()

    def _shell_hides(self, argv: list[str]) -> bool:
        """``sh -c STRING`` (``--command``) whose string names a guarded command or is
        computed, or a shell reading its commands from stdin (``… | bash``, ``bash -s``);
        ``eval``, ``trap``, ``alias`` or ``emulate`` with such text."""
        if not argv:
            return False
        shell = program(argv[0])
        if argv[0] in _TEXT_RUNNERS:
            return self._names_guarded(argv[1:])
        if shell == "parallel":
            words = _parallel_command(argv[1:])
            if words is None or self._names_guarded(argv[1:]):
                return True
            return self._runner_hides(words, {"{"})
        if shell == "find":
            return self._find_hides(argv[1:])
        if argv[0] in _MAPFILE:
            return self._names_guarded(_mapfile_words(argv[1:])[1])
        if shell in _OTHER_SHELLS:
            encoded = shell == "pwsh" and any(w.lower().startswith("-e") for w in argv[1:])
            stdin = all(w.startswith("-") for w in argv[1:])
            return encoded or stdin or self._names_guarded(argv[1:])
        if shell not in _SHELLS:
            return False
        letters, operands, command = "", [], False
        words = iter(argv[1:])
        for w in words:
            if w.startswith("--"):
                name, eq, value = w.partition("=")
                if name in _COMMAND_OPTIONS:
                    command = True
                    operands += [value] if eq else []
                elif name in _FILE_OPTIONS and not eq:
                    next(words, None)  # ``--rcfile FILE``
            elif len(w) > 1 and w[0] in "-+":
                letters += w[1:]
                if w[-1] in "oO":
                    next(words, None)  # ``-o pipefail``
            else:
                operands.append(w)
        if "c" in letters or command:
            return self._names_guarded(operands)
        return not operands or "s" in letters

    def _hidden(self, cmd: Unwrapped) -> bool:
        """What runs can't be read: ``env -S``, a hiding shell, or under ``xargs`` a program
        that stdin gives (``xargs nice``), a replace string, a shell or a ``$``."""
        if cmd.hidden or self._shell_hides(cmd.argv):
            return True
        if not cmd.appended:
            return False
        if not cmd.argv:  # bare ``xargs`` runs echo; ``xargs nice`` runs what stdin names
            return cmd.wrappers[-1] != "xargs"
        if _filled_in(cmd.runner_words, cmd.placeholders, cmd.argv):
            return True  # a replace string anywhere: stdin fills in what runs
        return _runs_unreadable(cmd.argv[0], cmd.placeholders)

    def _find_hides(self, args: list[str]) -> bool:
        """``find … -exec CMD … ;`` (every action): CMD, behind its wrappers, is missing, the
        ``{}`` placeholder (a found file), a shell or computed, or names a guarded program."""
        for i, word in enumerate(args):
            if word not in _FIND_RUNS:
                continue
            run: list[str] = []
            for w in args[i + 1 :]:
                if w == ";" or (w == "+" and run[-1:] == ["{}"]):
                    break
                run.append(w)
            if self._names_guarded(run) or self._runner_hides(run, {"{}"}):
                return True
        return False

    def _runner_hides(self, words: list[str], placeholders: set[str]) -> bool:
        """Command words a runner (``find -exec``, ``parallel``) fills in: behind their
        wrappers, nothing is left, or the program is a placeholder, a shell or computed."""
        cmd = unwrap_all(words)
        if cmd.hidden or not cmd.argv or self._shell_hides(cmd.argv):
            return True
        if _filled_in(words, placeholders, cmd.argv):
            return True  # a placeholder anywhere, behind any wrapper: the input fills it in
        return _runs_unreadable(cmd.argv[0], placeholders | cmd.placeholders)

    def _names_guarded(self, words: list[str]) -> bool:
        """Command text that names a guarded program or is computed."""
        return any(_expands(w) or opaque(w) or self.guarded.search(w) for w in words)

    def _runs_guarded(self, argv: list[str]) -> bool:
        """The command would match a rule or a switch, or can't be named."""
        if argv[0] not in ("[", "[[") and computed(argv[0]):
            return True
        cmd, args = program(argv[0]), argv[1:]
        if any(cmd in r.command and self.reader.subcommand(r, args)[0] for r in self.rules):
            return True
        with_value = {f for r in self.rules if cmd in r.command for f in r.global_flags_with_value}
        return any(
            cmd in sw.command and _leading_args(args, sw.args, with_value) for sw in self.switches
        )

    def _body_runs_guarded(self, body: str, depth: int = 0) -> bool:
        """A substitution body the lexer did not split runs a guarded command (or may)."""
        if depth > 8 or _unreliable_substitutions(body) or _may_comment(body):
            return True  # a comment in the body may hide its end or a command
        stripped = _without_quoted_heredocs(body)
        if stripped is None:
            return True  # a here-document it can't place: the body may run anything
        body = stripped
        try:
            items = parse(body)
        except ValueError:
            return self.guarded.search(body) is not None
        for what, seg in items:
            if what != "cmd":
                continue
            cmd = unwrap_all(strip_keywords(seg)[1])
            while (inner := _body(cmd.argv)) is not None:
                cmd = unwrap_all(strip_keywords(inner)[1])
            if self._hidden(cmd):
                return True
            if cmd.argv and self._runs_guarded(cmd.argv):
                return True
        return any(self._body_runs_guarded(b, depth + 1) for b in _hidden_bodies(body))

    def _match_every(self) -> None:
        """A command the guard can't name: every rule matches and none can tell."""
        self._distrust()
        for rule in self.rules:
            self.matched = True
            _add(self.kinds, rule.kind)
            _add(self.unresolved, rule.kind)

    def _scope(self, assigns: dict[str, str], clear_env: bool) -> Scope:
        st = self.state
        # ``A=$X``, ``A+=b``; a read-only variable keeps its value whatever the prefix says
        computed_here = {k for k, v in assigns.items() if opaque(v)} | st.locked
        if clear_env:
            unknown = frozenset(computed_here)
            return Scope(st.cwd, {}, assigns, unknown, st.ambient, clear_env=True)
        unknown = (st.unknown - assigns.keys()) | computed_here
        exported = st.exported | self.environ.keys() | assigns.keys()
        return Scope(
            st.cwd,
            self.environ,
            {**st.overlay, **assigns},
            frozenset(unknown),
            st.ambient,
            env_unknown=bool(unknown & exported) or (st.allexport is not False and bool(unknown)),
        )

    def _match(self, cmd: Unwrapped, expands: bool) -> None:
        """Resolve each rule the command matches; ``expands``: a word of the segment may turn
        into other words (``_expands``), so no value, flags included, can be read. A wrapper
        that runs it in another folder (``env -C``, ``sudo -D``) leaves only flag values."""
        argv = cmd.argv
        if argv[0] not in ("[", "[[") and computed(argv[0]):
            self._match_every()  # ``$K …``, ``{kubectl,} …``: it may be any of them
            return
        name, args = program(argv[0]), argv[1:]
        scope = self._scope(cmd.assigns, cmd.clear_env)
        if cmd.moved:
            scope = replace(scope, ambient=True)
        for rule in self.rules:
            if name not in rule.command:
                continue
            passes, sub = self.reader.subcommand(rule, args)
            if not passes:
                continue
            self.matched = True
            _add(self.kinds, rule.kind)
            blind = self.untrusted or expands or cmd.appended
            # a changed ``HOME`` changes files under ``~`` (configs, ``.gitconfig``,
            # ``.kube/config``): only the folder resolver reads none
            homeless = self.state.home_changed and rule.resolver != "terraform_dir"
            if blind:
                values = []
            elif rule.kind in self.switched:
                # the kind changed on disk: only a value given on this command still counts
                values = flag_values(rule, args)[0] if rule.resolver is None else []
            else:
                here = replace(scope, ambient=True) if homeless else scope
                values = self.reader.values(rule, name, args, sub, here)
            for value in values:
                _add(self.targets, (rule.kind, value))
            # a blank flag value: the default read in its place is a guess, so can't tell too
            if not values or (rule.resolver is None and flag_values(rule, args)[1]):
                _add(self.unresolved, rule.kind)
