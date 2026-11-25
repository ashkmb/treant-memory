import uuid

import pytest

from treant import graph
from treant.brief import MARKER, brief
from treant.tokens import TokenContext


@pytest.fixture
def ctx_fresh(db, prefix, admin_dsn):
    from treant import migrate

    name = "br" + uuid.uuid4().hex[:8]
    migrate.create_workspace(admin_dsn, prefix, name)
    return TokenContext("b1b1b1b1b1b1", name, frozenset({"read"}))


def test_brief_empty_workspace_is_empty_string(db, ctx_fresh):
    assert brief(db, ctx_fresh, None) == ""


def test_brief_order_and_cap(db, ctx_fresh):
    ws = ctx_fresh.workspace
    with db.ws_tx(ws) as c:
        for i in range(100):
            graph.add_fact(c, ws, "warning", f"warning number {i} " + "x" * 80)
        graph.add_fact(c, ws, "open_thread", "thread one")
    out = brief(db, ctx_fresh, None)
    assert len(out) <= 6000 and MARKER in out
    assert out.startswith("## Warnings")
    # many warnings do not crowd out the other sections
    assert "## Open threads\n- thread one" in out
    assert out.index(MARKER) < out.index("## Open threads")
    with db.ws_tx(ws) as c:
        c.execute(
            f"DELETE FROM ws_{ws}.facts WHERE type = 'warning' AND text NOT LIKE '%number 0 %'"
        )
    out = brief(db, ctx_fresh, None)
    assert out.index("## Warnings") < out.index("## Open threads")
    assert MARKER not in out


def test_brief_repo_section_uses_alias(db, ctx_fresh):
    ws = ctx_fresh.workspace
    with db.ws_tx(ws) as c:
        n = graph.add_node(c, ws, "instance", "repo", "treant-core", aliases=["TRT"])
        m = graph.add_node(c, ws, "instance", "service", "pgneighbour")
        graph.add_edge(c, ws, n, m, "uses")
        graph.add_fact(c, ws, "fact", "neighbour fact text", node_id=m)
        graph.add_fact(c, ws, "fact", "own fact text", node_id=n)
    out = brief(db, ctx_fresh, "trt")
    assert "## trt" in out and "treant-core" in out
    assert "neighbour fact text" in out and "own fact text" in out
    assert "## trt" not in brief(db, ctx_fresh, None)


def test_brief_omits_canary_items_unless_asked_by_name(db, ctx_fresh):
    from fakes import FakeEmbedder

    from treant import canary

    ws = ctx_fresh.workspace
    with db.ws_tx(ws) as c:
        graph.add_fact(c, ws, "warning", "real warning stays")
        graph.add_fact(c, ws, "open_thread", "real thread stays")
    marker = canary.plant(db, FakeEmbedder(), ws)
    with db.ws_tx(ws) as c:  # a canary open thread too, to cover both sections
        node = c.execute(
            f"SELECT id::text FROM ws_{ws}.nodes WHERE name = %s", (marker,)
        ).fetchone()[0]
        graph.add_fact(c, ws, "open_thread", f"canary thread {marker}", node_id=node)
    out = brief(db, ctx_fresh, None)
    assert marker not in out
    assert "real warning stays" in out and "real thread stays" in out
    assert marker in brief(db, ctx_fresh, marker)


def test_brief_open_threads_newest_first(db, ctx_fresh):
    ws = ctx_fresh.workspace
    with db.ws_tx(ws) as c:
        for i, age in enumerate(("3 days", "1 day", "2 days")):
            c.execute(
                f"INSERT INTO ws_{ws}.facts (type, text, created_at) "
                "VALUES ('open_thread', %s, now() - %s::interval)",
                (f"thread {i}", age),
            )
        for i, age in enumerate(("2 days", "3 days")):
            c.execute(
                f"INSERT INTO ws_{ws}.facts (type, text, created_at) "
                "VALUES ('warning', %s, now() - %s::interval)",
                (f"warning {i}", age),
            )
    out = brief(db, ctx_fresh, None)
    assert out == (
        "## Warnings\n- warning 1\n- warning 0\n\n"
        "## Open threads\n- thread 1\n- thread 2\n- thread 0"
    )


