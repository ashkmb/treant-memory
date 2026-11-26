"""Stale-fact flags: pure validation of what a client reports about a fact.

The client sends only tokens that already appear verbatim in the fact text, a commit id,
and removing commit subjects. Nothing here changes a fact.
"""

import hashlib
import re
from collections.abc import Sequence
from typing import Any

from treant.errors import InvalidInput
from treant.scrub import scrub

MAX_MISSING = 10
MAX_REPO_FACTS = 200
MAX_REPO = 200
MAX_SUBJECT = 200
TOKEN_MIN = 3
TOKEN_MAX = 120
PART_OF_DEPTH = 5

_COMMIT = re.compile(r"[0-9a-f]{7,64}")


def valid_commit(value: Any) -> str:
    """A commit id as lowercase hex, 7-64 characters."""
    if isinstance(value, str):
        lowered = value.lower()
        if _COMMIT.fullmatch(lowered):
            return lowered
    raise InvalidInput("commit")


def valid_repo(value: Any) -> str:
    """A non-empty one-line repository name of at most MAX_REPO characters."""
    if isinstance(value, str) and value and len(value) <= MAX_REPO and "\n" not in value:
        return value
    raise InvalidInput("repo")


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _subject(value: Any, terms: Sequence[str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidInput("subject")
    lines = value.splitlines()
    first = lines[0].strip()[:MAX_SUBJECT] if lines else ""
    scrubbed = scrub(first, terms)
    return scrubbed.text if scrubbed.confident else None


def clean_missing(missing: Any, fact_text: str, terms: Sequence[str]) -> list[dict]:
    """Validate the client's missing-token entries; keep at most MAX_MISSING.

    Every token must be a substring of the fact text, so file contents never pass.
    A duplicate token keeps its first entry. An unsure scrub drops the subject only.
    """
    if not isinstance(missing, list) or not missing:
        raise InvalidInput("missing")
    out: list[dict] = []
    seen: set[str] = set()
    for entry in missing:
        if not isinstance(entry, dict):
            raise InvalidInput("missing")
        token = entry.get("token")
        if (
            not isinstance(token, str)
            or not TOKEN_MIN <= len(token) <= TOKEN_MAX
            or token not in fact_text
        ):
            raise InvalidInput("token")
        commit = entry.get("commit")
        commit = None if commit is None else valid_commit(commit)
        subject = _subject(entry.get("subject"), terms)
        if token in seen or len(out) >= MAX_MISSING:
            continue
        seen.add(token)
        out.append({"token": token, "commit": commit, "subject": subject})
    return out


def marker(missing: list[dict], commit: str) -> str:
    """The short in-doubt note shown beside a flagged fact."""
    first = missing[0]["token"]
    more = len(missing) - 1
    what = first if more <= 0 else f"{first} and {more} more"
    return f"(in doubt: {what} not found at {commit[:7]})"
