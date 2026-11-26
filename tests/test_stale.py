import uuid

import psycopg
import pytest
from fakes import FakeEmbedder

from treant import auth, brief, graph, search, stale, tools
from treant.tokens import TokenContext

SHA = "a" * 40
MISSING = [{"token": "old_name.py", "commit": "abc1234", "subject": "rename old_name to new_name"}]


@pytest.fixture
def t(fresh_db):
    return tools.Tools(fresh_db, FakeEmbedder())


@pytest.fixture
def as_ctx():
    tokens = []

    def use(ctx):
        tokens.append(auth.current_token.set(ctx))

    yield use
    for tok in reversed(tokens):
        auth.current_token.reset(tok)


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


def _repo(db):
    """alpha: repo "demo" (alias "demo-repo"), "web" part_of demo, "tmpl" part_of web,
    "old" part_of demo through a retracted edge, unrelated "other"; general "demo" too."""
    with db.publisher_tx() as conn:
        gnode = graph.add_node(conn, "general", "concept", "repo", "demo")
        graph.add_fact(conn, "general", "fact", "on general demo", node_id=gnode)
    n = {}
    f = {}
    with db.ws_tx("alpha") as conn:
        for name, aliases in (("demo", ["demo-repo"]), ("web", []), ("tmpl", []), ("old", [])):
            n[name] = graph.add_node(conn, "alpha", "instance", "repo", name, aliases)
        n["other"] = graph.add_node(conn, "alpha", "instance", "repo", "other")
        graph.add_edge(conn, "alpha", n["web"], n["demo"], "part_of")
        graph.add_edge(conn, "alpha", n["tmpl"], n["web"], "part_of")
        gone = graph.add_edge(conn, "alpha", n["old"], n["demo"], "part_of")
        conn.execute("UPDATE ws_alpha.edges SET status = 'retracted' WHERE id = %s", (gone,))
        f["demo"] = graph.add_fact(
            conn, "alpha", "fact", "on demo: entry point is `old_name.py`", node_id=n["demo"]
        )
        f["web"] = graph.add_fact(conn, "alpha", "fact", "on web: `old_name.py` too", n["web"])
        graph.add_fact(conn, "alpha", "fact", "on tmpl", node_id=n["tmpl"])
        graph.add_fact(conn, "alpha", "fact", "on old", node_id=n["old"])
        f["other"] = graph.add_fact(
            conn, "alpha", "fact", "on other: `old_name.py` here", node_id=n["other"]
        )
        gone_fact = graph.add_fact(conn, "alpha", "fact", "superseded on demo", n["demo"])
        conn.execute("UPDATE ws_alpha.facts SET status = 'superseded' WHERE id = %s", (gone_fact,))
    return n, f


def _flags(db, ws, fact_id=None):
    with db.ws_tx(ws) as conn:
        if fact_id is None:
            return conn.execute(f"SELECT count(*) FROM ws_{ws}.stale_flags").fetchone()[0]
        return conn.execute(
            f"SELECT repo, commit, missing, fact_version, text_sha256, resolved_at, resolution "
            f"FROM ws_{ws}.stale_flags WHERE fact_id = %s ORDER BY id",
            (fact_id,),
        ).fetchall()


def _fact_row(db, fid):
    with db.ws_tx("alpha") as conn:
        row = conn.execute(
            "SELECT status, text, version FROM ws_alpha.facts WHERE id = %s", (fid,)
        ).fetchone()
        versions = conn.execute(
            "SELECT count(*) FROM ws_alpha.versions WHERE item_kind = 'fact' AND item_id = %s",
            (fid,),
        ).fetchone()[0]
    return row, versions


