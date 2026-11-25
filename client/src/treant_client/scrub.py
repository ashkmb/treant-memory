"""Deterministic, regex-only scrubber for chat summaries.

Vendored 2026-11-01 from an earlier chat sanitizer. The only change:
the hostname rule is generated from the caller's ``host_terms``.

Runs on session text before it leaves the Mac for the Treant server. Every removed
span becomes ``[removed]``.
"""

import re
from collections.abc import Callable, Sequence
from typing import NamedTuple

REMOVED = "[removed]"
MAX_CONFIDENT_REMOVALS = 12

_NOT_REMOVED = r"(?!\[removed\])"
_HEX = r"[0-9A-Fa-f]{1,4}"


class Sanitized(NamedTuple):
    text: str
    removed: int
    confident: bool


def _blob_ok(m: re.Match[str]) -> bool:
    s = m.group(0)
    if not (any(c.isdigit() for c in s) and any(c.isalpha() for c in s)):
        return False
    # a "/" run is a path unless it also looks like base64 (+ or = padding)
    return "/" not in s or "+" in s or "=" in s


_WORDISH = re.compile(r"[A-Za-z][a-z]{3,}")


def _token_ok(m: re.Match[str]) -> bool:
    """A base64url run is a token when it mixes upper, lower and digits and none of its
    ``_``/``-`` separated parts reads as a word (lowercase or Titlecase, 4+ letters).
    That spares snake_case and CamelCase identifiers, slugs and titles."""
    s = m.group(0)
    if not (
        any(c.isupper() for c in s) and any(c.islower() for c in s) and any(c.isdigit() for c in s)
    ):
        return False
    return not any(_WORDISH.fullmatch(part) for part in re.split(r"[_-]", s))


def _kv_value_ok(m: re.Match[str]) -> bool:
    """A "/" in the value is a path or a URL unless it also looks like base64 (+ or =)."""
    value = m.group(0)[len(m.group(1)) :]
    return "/" not in value or ("://" not in value and ("+" in value or "=" in value))


# (compiled regex, replacement template or None for whole match, predicate)
# Applied in order, most specific first. A replacement template may use \1 to
# keep a prefix group.
_Rule = tuple[re.Pattern[str], str | None, Callable[[re.Match[str]], bool] | None]

_RULES_HEAD: list[_Rule] = [
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        REMOVED,
        None,
    ),
    # truncated block: header without END, up to a closing fence or end of text
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?=\n?```|\Z)", re.DOTALL),
        REMOVED,
        None,
    ),
    # credentials in URLs: scheme://user:pass@host
    (re.compile(r"(?<=://)[^\s/@:]*:[^\s/@]+(?=@)"), REMOVED, None),
    # secret-looking query values
    (
        re.compile(
            r"(?i)([?&](?:access_token|token|key|secret|password|sig|signature)=)"
            + _NOT_REMOVED
            + r"[^&\s#\"'`)>\]]+"
        ),
        r"\1" + REMOVED,
        None,
    ),
    (
        re.compile(
            r"\b(?:sk-ant-|sk-|ghp_|gho_|github_pat_|glpat-|xox[abpr]-|ATATT)"
            r"[A-Za-z0-9_\-]{10,}"
        ),
        REMOVED,
        None,
    ),
    # Hugging Face, Stripe, Grafana service account, npm and PyPI tokens
    (
        re.compile(
            r"\b(?:hf_[A-Za-z0-9]{30,}"
            r"|[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"
            r"|glsa_[A-Za-z0-9]{32}_[0-9A-Fa-f]{8}"
            r"|npm_[A-Za-z0-9]{36,}"
            r"|pypi-[A-Za-z0-9_\-]{50,})"
        ),
        REMOVED,
        None,
    ),
    # Telegram bot token (also inside .../bot<token>/...) and Google API key
    (re.compile(r"(?<!\d)\d{8,10}:[A-Za-z0-9_\-]{35}(?![\w-])"), REMOVED, None),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}(?![\w-])"), REMOVED, None),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), REMOVED, None),
    (re.compile(r"\beyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]{5,}"), REMOVED, None),
    (
        re.compile(
            r"(?i)\b(Bearer\s+)(?=[A-Za-z0-9._~+/=\-]*\d|[A-Za-z0-9._~+/=\-]{20})"
            r"[A-Za-z0-9._~+/=\-]{8,}"
        ),
        r"\1" + REMOVED,
        None,
    ),
    (
        re.compile(
            r"(?i)(?<![\w-])([\w-]*"
            r"(?:password|passwd|secret|token|api[_-]?key|private[_-]?key|access[_-]?key)"
            r"[\w-]*[ \t]*[:=][ \t]*)" + _NOT_REMOVED + r"(?:\"[^\"\n]*\"?|'[^'\n]*'?|[^\s\"'`,;]+)"
        ),
        None,
        None,
    ),
    # "the password is <value>", "token <value>": only a value that looks like a credential
    # (12+ characters up to a space, with a digit; a "/" only with base64 + or =), so prose,
    # paths and URLs stay.
    (
        re.compile(
            r"(?i)(?<![\w-])([\w-]*(?:password|passwd|secret|token|key)[\w-]*"
            r"[ \t]+(?:is[ \t]+)?)" + _NOT_REMOVED + r"(?=[^\s\"'`,;]*\d)"
            r"[^\s\"'`,;]{12,}(?![^\s\"'`,;])"
        ),
        None,
        _kv_value_ok,
    ),
    (
        re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+"),
        REMOVED,
        None,
    ),
    (
        re.compile(r"(?i)\b(?:[a-z0-9\-]+\.)*home\.arpa(?![\w\-])"),
        REMOVED,
        None,
    ),
]


