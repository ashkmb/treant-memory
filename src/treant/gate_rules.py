"""Deterministic gate rules: workspace specifics must not reach ``general``.

Findings carry a category and a span only, never the matched text, so a reason can be logged
or stored without leaking the name it caught. Names, domains, prefixes and terms are matched on
a canonical form (NFKC, casefold, zero-width dropped, separators unified) through set lookups,
so separator and Unicode tricks cannot hide them and runtime does not grow per entry.
"""

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from psycopg import sql

from treant import settings
from treant.db import Database
from treant.names import ws_schema
from treant.scrub import IPV4_PATTERN, IPV6_PATTERN

MIN_NAME_LEN = 3

_TICKET = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")
_IPV4 = re.compile(IPV4_PATTERN)
_IPV6 = re.compile(IPV6_PATTERN, re.IGNORECASE)
_ACCOUNT = re.compile(r"(?<!\d)\d{12}(?!\d)")
_PRIVATE_HOST = re.compile(
    r"(?<![\w.-])(?:[\w-]+\.)+(?:internal|local|lan|home\.arpa)(?![\w-])", re.IGNORECASE
)

# Canonical form: NFKC, casefold, zero-width characters dropped, every run of separators
# a single "-". Tokens are maximal alphanumeric runs; spans always point into the original.
_ZW = "\u200b\u200c\u200d\u2060\ufeff\u00ad"
_ZW_DEL = {ord(c): None for c in _ZW}
_TOKEN = re.compile(r"(?:[^\W_]|[\u0300-\u036f\u2460-\u24ff" + _ZW + r"])+")
_GENERAL_GAP = re.compile(r"[-_./:\s\u2010-\u2015\u2212]+")
_DOT_GAP = re.compile(r"[-.\u2010-\u2015\u2212]+")
_HYPHEN_GAP = re.compile(r"[-\u2010-\u2015\u2212]+")
_DIGITS = "0123456789"
# Gap classes between two tokens: 0 breaks a chain, 1 separators, 2 dots/hyphens, 3 hyphens only.
_BREAK, _SEP, _DOTTED, _HYPHEN = 0, 1, 2, 3


@dataclass(frozen=True)
class Finding:
    category: str
    start: int
    end: int


@dataclass(frozen=True)
class DenyList:
    names: frozenset[str] = frozenset()
    domains: tuple[str, ...] = ()
    ticket_prefixes: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()


def _norm_chunk(chunk: str) -> list[str]:
    if chunk.isascii():
        return [chunk.lower()]
    folded = unicodedata.normalize("NFKC", chunk).casefold().translate(_ZW_DEL)
    return _TOKEN.findall(folded)


def _norm_tokens(value: str) -> tuple[str, ...]:
    return tuple(t for chunk in _TOKEN.findall(value) for t in _norm_chunk(chunk))


@lru_cache(maxsize=4096)
def _gap_class(gap: str) -> int:
    g = unicodedata.normalize("NFKC", gap.translate(_ZW_DEL))
    if not g or _HYPHEN_GAP.fullmatch(g):
        return _HYPHEN if g else _SEP
    if _DOT_GAP.fullmatch(g):
        return _DOTTED
    return _SEP if _GENERAL_GAP.fullmatch(g) else _BREAK


class _Text:
    """The text as normalised tokens with original spans and chain starts."""

    def __init__(self, text: str) -> None:
        self.norm: list[str] = []
        self.starts: list[int] = []
        self.ends: list[int] = []
        self.gaps: list[int] = []
        prev_end: int | None = None
        for m in _TOKEN.finditer(text):
            parts = _norm_chunk(m.group())
            if not parts:
                continue
            gap = _BREAK if prev_end is None else _gap_class(text[prev_end : m.start()])
            for i, part in enumerate(parts):
                self.norm.append(part)
                self.starts.append(m.start())
                self.ends.append(m.end())
                self.gaps.append(gap if i == 0 else _SEP)
            prev_end = m.end()
        n = len(self.norm)
        self.lo_sep = list(range(n))
        self.lo_dot = list(range(n))
        for k in range(1, n):
            if self.gaps[k] >= _SEP:
                self.lo_sep[k] = self.lo_sep[k - 1]
            if self.gaps[k] >= _DOTTED:
                self.lo_dot[k] = self.lo_dot[k - 1]


