import logging
import uuid

import pytest
from fakes import FakeEmbedder

from treant import auth, graph, jobs, search, tools, worker
from treant.tokens import TokenContext


@pytest.fixture
def t(db):
    return tools.Tools(db, FakeEmbedder())


@pytest.fixture
def as_ctx():
    tokens = []

    def use(ctx):
        tokens.append(auth.current_token.set(ctx))

    yield use
    for tok in reversed(tokens):
        auth.current_token.reset(tok)


def _fact(db, ns, text, type="fact"):
    with db.ws_tx(ns) if ns != "general" else db.admin_tx() as conn:
        return graph.add_fact(conn, ns, type, text)


def test_read_only_token_cannot_save(t, as_ctx):
    as_ctx(TokenContext("b1b1b1b1b1b1", "alpha", frozenset({"read"})))
    assert t.save_session("s-ro", {"journal": "x"}) == {"error": "forbidden"}
    assert t.report_mistake("x") == {"error": "forbidden"}
    assert t.retract(str(uuid.uuid4())) == {"error": "forbidden"}
    assert t.update_fact(str(uuid.uuid4()), text="y") == {"error": "forbidden"}
    assert "hits" in t.search("anything")


def test_update_general_fact_is_not_found(db, t, as_ctx, ctx_a):
    as_ctx(ctx_a)
    gid = _fact(db, "general", "general know-how fact")
    assert t.update_fact(gid, text="hijack") == {"error": "not found"}
    assert t.retract(gid) == {"error": "not found"}
    assert t.update_fact("not-a-uuid", text="x") == {"error": "not found"}


def test_update_fact_validates_status(db, t, as_ctx, ctx_a):
    as_ctx(ctx_a)
    fid = _fact(db, "alpha", "status check fact")
    assert t.update_fact(fid, status="superseded") == {"error": "invalid input"}
    assert t.update_fact(fid, status="closed") == {"error": "invalid input"}  # not an open thread
    assert t.update_fact(fid) == {"version": 1}  # nothing to change: no new version


def test_update_fact_reembeds(db, t, as_ctx, ctx_a):
    as_ctx(ctx_a)
    fid = _fact(db, "alpha", "original wording about kestrels")
    emb = FakeEmbedder()
    with db.ws_tx("alpha") as conn:  # a bare add_fact queues nothing; seed the first embedding
        jobs.enqueue(conn, "alpha", "embed", f"seed:{fid}", {"revision_id": None})
    while worker.run_once(db, emb):
        pass

    def count():
        with db.ws_tx("alpha") as conn:
            return conn.execute(
                "SELECT count(*) FROM ws_alpha.embeddings WHERE item_kind='fact' AND item_id=%s",
                (fid,),
            ).fetchone()[0]

    assert count() == 1
    assert t.update_fact(fid, text="new wording about ospreys")["version"] == 2
    assert count() == 0
    while worker.run_once(db, emb):
        pass
    assert count() == 1


def test_retract_and_mistake(db, t, as_ctx, ctx_a):
    as_ctx(ctx_a)
    fid = _fact(db, "alpha", "to be retracted")
    assert t.retract(fid) == {"ok": True}
    assert t.report_mistake("x" * 2001) == {"error": "invalid input"}
    assert t.report_mistake("") == {"error": "invalid input"}
    assert t.report_mistake("wrong command suggested") == {"ok": True}


def test_search_brief_guard_shapes(db, t, as_ctx, ctx_a):
    as_ctx(ctx_a)
    r = t.save_session("s-shape", {"journal": "investigated osprey nesting"})
    assert set(r) == {"session_id", "revision"} and r["revision"] == 1
    while worker.run_once(db, FakeEmbedder()):
        pass
    s = t.search("osprey nesting")
    assert s["mode"] in ("hybrid", "keyword") and s["hits"]
    assert isinstance(t.brief()["brief"], str)
    assert t.brief(None, repo_only=True) == {"brief": "", "repo_only": True}
    assert "repo_only" not in t.brief("x")
    assert t.brief("x", repo_only="yes") == {"error": "invalid input"}
    assert t.warnings(["x"]) == {"warnings": []}
    assert t.guard_check("ls", "/tmp") == {
        "risky": False,
        "unlisted": [],
        "warnings": [],
        "unresolved": [],
    }
    assert t.guard_check("kubectl get po", "/tmp")["unresolved"] == ["kube_context"]
    out = t.guard_check("ansible-playbook s.yml", "/tmp", ansible_inventory="inv/hosts.yml")
    assert out["unresolved"] == []
    assert out["unlisted"] == [{"kind": "ansible_inventory", "value": "inv/hosts.yml"}]