def _host_term_rule(term: str) -> _Rule:
    """Any hostname with ``term`` in a label (employer hosts, SaaS tenants)."""
    return (
        re.compile(
            r"(?i)(?<![\w.\-])(?:[a-z0-9\-]+\.)*[a-z0-9\-]*" + re.escape(term) + r"[a-z0-9\-]*"
            r"(?:\.[a-z0-9\-]+)+(?![\w\-])"
        ),
        REMOVED,
        None,
    )


_RULES_TAIL: list[_Rule] = [
    (
        re.compile(
            r"(?i)(?<![\w.\-])"
            r"(?![\w.\-]*\b(?:config|env|settings|conf)\.(?:internal|local|lan)\b)"
            r"(?:[a-z0-9\-]+\.)+(?:internal|local|lan)(?![\w\-]|\.\w)"
        ),
        REMOVED,
        None,
    ),
    (
        re.compile(
            r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}"
            r"(?:25[0-5]|2[0-4]\d|1?\d?\d)(?!\d|\.\d)"
        ),
        REMOVED,
        None,
    ),
    (
        re.compile(
            r"(?i)(?<![\w:])(?:"
            rf"(?:{_HEX}:){{7}}{_HEX}"
            r"|(?=[0-9a-f:]*::)(?=[0-9a-f:]*[0-9a-f])"
            r"[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,7}"
            r")(?![\w:])"
        ),
        REMOVED,
        None,
    ),
    (re.compile(r"(?<!\d)\d{12}(?!\d)"), REMOVED, None),
    (
        re.compile(r"(?<![^\s\"'`=:])[A-Za-z0-9+/]{32,}={0,2}(?![^\s\"'`=:])"),
        REMOVED,
        _blob_ok,
    ),
    # base64url tokens with "_" or "-" (the rule above takes plain alphanumeric runs)
    (
        re.compile(r"(?<![\w\-./+])[A-Za-z0-9_\-]{32,}(?![\w\-/+])"),
        REMOVED,
        _token_ok,
    ),
]


def _rules(host_terms: Sequence[str]) -> list[_Rule]:
    terms = [t.strip() for t in host_terms if t.strip()]
    return _RULES_HEAD + [_host_term_rule(t) for t in terms] + _RULES_TAIL


def _pass(text: str, rules: list[_Rule]) -> tuple[str, int]:
    total = 0
    for rx, repl, pred in rules:
        count = 0

        def sub(m: re.Match, repl=repl, pred=pred) -> str:
            nonlocal count
            if pred is not None and not pred(m):
                return m.group(0)
            count += 1
            if repl is None:  # key/value rule: keep the key and separator
                return m.group(1) + REMOVED
            return m.expand(repl)

        text = rx.sub(sub, text)
        total += count
    return text, total


def sanitize(text: str, host_terms: Sequence[str] = ()) -> Sanitized:
    rules = _rules(host_terms)
    out, removed = _pass(text, rules)
    _, again = _pass(out, rules)
    confident = removed <= MAX_CONFIDENT_REMOVALS and again == 0
    return Sanitized(out, removed, confident)
