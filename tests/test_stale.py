import uuid

import psycopg
import pytest


def _columns(conn, table):
    return {
        r[0]: (r[1], r[2])
        for r in conn.execute(
            "select column_name, data_type, is_nullable from information_schema.columns "
            "where table_schema = 'ws_alpha' and table_name = %s",
            (table,),
        )
    }


def _fact(conn):
    return conn.execute(
        "insert into ws_alpha.facts (type, text) values ('fact', 'uses `old_name.py`') returning id"
    ).fetchone()[0]


def _flag(conn, fact_id, commit="abc1234"):
    return conn.execute(
        "insert into ws_alpha.stale_flags "
        "(fact_id, repo, commit, missing, fact_version, text_sha256) "
        "values (%s, 'acme/app', %s, '[]'::jsonb, 1, 'h') returning id",
        (fact_id, commit),
    ).fetchone()[0]


def test_stale_flags_table_shape(fresh_db):
    with fresh_db.admin_tx() as conn:
        cols = _columns(conn, "stale_flags")
    assert cols == {
        "id": ("bigint", "NO"),
        "fact_id": ("uuid", "NO"),
        "repo": ("text", "NO"),
        "commit": ("text", "NO"),
        "missing": ("jsonb", "NO"),
        "fact_version": ("integer", "NO"),
        "text_sha256": ("text", "NO"),
        "created_at": ("timestamp with time zone", "NO"),
        "resolved_at": ("timestamp with time zone", "YES"),
        "resolution": ("text", "YES"),
    }

    with fresh_db.ws_tx("alpha") as conn:  # the workspace role can insert
        fact = _fact(conn)
        assert _flag(conn, fact) > 0

    # unique (fact_id, commit)
    with pytest.raises(psycopg.errors.UniqueViolation), fresh_db.ws_tx("alpha") as conn:
        fact = _fact(conn)
        _flag(conn, fact, "abc1234")
        conn.execute(
            "update ws_alpha.stale_flags set resolved_at = now(), resolution = 'kept' "
            "where fact_id = %s",
            (fact,),
        )
        _flag(conn, fact, "abc1234")

    # at most one open flag per fact; a resolved one frees the slot
    with fresh_db.ws_tx("alpha") as conn:
        fact = _fact(conn)
        _flag(conn, fact, "abc1234")
    with pytest.raises(psycopg.errors.UniqueViolation), fresh_db.ws_tx("alpha") as conn:
        _flag(conn, fact, "def5678")
    with fresh_db.ws_tx("alpha") as conn:
        conn.execute(
            "update ws_alpha.stale_flags set resolved_at = now(), resolution = 'retracted' "
            "where fact_id = %s",
            (fact,),
        )
        _flag(conn, fact, "def5678")

    # resolution check
    with pytest.raises(psycopg.errors.CheckViolation), fresh_db.ws_tx("alpha") as conn:
        fact = _fact(conn)
        flag = _flag(conn, fact)
        conn.execute("update ws_alpha.stale_flags set resolution = 'bogus' where id = %s", (flag,))
    for ok in ("kept", "retracted", "superseded-by-edit"):
        with fresh_db.ws_tx("alpha") as conn:
            fact = _fact(conn)
            flag = _flag(conn, fact)
            conn.execute(
                "update ws_alpha.stale_flags set resolved_at = now(), resolution = %s "
                "where id = %s",
                (ok, flag),
            )

    # fact_id must reference a fact
    with pytest.raises(psycopg.errors.ForeignKeyViolation), fresh_db.ws_tx("alpha") as conn:
        _flag(conn, uuid.uuid4())
