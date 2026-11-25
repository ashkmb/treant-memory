"""Shell state carried along one command line: the folder and the environment overlay.

``State.apply`` follows one state command (``cd``, ``export``, ``unset``, ``set``, ...)
exactly where it can read it; what it can't read makes a variable unknown, or the folder and
environment ambient. Both bash and zsh are covered: the hook can't choose the shell.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")
_GLOB = re.compile(r"[*?\[]")
# ``${NAME=…}``, ``${NAME:=…}`` and zsh ``${NAME::=…}`` assign NAME while they expand.
_ASSIGNING = re.compile(r"\$\{([^}:=]*):{0,2}=")
_CD_OPTIONS = set("LPe@")
# zsh ``chdir`` is ``cd``; bash has no ``chdir``, so where it goes can't be told
CD_LIKE = {"cd", "chdir", "pushd", "popd"}
DECLARES = {"declare", "typeset", "local", "readonly"}
STATE_COMMANDS = CD_LIKE | DECLARES | {"export", "unset", "set"}
# ``declare``/``typeset`` flags followed exactly; any other makes the named variables unknown
# (``-r``: later writes fail; ``-g``: bash 3.2 rejects it, zsh takes it).
_EXACT_FLAGS = set("x-")
# ``set`` letters and ``-o`` names that change neither how variables are assigned or exported
# nor how words are read: allexport stays known. Checked in bash 3.2 and zsh 5.9; zsh rejects
# errtrace, functrace, history, monitor, ``-b`` and ``-m`` (a non-interactive zsh stops at the
# failing ``set``). Not listed: ``-P``/``physical`` (bash: physical ``cd``; zsh ``-P``:
# RC_EXPAND_PARAM) and ``-T`` (zsh: CDABLE_VARS).
_NOOP_LETTERS = set("euxvCEbmh")
_NOOP_OPTIONS = {
    "pipefail", "errexit", "nounset", "xtrace", "verbose", "noclobber", "errtrace",
    "functrace", "notify", "monitor", "ignoreeof", "emacs", "vi", "history", "hashall",
}  # fmt: skip
# Of those, the spellings both shells accept. Bash stops at a name it rejects and zsh at a
# letter it rejects, so another word of the same ``set`` may or may not take effect.
_BOTH_LETTERS = _NOOP_LETTERS - set("bm") | set("ano")
_BOTH_OPTIONS = _NOOP_OPTIONS - {"errtrace", "functrace", "history", "monitor"} | {"allexport"}
# ``set`` letters and ``-o`` names after which the line is still read (``-n``/``noexec`` is
# ignored by an interactive shell); allexport is unknown after the ones not in _NOOP_*.
_QUIET_LETTERS = _NOOP_LETTERS | {"n"}
_QUIET_OPTIONS = _NOOP_OPTIONS | {"noexec"}
# Variables that change where ``cd DIR`` goes (zsh ``cdpath`` is tied to ``CDPATH``).
_CD_SEARCH = {"CDPATH", "cdpath"}


def physical(path: str) -> str:
    """Where the kernel goes for ``path``: ``..`` after a symlink is the parent of the link's
    target, not of the link. Keeps the logical spelling when both name the same folder."""
    logical = os.path.normpath(path)
    if ".." not in path.split(os.sep):
        return logical
    real = os.path.realpath(path)
    try:
        if os.path.samefile(logical, real):
            return logical
    except OSError:
        pass
    return real


def _logical_ok(path: str) -> bool:
    """Every folder a logical ``cd`` passes before a ``..`` exists (bash and zsh check)."""
    cur = os.sep if path.startswith(os.sep) else ""
    for part in path.split(os.sep):
        if part in ("", "."):
            continue
        if part == "..":
            if not os.path.isdir(cur or "."):
                return False
            cur = os.path.dirname(cur.rstrip(os.sep)) or os.sep
        else:
            cur = os.path.join(cur, part)
    return True


def opaque(value: str) -> bool:
    """A value the shell computes when it runs (``$...`` or a backtick)."""
    return "$" in value or "`" in value


def computed(word: str) -> bool:
    """A word the shell expands: ``$``, a backtick, a glob, or braces (``{a,b}``, ``{1..3}``)."""
    braces = "{" in word and ("," in word or ".." in word)
    return opaque(word) or braces or bool(_GLOB.search(word))


def _split(word: str) -> tuple[str | None, str | None]:
    """(name, value or None) of an argument of ``export``/``declare``; name None if not one.
    ``NAME+=b`` appends, so its value is computed."""
    name, eq, value = word.partition("=")
    if eq and name.endswith("+"):
        name = name[:-1]
        value = "${" + name + "}" + value
    if not NAME.match(name):
        return None, None
    return name, value if eq else None


def _changes_names(word: str) -> bool:
    """A computed word whose expansion may name other variables (not just an assignment's
    ``$``-value, which bash and zsh neither split nor glob)."""
    name, eq, value = word.partition("=")
    braces = "{" in value and ("," in value or ".." in value)  # bash expands these
    return computed(name if eq else word) or (bool(eq) and braces)


@dataclass
class State:
    """Shell state carried from segment to segment: folder and environment overlay."""

    cwd: str
    home: str = field(compare=False)
    environ: Mapping[str, str] = field(compare=False)  # the hook environment
    exported: set[str]  # names a plain ``A=b`` changes in the environment
    overlay: dict[str, str] = field(default_factory=dict)  # exported values; "" = unset
    shell: dict[str, str] = field(default_factory=dict)  # values set but not exported
    unknown: set[str] = field(default_factory=set)  # variables whose value can't be read
    locked: set[str] = field(default_factory=set)  # read-only: unknown even to a prefix
    allexport: bool | None = False  # ``set -a``; None: can't tell
    ambient: bool = False  # the folder or environment can't be read (for the rest of the line)
    moved: bool = False  # the folder or allexport was changed on the line
    touched: set[str] = field(default_factory=set)  # variables changed on the line
    home_changed: bool = False  # ``HOME`` was changed: files under ``~`` may be other ones

    def copy(self) -> State:
        return replace(
            self,
            exported=set(self.exported),
            overlay=dict(self.overlay),
            shell=dict(self.shell),
            unknown=set(self.unknown),
            locked=set(self.locked),
            touched=set(self.touched),
        )

    def apply(self, argv: list[str], assigns: Mapping[str, str]) -> bool:
        """Follow one state command; True when nothing later on the line can be trusted."""
        name, args = argv[0], argv[1:]
        if name in CD_LIKE:
            searched = (_CD_SEARCH | {"HOME"}) & assigns.keys()
            self._move(None if searched else self._dir_target(name, args))
        elif name == "set":  # ``set $O`` may be ``set -k``
            return any(computed(a) for a in args) or self._set(args)
        elif any(_changes_names(a) for a in args):
            self.ambient = True  # ``export $V``, ``unset {A,B}``
        elif name == "unset":
            self._unset(args)
        elif name == "local":  # bash: fails outside a function; zsh: sets the variable
            self._forget_words(args)
        elif name == "readonly":  # bash 3.2 has no ``readonly -x``; later writes fail
            self._forget_words(args, lock=True)
        else:
            self._declare(["-x", *args] if name == "export" else args)
        return False

    def expansions(self, tokens: list[str]) -> None:
        """``${NAME=…}``/``${NAME:=…}`` in any word assign NAME: it becomes unknown."""
        for tok in tokens:
            for m in _ASSIGNING.finditer(tok):
                if NAME.match(m.group(1)):
                    self.forget(m.group(1))
                else:
                    self.ambient = True  # ``${!ref=…}``, zsh ``${(P)ref=…}``

    def autocd(self, word: str) -> None:
        """zsh ``AUTO_CD``: a lone word naming a folder changes into it."""
        if "/" in word or word in (".", "..") or word.startswith("~"):
            self._move(None)
            return
        folder = self._cdpath() or os.path.isdir(os.path.join(self.cwd, word))
        if folder and not self._on_path(word):
            self._move(None)

    # variables

    def _touch(self, name: str) -> None:
        self.touched.add(name)
        self.home_changed = self.home_changed or name == "HOME"

    def followed_home(self) -> str | None:
        """The ``HOME`` that ``~`` and ``cd`` alone use; None when it can't be read or is
        unset (bash: ``cd: HOME not set``)."""
        if "HOME" not in self.touched:
            return self.home
        home = None if "HOME" in self.unknown else self._value("HOME")
        return None if not home or opaque(home) else home

    def forget(self, name: str) -> None:
        self.unknown.add(name)
        self._touch(name)

    def set_exported(self, name: str, value: str) -> None:
        self.exported.add(name)
        self._touch(name)
        self.shell.pop(name, None)
        self.overlay[name] = value
        if opaque(value):
            self.unknown.add(name)

    def hide(self, name: str) -> None:
        """Unset or un-export: later commands do not see the variable."""
        self.exported.discard(name)
        self._touch(name)
        self.shell.pop(name, None)
        self.overlay[name] = ""

    def assign(self, name: str, value: str) -> None:
        """A plain ``A=b``: changes the environment only for an exported variable."""
        if self.allexport or name in self.exported:
            self.set_exported(name, value)
        elif self.allexport is None:
            self.forget(name)  # exported or not: can't tell
        else:
            self.shell[name] = value
            self._touch(name)

    def _forget_words(self, args: list[str], lock: bool = False) -> None:
        for word in args:
            name, _ = _split(word)
            if name is not None:
                self.forget(name)
                if lock:
                    self.locked.add(name)

    def _unset(self, args: list[str]) -> None:
        if not any("f" in a for a in args if a.startswith("-")):  # not functions
            for a in args:
                if NAME.match(a):
                    self.hide(a)

    def _declare(self, args: list[str]) -> None:
        """``declare``/``typeset`` (``export`` is ``declare -x``): ``-x`` exports, ``+x``
        un-exports; any other flag (``-r``, ``-g``, ``-n``, ``-a``, ``-i``, ``-u``, ``-f``, zsh
        ``-T``, ...) makes the named variables unknown, ``-r`` for the rest of the line."""
        flags = [a for a in args if a.startswith(("-", "+"))]
        words = [a for a in args if not a.startswith(("-", "+"))]
        letters = {c for f in flags for c in f[1:]}
        if not letters <= _EXACT_FLAGS:
            self._forget_words(words, lock="r" in letters)
            return
        unexport = any(f[0] == "+" and "x" in f for f in flags)
        export = any(f[0] == "-" and "x" in f for f in flags)
        for word in words:
            name, value = _split(word)
            if name is None:
                continue
            if unexport:
                self.hide(name)
                if value is not None:
                    self.shell[name] = value
            elif export:
                self._export(name, value)
            elif value is not None:
                self.assign(name, value)

    def _export(self, name: str, value: str | None) -> None:
        if value is not None:
            self.set_exported(name, value)
        elif name in self.shell:
            self.set_exported(name, self.shell[name])
        else:
            self.exported.add(name)
            self._touch(name)

    def _set(self, args: list[str]) -> bool:
        """Follow ``set`` flags; True for one that changes how later words are read."""
        i = 0
        doubtful = False  # a word one of the shells rejects
        before = self.allexport
        while i < len(args):
            a = args[i]
            if a in ("-", "--") or not a.startswith(("-", "+")):
                break
            for c in a[1:]:
                doubtful = doubtful or c not in _BOTH_LETTERS
                if c == "o":
                    i += 1
                    if i < len(args):
                        doubtful = doubtful or args[i] not in _BOTH_OPTIONS
                        if not self._set_option(args[i], a[0] == "-"):
                            return True
                elif c == "a":
                    self.allexport, self.moved = a[0] == "-", True
                elif c not in _QUIET_LETTERS:
                    return True  # bash ``-k``, zsh ``-J`` (AUTO_CD), ...
            i += 1
        if doubtful and self.allexport != before:
            self.allexport = None
        return False

    def _set_option(self, option: str, on: bool) -> bool:
        """Follow ``set -o NAME``; False when it can't be followed. Only ``allexport`` is
        followed: bash rejects zsh spellings (``ALL_EXPORT``, ``noallexport``) that zsh takes,
        so after a name other than a no-op one a plain ``A=b`` may or may not be exported."""
        if option == "allexport":
            self.allexport, self.moved = on, True
            return True
        name = option.lower().replace("_", "")
        if name not in _NOOP_OPTIONS:
            self.allexport = None
        return name in _QUIET_OPTIONS

    # folder

    def _move(self, target: str | None) -> None:
        self.ambient = self.ambient or target is None
        self.cwd = target or self.cwd
        self.moved = True

    def _dir_target(self, name: str, args: list[str]) -> str | None:
        """The folder a plain ``cd DIR``/``pushd DIR`` moves to; None when it can't be read
        (``cd -``, ``popd``, ``chdir``, a computed or globbed folder, ``CDPATH``, a folder it
        can't enter)."""
        if name in ("popd", "chdir"):
            return None
        i = 0
        modes = ""  # the -L/-P letters in order
        while i < len(args) and args[i].startswith("-") and args[i] != "-":
            if args[i] == "--":
                i += 1
                break
            if name != "cd" or not set(args[i][1:]) <= _CD_OPTIONS:
                return None
            modes += "".join(c for c in args[i][1:] if c in "LP")
            i += 1
        if "P" in modes and modes[-1] == "L":
            return None  # bash: the last one wins (logical); zsh: -P wins
        rest = args[i:]
        if len(rest) > 1 or (not rest and name != "cd"):
            return None
        d = rest[0] if rest else "~"
        if d == "-" or d.startswith("+") or computed(d):
            return None
        if d.startswith("~"):
            home = self.followed_home()
            if home is None or (d != "~" and not d.startswith("~/")):
                return None  # another user's home, or one it can't read
            d = home + d[1:]
        elif not d.startswith(("/", "./", "../")) and d not in (".", "..") and self._cdpath():
            return None
        joined = os.path.join(self.cwd, d)
        # ``-P`` follows symlinks (``..`` after a link is the target's parent) and sets the
        # physical folder; a plain ``cd`` or ``cd -L`` is logical in bash and zsh. Either way
        # each folder before a ``..`` must exist, or ``cd`` fails and stays.
        if "P" in modes:
            target = os.path.realpath(joined) if os.path.isdir(joined) else None
        else:
            target = os.path.normpath(joined) if _logical_ok(joined) else None
        return target if target and os.path.isdir(target) and os.access(target, os.X_OK) else None

    def _value(self, name: str) -> str | None:
        if name in self.shell:
            return self.shell[name]
        if name in self.overlay:
            return self.overlay[name] or None
        return self.environ.get(name)

    def _cdpath(self) -> bool:
        """``CDPATH`` is set, so a relative ``cd`` may go elsewhere."""
        if _CD_SEARCH & self.unknown:
            return True
        return any(self._value(n) for n in _CD_SEARCH)

    def _on_path(self, word: str) -> bool:
        if {"PATH", "path"} & self.touched:
            return False  # can't tell: assume not, so a folder of that name is entered
        return shutil.which(word, path=self._value("PATH") or os.defpath) is not None
