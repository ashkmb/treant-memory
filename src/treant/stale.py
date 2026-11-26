"""Stale-fact flags: validation of what a client reports about a fact, and the flag store.

The client sends only tokens that already appear verbatim in the fact text, a commit id,
and removing commit subjects. Nothing here changes a fact: a flag is a row in the workspace's
``stale_flags`` for the owner to keep or retract. The DB helpers take a connection inside the
caller's ``ws_tx`` and read the workspace schema only, never ``general``.
"""

import hashlib
import re
import uuid
from collections.abc import Sequence
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from treant import names
from treant.errors import InvalidInput, NotFound
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


def _schema(workspace: str) -> sql.Identifier:
    try:
        return sql.Identifier(names.ws_schema(workspace))
    except ValueError:
        raise InvalidInput("invalid namespace") from None


def repo_node_ids(conn: psycopg.Connection, workspace: str, repo: str) -> list[str]:
    """Current nodes named ``repo`` (name or alias, case-insensitive) and the current nodes
    that are part of them through current ``part_of`` edges, at most PART_OF_DEPTH deep."""
    schema = _schema(workspace)
    query = sql.SQL(
        "WITH RECURSIVE r(id, depth) AS ("
        " SELECT n.id, 0 FROM {s}.nodes n WHERE n.status = 'current' AND (lower(n.name) = lower(%s)"
        "  OR EXISTS (SELECT 1 FROM unnest(n.aliases) AS a(alias) WHERE lower(a.alias) = lower(%s)))"
        " UNION"
        " SELECT e.src, r.depth + 1 FROM r"
        " JOIN {s}.edges e ON e.dst = r.id AND e.type = 'part_of' AND e.status = 'current'"
        " JOIN {s}.nodes n ON n.id = e.src AND n.status = 'current'"
        " WHERE r.depth < %s"
        ") SELECT DISTINCT id FROM r ORDER BY id"
    ).format(s=schema)
    rows = conn.execute(query, (repo, repo, PART_OF_DEPTH)).fetchall()
    return [str(r[0]) for r in rows]


def repo_facts(conn: psycopg.Connection, workspace: str, repo: str) -> tuple[list[dict], bool]:
    """Current facts on the repo's nodes, newest first, at most MAX_REPO_FACTS; and whether
    more were left out."""
    ids = repo_node_ids(conn, workspace, repo)
    if not ids:
        return [], False
    query = sql.SQL(
        "SELECT f.id, f.type, f.text, n.name FROM {s}.facts f JOIN {s}.nodes n ON n.id = f.node_id "
        "WHERE f.status = 'current' AND f.node_id = ANY(%s::uuid[]) "
        "ORDER BY f.created_at DESC, f.id DESC LIMIT %s"
    ).format(s=_schema(workspace))
    rows = conn.execute(query, (ids, MAX_REPO_FACTS + 1)).fetchall()
    facts = [
        {"id": str(fid), "type": typ, "text": text, "node": node}
        for fid, typ, text, node in rows[:MAX_REPO_FACTS]
    ]
    return facts, len(rows) > MAX_REPO_FACTS


def load_fact(
    conn: psycopg.Connection, workspace: str, fact_id: str
) -> tuple[str, str, int, str | None]:
    """Lock a workspace fact for the rest of the transaction; return
    (status, text, version, node_id). A bad id or an absent fact is NotFound."""
    try:
        fid = str(uuid.UUID(str(fact_id)))
    except ValueError:
        raise NotFound("fact not found") from None
    row = conn.execute(
        sql.SQL(
            "SELECT status, text, version, node_id FROM {}.facts WHERE id = %s FOR UPDATE"
        ).format(_schema(workspace)),
        (fid,),
    ).fetchone()
    if row is None:
        raise NotFound("fact not found")
    status, text, version, node_id = row
    return status, text, version, None if node_id is None else str(node_id)


def resolve_edited(conn: psycopg.Connection, workspace: str) -> int:
    """Resolve open flags whose fact changed since it was flagged; return how many."""
    cur = conn.execute(
        sql.SQL(
            "UPDATE {s}.stale_flags sf SET resolved_at = now(), resolution = 'superseded-by-edit' "
            "FROM {s}.facts f WHERE f.id = sf.fact_id AND sf.resolved_at IS NULL "
            "AND f.version > sf.fact_version"
        ).format(s=_schema(workspace))
    )
    return cur.rowcount


def flag(
    conn: psycopg.Connection,
    workspace: str,
    fact_id: str,
    repo: str,
    commit: str,
    missing: Any,
    terms: Sequence[str],
) -> dict:
    """Record that a current fact of ``repo`` names tokens missing at ``commit``.

    Never changes the fact. Returns ``{"flagged": True, "flag_id": id}``, or
    ``{"flagged": False, "reason": "exists" | "kept"}`` when an open flag (or one for the same
    commit) is there already, or the owner kept a flag on this very text.
    """
    repo = valid_repo(repo)
    commit = valid_commit(commit)
    schema = _schema(workspace)
    resolve_edited(conn, workspace)
    status, text, version, node_id = load_fact(conn, workspace, fact_id)
    if status != "current":
        raise InvalidInput("fact not current")
    if node_id is None or node_id not in repo_node_ids(conn, workspace, repo):
        raise InvalidInput("fact not in repo")
    cleaned = clean_missing(missing, text, terms)
    sha = text_sha256(text)
    fid = str(uuid.UUID(str(fact_id)))
    kept = conn.execute(
        sql.SQL(
            "SELECT 1 FROM {}.stale_flags WHERE fact_id = %s AND resolution = 'kept' "
            "AND text_sha256 = %s"
        ).format(schema),
        (fid, sha),
    ).fetchone()
    if kept is not None:
        return {"flagged": False, "reason": "kept"}
    row = conn.execute(
        sql.SQL(
            "INSERT INTO {}.stale_flags (fact_id, repo, commit, missing, fact_version, text_sha256) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id"
        ).format(schema),
        (fid, repo, commit, Jsonb(cleaned), version, sha),
    ).fetchone()
    if row is None:
        return {"flagged": False, "reason": "exists"}
    return {"flagged": True, "flag_id": row[0]}
