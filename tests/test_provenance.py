"""Provenance lookups against Postgres, under the workspace role as search runs them."""

import uuid
from datetime import UTC, datetime

import pytest
from psycopg.types.json import Jsonb

from treant import graph, migrate, names
from treant.provenance import lookup

DAY1 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
DAY2 = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
DAY3 = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


@pytest.fixture
def ws(db, admin_dsn, prefix):
    """A fresh, uniquely named workspace no other test writes to."""
    name = "sr" + uuid.uuid4().hex[:8]
    migrate.create_workspace(admin_dsn, prefix, name)
    return name


def _revision(db, ws, client_session, when, status="stored"):
    """A revision of ``client_session`` (created if new) dated ``when``."""
    s = names.ws_schema(ws)
    with db.admin_tx() as c:
        sid = c.execute(
            f"INSERT INTO {s}.sessions (token_id, client_session_id) VALUES ('t', %s) "
            "ON CONFLICT (token_id, client_session_id) DO UPDATE SET token_id = EXCLUDED.token_id "
            "RETURNING id::text",
            (client_session,),
        ).fetchone()[0]
        return c.execute(
            f"INSERT INTO {s}.revisions (session_id, n, sections, status, created_at) "
            f"VALUES (%s, (SELECT coalesce(max(n), 0) + 1 FROM {s}.revisions "
            "WHERE session_id = %s), %s, %s, %s) RETURNING id::text",
            (sid, sid, Jsonb({}), status, when),
        ).fetchone()[0]


def _journal(db, ws, revision):
    with db.admin_tx() as c:
        return c.execute(
            f"INSERT INTO {names.ws_schema(ws)}.journal_entries (revision_id, text) "
            "VALUES (%s, 'journal text') RETURNING id::text",
            (revision,),
        ).fetchone()[0]


def _lookup(db, ws, items):
    with db.ws_tx(ws) as c:
        return lookup(c, names.ws_schema(ws), items)


def _today():
    return datetime.now(UTC).date().isoformat()


def test_fact_saved_in_one_session_confirmed_in_another(db, ws):
    r1 = _revision(db, ws, "sess-one", DAY1)
    r2 = _revision(db, ws, "sess-two", DAY2)
    with db.ws_tx(ws) as c:
        fid = graph.add_fact(c, ws, "fact", "a provenance fact", revision_id=r1)
        graph.add_support(c, ws, "fact", fid, r2)
    key = (names.ws_schema(ws), "fact", fid)
    assert _lookup(db, ws, [key]) == {
        key: {"saved": _today(), "confirmed": "2026-11-21", "session": "sess-one"}
    }


def test_owner_fact_has_saved_only(db, ws):
    with db.ws_tx(ws) as c:
        fid = graph.add_fact(c, ws, "fact", "an owner fact")
    key = (names.ws_schema(ws), "fact", fid)
    assert _lookup(db, ws, [key]) == {key: {"saved": _today()}}


def test_quarantined_revision_does_not_count(db, ws):
    r1 = _revision(db, ws, "sess-one", DAY1)
    bad = _revision(db, ws, "sess-bad", DAY3, status="quarantined")
    early_bad = _revision(db, ws, "sess-early", datetime(2026, 11, 1, tzinfo=UTC), "quarantined")
    with db.ws_tx(ws) as c:
        fid = graph.add_fact(c, ws, "fact", "a fact with a quarantined support", revision_id=r1)
        graph.add_support(c, ws, "fact", fid, bad)
        graph.add_support(c, ws, "fact", fid, early_bad)
        lone = graph.add_fact(c, ws, "fact", "only quarantined", revision_id=bad)
    s = names.ws_schema(ws)
    got = _lookup(db, ws, [(s, "fact", fid), (s, "fact", lone)])
    assert got[(s, "fact", fid)] == {
        "saved": _today(),
        "confirmed": "2026-11-20",
        "session": "sess-one",
    }
    assert got[(s, "fact", lone)] == {"saved": _today()}


def test_journal_and_node_sources(db, ws):
    rj = _revision(db, ws, "sess-journal", DAY2)
    jid = _journal(db, ws, rj)
    late = _revision(db, ws, "sess-late", DAY3)
    early = _revision(db, ws, "sess-early", DAY1)
    with db.ws_tx(ws) as c:
        nid = graph.add_node(c, ws, "instance", "service", "provnode")
        graph.add_support(c, ws, "node", nid, late)
        graph.add_support(c, ws, "node", nid, early)
    s = names.ws_schema(ws)
    got = _lookup(db, ws, [(s, "journal", jid), (s, "node", nid)])
    assert got[(s, "journal", jid)] == {
        "saved": _today(),
        "confirmed": "2026-11-21",
        "session": "sess-journal",
    }
    assert got[(s, "node", nid)] == {
        "saved": _today(),
        "confirmed": "2026-11-22",
        "session": "sess-early",
    }


def test_general_item_saved_only(db, ws):
    with db.publisher_tx() as c:
        gid = graph.add_fact(c, "general", "fact", "a general provenance fact")
    key = ("general", "fact", gid)
    assert _lookup(db, ws, [key]) == {key: {"saved": _today()}}


def test_missing_and_foreign_items_absent(db, ws):
    with db.ws_tx(ws) as c:
        fid = graph.add_fact(c, ws, "fact", "present")
    s = names.ws_schema(ws)
    got = _lookup(
        db,
        ws,
        [(s, "fact", fid), (s, "fact", str(uuid.uuid4())), ("ws_beta", "fact", fid)],
    )
    assert list(got) == [(s, "fact", fid)]


class _Counting:
    def __init__(self, conn):
        self.conn, self.calls = conn, 0

    def execute(self, *args, **kwargs):
        self.calls += 1
        return self.conn.execute(*args, **kwargs)


def test_many_supports_one_query_per_schema(db, ws):
    first = _revision(db, ws, "sess-one", DAY1)
    revs = [_revision(db, ws, f"sess-{i}", DAY2) for i in range(49)]
    with db.ws_tx(ws) as c:
        fid = graph.add_fact(c, ws, "fact", "a much confirmed fact")
        nid = graph.add_node(c, ws, "instance", "service", "provmany")
        for r in [first, *revs]:
            graph.add_support(c, ws, "fact", fid, r)
    with db.publisher_tx() as c:
        gid = graph.add_fact(c, "general", "fact", "a general fact")
    s = names.ws_schema(ws)
    items = [(s, "fact", fid), (s, "node", nid), ("general", "fact", gid)]
    with db.ws_tx(ws) as c:
        counting = _Counting(c)
        got = lookup(counting, s, items)
    assert counting.calls <= 2
    assert got[(s, "fact", fid)] == {
        "saved": _today(),
        "confirmed": "2026-11-21",
        "session": "sess-one",
    }
    assert got[(s, "node", nid)] == {"saved": _today()}
    assert got[("general", "fact", gid)] == {"saved": _today()}
