"""Code tokens in fact text, for the stale-fact check.

Pure functions: pick the file paths and code identifiers a fact names, so the check can
look them up with ``git ls-files`` / ``git grep``. Precision over recall: anything
uncertain (URLs, hostnames, globs, absolute or home paths, commands, scrubbed values) is
not a token. Every ``Token.raw`` is an exact substring of the fact text.
"""

import re
from collections.abc import Sequence
from typing import Literal, NamedTuple

from treant_client.scrub import sanitize

MAX_TOKENS = 10
MIN_LEN = 3
MAX_LEN = 120

CHECKED_TYPES = frozenset(
    {"fact", "decision", "lesson", "procedure", "warning", "preference", "open_thread"}
)

_EXTS = frozenset(
    {
        "py", "ts", "tsx", "js", "jsx", "mjs", "go", "rs", "java", "kt", "rb", "sh",
        "md", "toml", "yml", "yaml", "json", "sql", "html", "css", "j2", "tf", "hcl",
        "cfg", "ini", "conf", "txt", "lock", "service", "timer", "env",
    }
)  # fmt: skip

_HISTORY = re.compile(
    r"(?i)\b(?:renamed|removed|deleted|dropped|replaced|retired|deprecated|formerly"
    r"|no\s+longer|used\s+to|instead\s+of|moved\s+to|moved\s+from)\b"
)

_BACKTICK = re.compile(r"`([^`\n]*)`")
_FORBIDDEN = ("://", "*", "{", "}", "<", ">", "$", "[removed]")
_DIR_SEGMENT = re.compile(r"\.?[A-Za-z0-9_-]+")
_FILE_SEGMENT = re.compile(r"[A-Za-z0-9_.-]+")
_LINE_SUFFIX = re.compile(r":\d+$")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SNAKE = re.compile(r"(?=[A-Za-z0-9_]*[A-Za-z])[A-Za-z0-9_]*_[A-Za-z0-9_]*")
_CAMEL = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_LOWER_UPPER = re.compile(r"[a-z][A-Z]")
_HOST_TLD = re.compile(r"[a-z]{2,3}")

# relative path in running text: one or more directory segments, then a file name;
# the extension and the right-hand context are checked in code
_UNQUOTED_PATH = re.compile(
    r"(?<![/:.~\w])(?:\.?[A-Za-z0-9_-]+/)+[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*"
)
_PATH_TAIL = re.compile(r"(?::\d+)?[.,;:)]*(?:\s|$)")


class Token(NamedTuple):
    raw: str
    kind: Literal["path", "ident"]
    needle: str


def checkable(fact_type: str, text: str) -> bool:
    """True for current-state fact types whose text does not describe a past change."""
    return fact_type in CHECKED_TYPES and not _HISTORY.search(text)


def _has_ext(name: str) -> bool:
    _, dot, ext = name.rpartition(".")
    return bool(dot) and ext in _EXTS


def _path_needle(raw: str) -> str | None:
    """The git lookup for a path-shaped ``raw``, or None when it is not a path."""
    needle = _LINE_SUFFIX.sub("", raw).removeprefix("./").rstrip("/")
    if "/" not in raw and not _has_ext(needle):
        return None
    *dirs, last = needle.split("/")
    if not all(_DIR_SEGMENT.fullmatch(d) for d in dirs):
        return None
    if not _FILE_SEGMENT.fullmatch(last) or last.strip(".") == "":
        return None
    return needle


def _hostname_like(segments: list[str]) -> bool:
    if any("_" in s or any(c.isupper() for c in s) for s in segments):
        return False
    return bool(_HOST_TLD.fullmatch(segments[-1]))


def _ident_needle(raw: str) -> str | None:
    """The git lookup for an identifier-shaped ``raw``, or None."""
    if raw.endswith("()"):
        base = raw[:-2]
        segments = base.split(".")
        if base and all(_IDENT.fullmatch(s) for s in segments):
            return base
        return None
    if _SNAKE.fullmatch(raw):
        return raw
    if _CAMEL.fullmatch(raw) and _LOWER_UPPER.search(raw):
        return raw
    segments = raw.split(".")
    if (
        len(segments) >= 2
        and all(_IDENT.fullmatch(s) for s in segments)
        and not _hostname_like(segments)
    ):
        return raw
    return None


def _classify_span(raw: str) -> Token | None:
    if not MIN_LEN <= len(raw) <= MAX_LEN:
        return None
    if any(c.isspace() for c in raw) or any(f in raw for f in _FORBIDDEN):
        return None
    if raw.startswith(("/", "~", "..", "-")):
        return None
    needle = _path_needle(raw)
    if needle:
        return Token(raw, "path", needle)
    needle = _ident_needle(raw)
    if needle:
        return Token(raw, "ident", needle)
    return None


def _unquoted_paths(text: str) -> list[tuple[int, Token]]:
    found = []
    for m in _UNQUOTED_PATH.finditer(text):
        raw = m.group(0)
        if not _PATH_TAIL.match(text, m.end()):
            continue
        if not MIN_LEN <= len(raw) <= MAX_LEN or not _has_ext(raw.rsplit("/", 1)[-1]):
            continue
        needle = _path_needle(raw)
        if needle:
            found.append((m.start(), Token(raw, "path", needle)))
    return found


def extract_tokens(text: str, host_terms: Sequence[str] = ()) -> list[Token]:
    """Paths and identifiers named in ``text``, in order of first appearance, unique by
    ``raw``, at most ``MAX_TOKENS``."""
    found: list[tuple[int, Token]] = []
    masked = list(text)
    for m in _BACKTICK.finditer(text):
        masked[m.start() : m.end()] = " " * (m.end() - m.start())
        tok = _classify_span(m.group(1))
        if tok:
            found.append((m.start(1), tok))
    found.extend(_unquoted_paths("".join(masked)))
    found.sort(key=lambda pair: pair[0])

    out: list[Token] = []
    seen: set[str] = set()
    for _, tok in found:
        if tok.raw in seen:
            continue
        seen.add(tok.raw)
        if sanitize(tok.raw, host_terms).removed > 0:
            continue
        out.append(tok)
        if len(out) == MAX_TOKENS:
            break
    return out