def test_repo_facts_follows_part_of_and_skips_the_rest(fresh_db, t, as_ctx, ctx_a):
    _repo(fresh_db)
    as_ctx(ctx_a)
    res = t.repo_facts("Demo-Repo")
    assert {f["text"] for f in res["facts"]} == {
        "on demo: entry point is `old_name.py`",
        "on web: `old_name.py` too",
        "on tmpl",
    }
    assert set(res["facts"][0]) == {"id", "type", "text", "node"} and res["truncated"] is False
    assert {f["node"] for f in res["facts"]} == {"demo", "web", "tmpl"}
    assert t.repo_facts("DEMO") == res  # name, case-insensitive
    assert {f["text"] for f in t.repo_facts("web")["facts"]} == {
        "on web: `old_name.py` too",
        "on tmpl",
    }
    assert t.repo_facts("no-such-repo") == {"facts": [], "truncated": False}
    assert t.repo_facts("") == {"error": "invalid input"}
    assert t.repo_facts("a\nb") == {"error": "invalid input"}
    as_ctx(TokenContext("b1b1b1b1b1b1", "beta", frozenset({"read"})))
    assert t.repo_facts("demo") == {"facts": [], "truncated": False}  # alpha's repo stays alpha's
    as_ctx(TokenContext("c1c1c1c1c1c1", "alpha", frozenset({"create"})))
    assert t.repo_facts("demo") == {"error": "forbidden"}


def test_repo_node_ids_stops_at_part_of_depth(fresh_db):
    with fresh_db.ws_tx("alpha") as conn:
        chain = [graph.add_node(conn, "alpha", "instance", "repo", "deep")]
        for i in range(stale.PART_OF_DEPTH + 1):
            chain.append(graph.add_node(conn, "alpha", "instance", "dir", f"level{i}"))
            graph.add_edge(conn, "alpha", chain[-1], chain[-2], "part_of")
        ids = set(stale.repo_node_ids(conn, "alpha", "deep"))
    assert ids == set(chain[: stale.PART_OF_DEPTH + 1])


def test_repo_facts_caps_at_200(fresh_db, t, as_ctx, ctx_a):
    with fresh_db.ws_tx("alpha") as conn:
        node = graph.add_node(conn, "alpha", "instance", "repo", "big")
        for i in range(205):
            fid = graph.add_fact(conn, "alpha", "fact", f"big fact {i}", node_id=node)
            conn.execute(
                "UPDATE ws_alpha.facts SET created_at = now() + make_interval(secs => %s) "
                "WHERE id = %s",
                (i, fid),
            )
    as_ctx(ctx_a)
    res = t.repo_facts("big")
    assert res["truncated"] is True and len(res["facts"]) == stale.MAX_REPO_FACTS == 200
    assert [f["text"] for f in res["facts"]] == [f"big fact {i}" for i in range(204, 4, -1)]


def test_flag_stale_inserts_a_flag_and_leaves_the_fact_alone(fresh_db, t, as_ctx, ctx_a):
    _, f = _repo(fresh_db)
    as_ctx(ctx_a)
    before = _fact_row(fresh_db, f["web"])
    res = t.flag_stale(f["web"], "demo", SHA, MISSING)
    assert res["flagged"] is True and isinstance(res["flag_id"], int)
    assert _fact_row(fresh_db, f["web"]) == before
    assert before == (("current", "on web: `old_name.py` too", 1), 0)
    [(repo, commit, missing, version, sha, resolved_at, resolution)] = _flags(
        fresh_db, "alpha", f["web"]
    )
    assert (repo, commit, version, resolved_at, resolution) == ("demo", SHA, 1, None, None)
    assert missing == MISSING
    assert sha == stale.text_sha256("on web: `old_name.py` too")


def test_flag_stale_refusals(fresh_db, t, as_ctx, ctx_a):
    _, f = _repo(fresh_db)
    with fresh_db.ws_tx("alpha") as conn:
        gone = graph.add_fact(conn, "alpha", "fact", "gone: `old_name.py`")
        graph.retract(conn, "alpha", "fact", gone)
    with fresh_db.ws_tx("beta") as conn:
        bnode = graph.add_node(conn, "beta", "instance", "repo", "demo")
        beta_fact = graph.add_fact(conn, "beta", "fact", "beta `old_name.py`", node_id=bnode)
    as_ctx(ctx_a)
    assert t.flag_stale(gone, "demo", SHA, MISSING) == {"error": "invalid input"}
    assert t.flag_stale(f["other"], "demo", SHA, MISSING) == {"error": "invalid input"}
    assert t.flag_stale(f["demo"], "demo", SHA, [{"token": "not_there.py"}]) == {
        "error": "invalid input"
    }
    assert t.flag_stale(f["demo"], "demo", SHA, []) == {"error": "invalid input"}
    assert t.flag_stale(f["demo"], "demo", "not-a-sha", MISSING) == {"error": "invalid input"}
    assert t.flag_stale(f["demo"], "", SHA, MISSING) == {"error": "invalid input"}
    assert t.flag_stale("not-a-uuid", "demo", SHA, MISSING) == {"error": "not found"}
    assert t.flag_stale(str(uuid.uuid4()), "demo", SHA, MISSING) == {"error": "not found"}
    assert t.flag_stale(beta_fact, "demo", SHA, MISSING) == {"error": "not found"}
    assert _flags(fresh_db, "beta") == 0
    for actions in ({"read"}, {"create", "read"}):
        as_ctx(TokenContext("c1c1c1c1c1c1", "alpha", frozenset(actions)))
        assert t.flag_stale(f["demo"], "demo", SHA, MISSING) == {"error": "forbidden"}
    assert _flags(fresh_db, "alpha") == 0


