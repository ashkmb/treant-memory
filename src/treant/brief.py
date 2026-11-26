"""Session brief: warnings, open threads and an optional repo section, capped at 6000 chars
(each of the first two at a third of that). With ``repo_only`` the brief is the repo section
alone, with the whole cap and without warning or open-thread facts (a batch agent uses this
for a task's repository; those types are workspace-wide concerns it must not pass on).

The repo node is looked up in the workspace and in ``general``, so a shared concept with the
same name or alias is covered too. Facts on canary nodes are left out of the Warnings and Open
threads sections; the repo section still shows them when the canary is asked for by name.
"""

import psycopg
from psycopg import sql

from treant import names, stale
from treant.db import Database
from treant.tokens import TokenContext
from treant.walk import NodeRef, walk

CAP = 6000
# Warnings and Open threads get at most a third each, so neither crowds out the rest.
SECTION_CAP = CAP // 3
HEADING_CHARS = 200
MARKER = "(more: use search)"
_FACTS_PER_NODE = 20
CANARY_NODE_TYPE = "canary"  # node type canary.plant uses


def _facts(conn: psycopg.Connection, workspace: str, ftype: str, newest_first: bool) -> list[str]:
    order = "DESC" if newest_first else "ASC"
    query = sql.SQL(
        "SELECT f.id::text, f.text FROM {s}.facts f LEFT JOIN {s}.nodes n ON n.id = f.node_id "
        "WHERE f.status = 'current' AND f.type = %s AND n.type IS DISTINCT FROM %s "
        "ORDER BY f.created_at {o}, f.id"
    ).format(s=sql.Identifier(names.ws_schema(workspace)), o=sql.SQL(order))
    rows = conn.execute(query, (ftype, CANARY_NODE_TYPE)).fetchall()
    notes = stale.open_markers(conn, workspace, [r[0] for r in rows])
    return [stale.annotate(text, notes.get(fid)) for fid, text in rows]


def _cut(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[: cap - len(MARKER) - 1].rstrip() + "\n" + MARKER


def _one_line(text: str) -> str:
    """A caller- or data-supplied name as a heading: one line, bounded."""
    return " ".join(text.split())[:HEADING_CHARS]


def _section(title: str, lines: list[str]) -> str:
    if not lines:
        return ""
    return _cut(f"## {title}\n" + "\n".join(f"- {line}" for line in lines), SECTION_CAP)


def _repo_nodes(conn: psycopg.Connection, ws: str, repo: str) -> list[NodeRef]:
    found: list[NodeRef] = []
    for space, schema in (("ws", ws), ("general", "general")):
        rows = conn.execute(
            sql.SQL(
                "SELECT n.id::text FROM {s}.nodes n WHERE n.status = 'current' AND ("
                "lower(n.name) = lower(%(r)s) OR EXISTS ("
                "SELECT 1 FROM unnest(n.aliases) a WHERE lower(a) = lower(%(r)s))) ORDER BY n.id"
            ).format(s=sql.Identifier(schema)),
            {"r": repo},
        ).fetchall()
        found += [(space, r[0]) for r in rows]
    return found


_ALL_TYPES: tuple[str, ...] = ()
_REPO_ONLY_SKIP = ("warning", "open_thread")


def _repo_section(
    conn: psycopg.Connection, workspace: str, repo: str, skip: tuple[str, ...] = _ALL_TYPES
) -> str:
    ws = names.ws_schema(workspace)
    start = _repo_nodes(conn, ws, repo)
    if not start:
        return ""
    lines: list[str] = []
    for space, node_id in walk(conn, workspace, start, hops=1):
        schema = sql.Identifier(ws if space == "ws" else "general")
        node = conn.execute(
            sql.SQL("SELECT name, type FROM {s}.nodes WHERE id = %s").format(s=schema), (node_id,)
        ).fetchone()
        if node is None:
            continue
        lines.append(f"### {_one_line(node[0])} ({_one_line(node[1])})")
        facts = conn.execute(
            sql.SQL(
                "SELECT id::text, type, text FROM {s}.facts WHERE node_id = %s AND status = 'current' "
                "AND NOT (type = ANY(%s)) ORDER BY created_at DESC, id LIMIT %s"
            ).format(s=schema),
            (node_id, list(skip), _FACTS_PER_NODE),
        ).fetchall()
        notes = (
            stale.open_markers(conn, workspace, [fid for fid, _, _ in facts])
            if space == "ws"
            else {}
        )
        lines += [f"- [{t}] {stale.annotate(text, notes.get(fid))}" for fid, t, text in facts]
    return f"## {_one_line(repo)}\n" + "\n".join(lines)


def brief(db: Database, ctx: TokenContext, repo: str | None, repo_only: bool = False) -> str:
    if repo_only:
        if not repo:
            return ""
        with db.ws_tx(ctx.workspace) as conn:
            return _cut(_repo_section(conn, ctx.workspace, repo, _REPO_ONLY_SKIP), CAP)
    with db.ws_tx(ctx.workspace) as conn:
        parts = [
            _section("Warnings", _facts(conn, ctx.workspace, "warning", False)),
            _section("Open threads", _facts(conn, ctx.workspace, "open_thread", True)),
        ]
        if repo:
            parts.append(_repo_section(conn, ctx.workspace, repo))
    return _cut("\n\n".join(p for p in parts if p), CAP)