def test_brief_repo_heading_is_one_line(db, ctx_fresh):
    ws = ctx_fresh.workspace
    with db.ws_tx(ws) as c:
        n = graph.add_node(c, ws, "instance", "repo", "multi line\n## Injected")
        graph.add_fact(c, ws, "fact", "repo fact", node_id=n)
    out = brief(db, ctx_fresh, "multi line\n## Injected")
    assert "repo fact" in out
    assert "\n## Injected" not in out.split("\n", 1)[0]
    assert out.splitlines()[0] == "## multi line ## Injected"
    long = brief(db, ctx_fresh, "x" * 500)
    assert long == ""  # no such node: no section at all


def test_brief_long_repo_name_heading_is_capped(db, ctx_fresh):
    ws = ctx_fresh.workspace
    name = "r" * 300
    with db.ws_tx(ws) as c:
        n = graph.add_node(c, ws, "instance", "repo", name)
        graph.add_fact(c, ws, "fact", "long repo fact", node_id=n)
    first = brief(db, ctx_fresh, name).splitlines()[0]
    assert first.startswith("## r") and len(first) <= 3 + 200


def test_brief_repo_only_leaves_out_warnings_and_threads(db, ctx_fresh):
    ws = ctx_fresh.workspace
    with db.ws_tx(ws) as c:
        for i in range(100):
            graph.add_fact(c, ws, "warning", f"warning number {i} " + "x" * 80)
            graph.add_fact(c, ws, "open_thread", f"thread number {i} " + "y" * 80)
        n = graph.add_node(c, ws, "instance", "repo", "solo-repo", aliases=["me/solo-repo"])
        for i in range(30):
            graph.add_fact(c, ws, "fact", f"repo fact {i} " + "z" * 150, node_id=n)
    out = brief(db, ctx_fresh, "me/solo-repo", repo_only=True)
    assert out.startswith("## me/solo-repo\n### solo-repo (repo)")
    assert "warning number" not in out and "thread number" not in out
    assert "## Warnings" not in out and "## Open threads" not in out
    # the whole cap goes to the repo section: more of it than in the full brief
    full = brief(db, ctx_fresh, "me/solo-repo")
    assert len(out) > len(full.split("## me/solo-repo", 1)[1])
    assert len(out) <= 6000


def test_brief_repo_only_without_repo_or_node_is_empty(db, ctx_fresh):
    ws = ctx_fresh.workspace
    with db.ws_tx(ws) as c:
        graph.add_fact(c, ws, "warning", "a warning")
        graph.add_fact(c, ws, "open_thread", "a thread")
    assert brief(db, ctx_fresh, None, repo_only=True) == ""
    assert brief(db, ctx_fresh, "", repo_only=True) == ""
    assert brief(db, ctx_fresh, "no-such-repo", repo_only=True) == ""


def test_brief_repo_only_is_capped(db, ctx_fresh):
    ws = ctx_fresh.workspace
    with db.ws_tx(ws) as c:
        n = graph.add_node(c, ws, "instance", "repo", "big-repo")
        for j in range(8):
            m = graph.add_node(c, ws, "instance", "service", f"svc{j}")
            graph.add_edge(c, ws, n, m, "uses")
            for i in range(20):
                graph.add_fact(c, ws, "fact", f"svc{j} fact {i} " + "w" * 150, node_id=m)
    out = brief(db, ctx_fresh, "big-repo", repo_only=True)
    assert len(out) <= 6000 and out.endswith(MARKER)


def test_brief_repo_only_matches_a_bare_named_repo_and_skips_warnings(db, ctx_fresh):
    ws = ctx_fresh.workspace
    with db.ws_tx(ws) as c:
        n = graph.add_node(c, ws, "instance", "repo", "agent-harness")  # no alias
        host = graph.add_node(c, ws, "instance", "host", "agent-box")
        graph.add_edge(c, ws, n, host, "runs_on")
        graph.add_fact(c, ws, "lesson", "use python -m pytest", node_id=n)
        graph.add_fact(c, ws, "warning", "repo warning names a credential", node_id=n)
        graph.add_fact(c, ws, "open_thread", "repo thread", node_id=n)
        graph.add_fact(c, ws, "warning", "host warning names a key", node_id=host)
        graph.add_fact(c, ws, "fact", "host has 30 GB", node_id=host)
    out = brief(db, ctx_fresh, "agent-harness", repo_only=True)
    assert "use python -m pytest" in out and "host has 30 GB" in out
    assert "credential" not in out and "repo thread" not in out and "names a key" not in out
    # the plain brief still shows them in the repo section
    full = brief(db, ctx_fresh, "agent-harness")
    assert "repo warning names a credential" in full and "host warning names a key" in full