def test_flag_stale_one_open_flag_per_fact(fresh_db, t, as_ctx, ctx_a):
    _, f = _repo(fresh_db)
    as_ctx(ctx_a)
    assert t.flag_stale(f["demo"], "demo", SHA, MISSING)["flagged"] is True
    assert t.flag_stale(f["demo"], "demo", SHA, MISSING) == {"flagged": False, "reason": "exists"}
    assert t.flag_stale(f["demo"], "demo", "b" * 40, MISSING) == {
        "flagged": False,
        "reason": "exists",
    }
    assert len(_flags(fresh_db, "alpha", f["demo"])) == 1


def test_kept_flag_suppresses_until_the_text_changes(fresh_db, t, as_ctx, ctx_a):
    _, f = _repo(fresh_db)
    as_ctx(ctx_a)
    assert t.flag_stale(f["demo"], "demo", SHA, MISSING)["flagged"] is True
    with fresh_db.ws_tx("alpha") as conn:
        conn.execute(
            "UPDATE ws_alpha.stale_flags SET resolved_at = now(), resolution = 'kept' "
            "WHERE fact_id = %s",
            (f["demo"],),
        )
    assert t.flag_stale(f["demo"], "demo", "b" * 40, MISSING) == {
        "flagged": False,
        "reason": "kept",
    }
    assert t.update_fact(f["demo"], text="on demo: the entry point is still `old_name.py`") == {
        "version": 2
    }
    res = t.flag_stale(f["demo"], "demo", "b" * 40, MISSING)
    assert res["flagged"] is True
    rows = _flags(fresh_db, "alpha", f["demo"])
    assert [(r[1], r[3], r[6]) for r in rows] == [(SHA, 1, "kept"), ("b" * 40, 2, None)]


def test_edit_resolves_open_flag_as_superseded_by_edit(fresh_db, t, as_ctx, ctx_a):
    _, f = _repo(fresh_db)
    as_ctx(ctx_a)
    assert t.flag_stale(f["demo"], "demo", SHA, MISSING)["flagged"] is True
    assert t.update_fact(f["demo"], text="on demo: entry point is `new_name.py`")["version"] == 2
    assert _flags(fresh_db, "alpha", f["demo"])[0][6] is None  # nothing resolves until a flag
    assert t.flag_stale(f["web"], "demo", SHA, MISSING)["flagged"] is True
    [row] = _flags(fresh_db, "alpha", f["demo"])
    assert row[5] is not None and row[6] == "superseded-by-edit"
    with fresh_db.ws_tx("alpha") as conn:
        assert stale.resolve_edited(conn, "alpha") == 0


def test_load_fact_locks_and_reports_not_found(fresh_db):
    from treant.errors import NotFound

    n, f = _repo(fresh_db)
    with fresh_db.ws_tx("alpha") as conn:
        assert stale.load_fact(conn, "alpha", f["demo"]) == (
            "current",
            "on demo: entry point is `old_name.py`",
            1,
            n["demo"],
        )
        for bad in ("not-a-uuid", str(uuid.uuid4())):
            with pytest.raises(NotFound):
                stale.load_fact(conn, "alpha", bad)