def test_tool_errors_are_categories_only(db, t, as_ctx, ctx_a, monkeypatch, caplog):
    as_ctx(ctx_a)

    def boom(*a, **k):
        raise RuntimeError("secret-detail-xyz")

    monkeypatch.setattr(search, "search", boom)
    with caplog.at_level(logging.DEBUG):
        r = t.search("q")
    assert r == {"error": "internal error"}
    assert "secret-detail-xyz" not in caplog.text and "secret-detail-xyz" not in str(r)
    assert "RuntimeError" in caplog.text


def _status_text(db, fid):
    with db.ws_tx("alpha") as conn:
        return conn.execute(
            "SELECT status, text, version FROM ws_alpha.facts WHERE id = %s", (fid,)
        ).fetchone()


def test_update_cannot_undo_retract_or_supersede(db, t, as_ctx, ctx_a):
    as_ctx(TokenContext("u1u1u1u1u1u1", "alpha", frozenset({"update"})))
    retracted = _fact(db, "alpha", "retracted fact text")
    superseded = _fact(db, "alpha", "superseded fact text")
    with db.ws_tx("alpha") as conn:
        graph.retract(conn, "alpha", "fact", retracted)
        graph.update_fact(conn, "alpha", superseded, status="superseded")
    for fid in (retracted, superseded):
        before = _status_text(db, fid)
        assert t.update_fact(fid, status="current") == {"error": "invalid input"}
        assert t.update_fact(fid, text="revived text") == {"error": "invalid input"}
        assert t.update_fact(fid) == {"error": "invalid input"}
        assert _status_text(db, fid) == before


def test_update_status_current_only_reopens_closed_threads(db, t, as_ctx, ctx_a):
    as_ctx(ctx_a)
    thread = _fact(db, "alpha", "open thread to close", type="open_thread")
    assert t.update_fact(thread, status="closed")["version"] == 2
    assert t.update_fact(thread, status="current")["version"] == 3
    assert _status_text(db, thread)[0] == "current"
    assert t.update_fact(thread, status="current") == {"version": 3}  # already current: no-op
    plain = _fact(db, "alpha", "already current")
    assert t.update_fact(plain, status="current") == {"version": 1}
    assert t.update_fact(plain, text="new text", status="current") == {"version": 2}
    assert _status_text(db, plain) == ("current", "new text", 2)


def test_update_text_must_be_non_empty_and_capped(db, t, as_ctx, ctx_a):
    as_ctx(ctx_a)
    fid = _fact(db, "alpha", "text limits fact")
    for bad in ("", "   \n\t", "x" * 20_001):
        assert t.update_fact(fid, text=bad) == {"error": "invalid input"}
    assert _status_text(db, fid)[1:] == ("text limits fact", 1)
    assert t.update_fact(fid, text="y" * 20_000)["version"] == 2


def test_other_workspace_fact_id_is_not_found(db, t, as_ctx, ctx_a):
    as_ctx(ctx_a)
    beta_fact = _fact(db, "beta", "beta private fact")

    def beta_row():
        with db.ws_tx("beta") as conn:
            return conn.execute(
                "SELECT status, text, version FROM ws_beta.facts WHERE id = %s", (beta_fact,)
            ).fetchone()

    before = beta_row()
    assert t.update_fact(beta_fact, text="overwritten from alpha") == {"error": "not found"}
    assert t.update_fact(beta_fact, status="closed") == {"error": "not found"}
    assert t.retract(beta_fact) == {"error": "not found"}
    assert beta_row() == before == ("current", "beta private fact", 1)