class _Index:
    """Normalised deny list: tuple sets for names, domains and prefixes, a trigram table
    for single-token terms. No per-entry regexes."""

    def __init__(self, deny: DenyList) -> None:
        self.names, self.names_max, self.names_last = self._tuples(deny.names, MIN_NAME_LEN)
        self.domains, self.domains_max, self.domains_last = self._tuples(deny.domains, 1)
        self.prefixes, self.prefixes_max, self.prefixes_last = self._tuples(deny.ticket_prefixes, 1)
        multi: set[tuple[str, ...]] = set()
        self.trigrams: dict[str, list[str]] = {}
        for term in deny.terms:
            toks = _norm_tokens(term)
            if sum(map(len, toks)) < MIN_NAME_LEN:
                continue
            if len(toks) > 1:
                multi.add(toks)
            elif len(toks[0]) >= 3:
                self.trigrams.setdefault(toks[0][:3], []).append(toks[0])
        self.terms_multi = frozenset(multi)
        self.terms_multi_max = max(map(len, multi), default=0)
        self.terms_multi_last = frozenset(t[-1] for t in multi)

    @staticmethod
    def _tuples(values, min_len: int):
        out = {t for v in values if (t := _norm_tokens(v)) and sum(map(len, t)) >= min_len}
        return frozenset(out), max(map(len, out), default=0), frozenset(t[-1] for t in out)

    def term_in_token(self, token: str) -> bool:
        for i in range(len(token) - 2):
            for term in self.trigrams.get(token[i : i + 3], ()):
                if token.startswith(term, i):
                    return True
        return False


@lru_cache(maxsize=8)
def _index(deny: DenyList) -> _Index:
    return _Index(deny)


def _ngrams(t: _Text, lo: list[int], tuples, max_n: int, last) -> list[tuple[int, int]]:
    hits: list[tuple[int, int]] = []
    if not tuples:
        return hits
    norm = t.norm
    for j, tok in enumerate(norm):
        if tok not in last:
            continue
        for n in range(1, min(max_n, j - lo[j] + 1) + 1):
            if tuple(norm[j - n + 1 : j + 1]) in tuples:
                hits.append((j - n + 1, j))
    return hits


def _prefix_hits(t: _Text, idx: _Index) -> list[tuple[int, int]]:
    hits: list[tuple[int, int]] = []
    if not idx.prefixes:
        return hits
    norm, lo = t.norm, t.lo_sep
    for k, tok in enumerate(norm):
        base = tok.rstrip(_DIGITS)
        if base == tok:
            continue
        if base:  # fused: proj77
            for n in range(1, min(idx.prefixes_max, k - lo[k] + 1) + 1):
                if (*norm[k - n + 1 : k], base) in idx.prefixes:
                    hits.append((k - n + 1, k))
        elif k > 0 and t.gaps[k] == _HYPHEN:  # proj-77
            for n in range(1, min(idx.prefixes_max, k - lo[k - 1]) + 1):
                if tuple(norm[k - n : k]) in idx.prefixes:
                    hits.append((k - n, k))
    return hits


def check(text: str, deny: DenyList) -> list[Finding]:
    found: set[Finding] = set()
    for category, rx in (
        ("ticket_id", _TICKET),
        ("ip_address", _IPV4),
        ("ip_address", _IPV6),
        ("account_id", _ACCOUNT),
        ("private_host", _PRIVATE_HOST),
    ):
        found.update(Finding(category, m.start(), m.end()) for m in rx.finditer(text))

    idx = _index(deny)
    if idx.names or idx.domains or idx.prefixes or idx.trigrams or idx.terms_multi:
        t = _Text(text)

        def add(category: str, i: int, j: int, lo: int | None = None) -> None:
            found.add(Finding(category, t.starts[i if lo is None else lo], t.ends[j]))

        for i, j in _ngrams(t, t.lo_sep, idx.names, idx.names_max, idx.names_last):
            add("instance_name", i, j)
        for i, j in _ngrams(t, t.lo_dot, idx.domains, idx.domains_max, idx.domains_last):
            add("domain", i, j, t.lo_dot[j])
        for i, j in _prefix_hits(t, idx):
            add("ticket_id", i, j)
        for i, j in _ngrams(
            t, t.lo_sep, idx.terms_multi, idx.terms_multi_max, idx.terms_multi_last
        ):
            add("term", i, j)
        if idx.trigrams:
            verdict: dict[str, bool] = {}
            for k, tok in enumerate(t.norm):
                hit = verdict.get(tok)
                if hit is None:
                    hit = verdict[tok] = idx.term_in_token(tok)
                if hit:
                    add("term", k, k)
    return sorted(found, key=lambda f: (f.start, f.end, f.category))


def load_deny(db: Database) -> DenyList:
    """Union over every workspace, active and archived. Fail-closed: a settings row that does
    not parse, or a schema that cannot be read, raises."""
    with db.control_tx() as conn:
        rows = conn.execute("SELECT name, settings FROM control.workspaces").fetchall()
    parsed = {name: settings.parse(raw or {}) for name, raw in rows}
    names: set[str] = set()
    with db.gate_tx() as conn:
        for ws in parsed:
            query = sql.SQL("SELECT name, aliases FROM {}.instance_names").format(
                sql.Identifier(ws_schema(ws))
            )
            for name, aliases in conn.execute(query).fetchall():
                names.update([name, *(aliases or [])])
    names = {n.strip() for n in names if n and len(n.strip()) >= MIN_NAME_LEN}

    def merged(attr: str) -> tuple[str, ...]:
        return tuple(sorted({v for s in parsed.values() for v in getattr(s, attr)}))

    return DenyList(
        names=frozenset(names),
        domains=merged("domains"),
        ticket_prefixes=merged("ticket_prefixes"),
        terms=merged("terms"),
    )