def _marker_repo(db):
    """alpha: repo "demo" with a warning and a plain fact that both name `old_name.py`, and a
    general fact on a general node of the same name."""
    with db.publisher_tx() as conn:
        gnode = graph.add_node(conn, "general", "concept", "repo", "demo")
        gfact = graph.add_fact(
            conn, "general", "fact", "general entry is `old_name.py`", node_id=gnode
        )
    with db.ws_tx("alpha") as conn:
        node = graph.add_node(conn, "alpha", "instance", "repo", "demo")
        warn = graph.add_fact(conn, "alpha", "warning", "never delete `old_name.py`", node_id=node)
        fact = graph.add_fact(conn, "alpha", "fact", "entry is `old_name.py`", node_id=node)
    return warn, fact, gfact


def _open_flag_count(db, fact_id):
    return len([r for r in _flags(db, "alpha", fact_id) if r[5] is None])


def test_brief_and_search_show_the_marker_until_resolved(fresh_db, t, as_ctx, ctx_a):
    warn, fact, _ = _marker_repo(fresh_db)
    as_ctx(ctx_a)
    commit = "abc1234" + "0" * 33
    missing = [{"token": "old_name.py", "commit": None, "subject": None}]
    with fresh_db.ws_tx("alpha") as conn:
        assert stale.flag(conn, "alpha", warn, "demo", commit, missing, [])["flagged"] is True
        assert stale.flag(conn, "alpha", fact, "demo", commit, missing, [])["flagged"] is True
    note = "(in doubt: old_name.py not found at abc1234)"

    out = brief.brief(fresh_db, ctx_a, "demo")
    assert f"entry is `old_name.py` {note}" in out
    assert f"- never delete `old_name.py` {note}" in out  # Warnings section
    assert "general entry is `old_name.py`\n" in out + "\n"  # general facts carry none
    assert f"general entry is `old_name.py` {note}" not in out
    only = brief.brief(fresh_db, ctx_a, "demo", repo_only=True)
    assert f"[fact] entry is `old_name.py` {note}" in only
    assert "never delete" not in only  # repo_only skips warnings

    hits = search.search(fresh_db, FakeEmbedder(down=True), ctx_a, "old_name.py").hits
    hit = next(h for h in hits if h["text"] == "entry is `old_name.py`")
    assert hit["in_doubt"] == note
    general = next(h for h in hits if h["namespace"] == "general" and h["kind"] == "fact")
    assert "in_doubt" not in general
    assert all("in_doubt" not in h for h in hits if h["kind"] != "fact")

    with fresh_db.ws_tx("alpha") as conn:  # owner keeps the warning's flag
        conn.execute(
            "UPDATE ws_alpha.stale_flags SET resolved_at = now(), resolution = 'kept' "
            "WHERE fact_id = %s",
            (warn,),
        )
    out = brief.brief(fresh_db, ctx_a, "demo")
    assert "- never delete `old_name.py`\n" in out + "\n"
    assert f"entry is `old_name.py` {note}" in out  # the other one is still in doubt

    assert t.update_fact(fact, text="entry is `new_name.py`")["version"] == 2
    assert _open_flag_count(fresh_db, fact) == 1  # nothing persisted the resolution yet
    out = brief.brief(fresh_db, ctx_a, "demo")
    assert "(in doubt:" not in out  # gone at once
    hits = search.search(fresh_db, FakeEmbedder(down=True), ctx_a, "new_name.py").hits
    assert all("in_doubt" not in h for h in hits)


def test_open_markers_is_read_only_and_scoped(fresh_db):
    warn, fact, _ = _marker_repo(fresh_db)
    with fresh_db.ws_tx("alpha") as conn:
        assert stale.open_markers(conn, "alpha", []) == {}
        stale.flag(
            conn,
            "alpha",
            fact,
            "demo",
            "abc1234" + "0" * 33,
            [
                {"token": "old_name.py"},
                {"token": "entry"},
            ],
            [],
        )
        got = stale.open_markers(conn, "alpha", [fact, warn])
    assert got == {fact: "(in doubt: old_name.py and 1 more not found at abc1234)"}
    with fresh_db.ws_tx("beta") as conn:
        assert stale.open_markers(conn, "beta", [fact]) == {}