def _set_terms(db, ws, terms):
    from psycopg.types.json import Jsonb

    with db.admin_tx() as conn:
        conn.execute(
            "UPDATE control.workspaces SET settings = %s WHERE name = %s",
            (Jsonb({"rules": {"terms": terms}}), ws),
        )


def _text_of(db, ws, fid):
    with db.ws_tx(ws) as conn:
        return conn.execute(
            f"SELECT text, version FROM ws_{ws}.facts WHERE id = %s", (fid,)
        ).fetchone()


def test_update_fact_text_is_scrubbed_with_workspace_terms(fresh_db, as_ctx, ctx_a):
    _set_terms(fresh_db, "alpha", ["zentrocorp"])
    t = tools.Tools(fresh_db, FakeEmbedder())
    as_ctx(ctx_a)
    fid = _fact(fresh_db, "alpha", "plain fact")
    out = t.update_fact(fid, text="deploy via ci.zentrocorp.io, password=Hunter2Secret99")
    assert out == {"version": 2}
    text, version = _text_of(fresh_db, "alpha", fid)
    assert "zentrocorp" not in text and "Hunter2Secret99" not in text
    assert "[removed]" in text and version == 2


def test_update_fact_not_confident_writes_nothing(fresh_db, as_ctx, ctx_a):
    t = tools.Tools(fresh_db, FakeEmbedder())
    as_ctx(ctx_a)
    fid = _fact(fresh_db, "alpha", "kept as is")
    many = " ".join(f"10.0.0.{i}" for i in range(1, 15))
    assert t.update_fact(fid, text=f"hosts {many}") == {"error": "invalid input"}
    assert _text_of(fresh_db, "alpha", fid) == ("kept as is", 1)


def test_update_fact_with_bad_settings_writes_nothing(fresh_db, as_ctx, ctx_a):
    from psycopg.types.json import Jsonb

    with fresh_db.admin_tx() as conn:
        conn.execute(
            "UPDATE control.workspaces SET settings = %s WHERE name = 'alpha'",
            (Jsonb({"bogus": 1}),),
        )
    t = tools.Tools(fresh_db, FakeEmbedder())
    as_ctx(ctx_a)
    fid = _fact(fresh_db, "alpha", "untouched")
    assert t.update_fact(fid, text="new text") == {"error": "invalid input"}
    assert _text_of(fresh_db, "alpha", fid) == ("untouched", 1)
    assert t.update_fact(fid) == {"version": 1}  # no text: nothing to scrub, settings unused


def _embed_jobs(db, fid):
    with db.control_tx() as conn:
        return conn.execute(
            "SELECT key FROM control.jobs WHERE workspace = 'alpha' AND kind = 'embed' "
            "AND key LIKE %s ORDER BY key",
            (f"fact:{fid}:%",),
        ).fetchall()


def test_update_with_unchanged_text_queues_nothing_and_keeps_version(db, t, as_ctx, ctx_a):
    as_ctx(ctx_a)
    fid = _fact(db, "alpha", "unchanged wording")
    assert t.update_fact(fid, text="unchanged wording") == {"version": 1}
    assert _embed_jobs(db, fid) == []
    assert t.update_fact(fid, text="changed wording") == {"version": 2}
    assert _embed_jobs(db, fid) == [(f"fact:{fid}:2",)]


def test_guard_check_bad_remotes_is_an_error_category(t, as_ctx, ctx_a):
    as_ctx(ctx_a)
    out = t.guard_check("git push", "/tmp", git_remotes=[1, 2])
    assert set(out) == {"error"}


def test_token_comes_from_the_request_scope_only(ctx_a):
    from types import SimpleNamespace

    from treant import server

    no_request = SimpleNamespace(request_context=SimpleNamespace(request=None))
    reset = auth.current_token.set(ctx_a)
    try:
        assert server._token_of(no_request) is None
    finally:
        auth.current_token.reset(reset)
    scoped = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(scope={"state": {auth.SCOPE_KEY: ctx_a}})
        )
    )
    assert server._token_of(scoped) is ctx_a
