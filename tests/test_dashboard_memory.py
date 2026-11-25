"""Dashboard memory pages: browse, node, fact edit/retract/restore, merges, conflicts."""

import pytest
from dashboard_fakes import PUBLIC_URL, FakeProvider, dash_settings, session_data, set_session
from fakes import FakeEmbedder
from starlette.testclient import TestClient

from treant import graph


def _logged_in(db, embedder):
    from treant.dashboard.app import build_dashboard

    app = build_dashboard(db, embedder, dash_settings(), FakeProvider())
    c = TestClient(app, base_url=PUBLIC_URL, follow_redirects=False)
    c.get("/login")
    assert c.get("/auth/callback").status_code == 303
    return c


@pytest.fixture
def client(fresh_db):
    with _logged_in(fresh_db, FakeEmbedder()) as c:
        yield c


@pytest.fixture
def kw_client(fresh_db):
    """Embedder down: search falls back to keyword matching (no embeddings needed)."""
    with _logged_in(fresh_db, FakeEmbedder(down=True)) as c:
        yield c


def _csrf(c):
    return session_data(c)["csrf"]


def _post(c, path, **data):
    return c.post(path, data={"csrf": _csrf(c), **data})


def _audit(db, action):
    with db.admin_tx() as c:
        return c.execute(
            "SELECT actor, detail FROM control.audit WHERE action = %s ORDER BY id", (action,)
        ).fetchall()


def _fact(db, ns, text, node=None, type="lesson"):
    tx = db.publisher_tx if ns == "general" else (lambda: db.ws_tx(ns))
    with tx() as c:
        return graph.add_fact(c, ns, type, text, node_id=node)


def _node(db, name="postgres", ns="alpha"):
    with db.ws_tx(ns) as c:
        return graph.add_node(c, ns, "instance", "db", name)


def _flashes(c):
    return [f[1] for f in session_data(c).get("flash", [])]


def test_untrusted_text_is_escaped(fresh_db, client):
    nid = _node(fresh_db)
    fid = _fact(fresh_db, "alpha", "<script>alert(1)</script>", nid)
    for path in (f"/memory/alpha/node/{nid}", f"/memory/alpha/fact/{fid}/history"):
        body = client.get(path).text
        assert "&lt;script&gt;" in body
        assert "<script>alert" not in body


def test_edit_fact_from_dashboard_audits_dashboard_actor(fresh_db, client):
    nid = _node(fresh_db)
    fid = _fact(fresh_db, "alpha", "old text", nid)
    r = _post(client, f"/memory/alpha/fact/{fid}/edit", text="new text", status="current")
    assert r.status_code == 303 and r.headers["location"] == f"/memory/alpha/node/{nid}"
    assert _audit(fresh_db, "fact.edit")[-1][0] == "dashboard:alice"
    assert "new text" in client.get(f"/memory/alpha/node/{nid}").text


def test_edit_without_csrf_is_refused(fresh_db, client):
    fid = _fact(fresh_db, "alpha", "x")
    r = client.post(f"/memory/alpha/fact/{fid}/edit", data={"text": "y"})
    assert r.status_code == 403


def test_general_edit_flagged_shows_force_checkbox_then_succeeds(fresh_db, client, queued_item):
    fid = _fact(fresh_db, "general", "clean")
    assert 'name="force"' in client.get(f"/memory/general/fact/{fid}/history").text
    r = _post(client, f"/memory/general/fact/{fid}/edit", text="use zorblax now", status="current")
    assert r.status_code == 303
    assert r.headers["location"] == f"/memory/general/fact/{fid}/history"
    assert _flashes(client) == ["flagged: the rules matched this text; tick the box to save anyway"]
    assert _audit(fresh_db, "fact.edit") == []
    r = _post(
        client,
        f"/memory/general/fact/{fid}/edit",
        text="use zorblax now",
        status="current",
        force="1",
    )
    assert r.status_code == 303
    assert "use zorblax now" in client.get(f"/memory/general/fact/{fid}/history").text
    assert len(_audit(fresh_db, "fact.edit")) == 1


def test_retract_from_dashboard(fresh_db, client):
    nid = _node(fresh_db)
    fid = _fact(fresh_db, "alpha", "gone", nid)
    r = _post(client, f"/memory/alpha/fact/{fid}/retract")
    assert r.status_code == 303
    assert _audit(fresh_db, "fact.retract")[-1][0] == "dashboard:alice"


def test_restore_from_history(fresh_db, client):
    fid = _fact(fresh_db, "alpha", "first")
    _post(client, f"/memory/alpha/fact/{fid}/edit", text="second", status="current")
    page = client.get(f"/memory/alpha/fact/{fid}/history").text
    assert "first" in page and "Restore this version" in page
    r = _post(client, f"/memory/alpha/fact/{fid}/restore", version="1")
    assert r.status_code == 303
    assert r.headers["location"] == f"/memory/alpha/fact/{fid}/history"  # no node
    assert _audit(fresh_db, "fact.restore")[-1][0] == "dashboard:alice"
    with fresh_db.admin_tx() as c:
        assert c.execute("SELECT text FROM ws_alpha.facts WHERE id = %s", (fid,)).fetchone() == (
            "first",
        )
    _post(client, f"/memory/alpha/fact/{fid}/restore", version="abc")
    assert _flashes(client)[-1] == "version"


def _flag(db):
    with db.ws_tx("alpha") as c:
        a = graph.add_node(c, "alpha", "instance", "db", "postgres")
        b = graph.add_node(c, "alpha", "instance", "db", "postgresql")
        f1 = graph.add_fact(c, "alpha", "fact", "new one", node_id=b)
        f2 = graph.add_fact(c, "alpha", "fact", "old one", node_id=a)
        flags = [
            c.execute(
                "INSERT INTO ws_alpha.merge_flags (node_a, node_b, score) VALUES (%s, %s, 0.9) "
                "RETURNING id",
                pair,
            ).fetchone()[0]
            for pair in ((a, b), (b, a))
        ]
        conf = c.execute(
            "INSERT INTO ws_alpha.conflicts (new_fact, old_fact) VALUES (%s, %s) RETURNING id",
            (f1, f2),
        ).fetchone()[0]
    return a, b, flags, conf


def test_merge_and_dismiss_flags(fresh_db, client):
    _a, _b, (f1, f2), conf = _flag(fresh_db)
    body = client.get("/memory/alpha/merges").text
    assert "postgresql" in body and f"/merges/{f1}/merge" in body
    assert _post(client, f"/memory/alpha/merges/{f2}/dismiss").status_code == 303
    assert _audit(fresh_db, "merge.dismiss")[-1][0] == "dashboard:alice"
    assert _post(client, f"/memory/alpha/merges/{f1}/merge").headers["location"] == (
        "/memory/alpha/merges"
    )
    assert _audit(fresh_db, "merge.merge")[-1][0] == "dashboard:alice"
    assert "No open merge flags" in client.get("/memory/alpha/merges").text
    # conflicts
    assert "old one" in client.get("/memory/alpha/conflicts").text
    _post(client, f"/memory/alpha/conflicts/{conf}/dismiss")
    assert _audit(fresh_db, "conflict.dismiss")[-1][0] == "dashboard:alice"
    assert "No open conflicts" in client.get("/memory/alpha/conflicts").text
    # already resolved: a flash, not a 500
    assert _post(client, f"/memory/alpha/merges/{f1}/dismiss").status_code == 303
    assert _flashes(client)


def test_unknown_namespace_404(client):
    for path in (
        "/memory/Bad",
        "/memory/control",
        "/memory/ws_alpha",
        "/memory/nowhere/search",
        "/memory/general/merges",
        "/memory/general/conflicts",
        "/memory/general/sessions",
        "/memory/alpha/node/not-a-uuid",
        "/memory/alpha/fact/not-a-uuid/history",
    ):
        assert client.get(path).status_code == 404, path


def test_paging_links(fresh_db, client):
    with fresh_db.ws_tx("alpha") as c:
        for i in range(101):
            graph.add_node(c, "alpha", "instance", "db", f"node{i:03d}")
    first = client.get("/memory/alpha").text
    assert "offset=100" in first and ">Previous<" not in first
    second = client.get("/memory/alpha?offset=100").text
    assert ">Previous<" in second and ">Next<" not in second and "node100" in second
    for bad in ("abc", "-5"):
        r = client.get(f"/memory/alpha?offset={bad}")
        assert r.status_code == 200 and ">Previous<" not in r.text


def test_prefix_filter_and_sessions_and_search(fresh_db, client, kw_client):
    nid = _node(fresh_db, "kafka")
    _node(fresh_db, "redis")
    _fact(fresh_db, "alpha", "kafka needs three brokers", nid)
    body = client.get("/memory/alpha?q=ka").text
    assert "kafka" in body and "redis" not in body
    r = kw_client.get("/memory/alpha/search?q=three brokers")
    assert r.status_code == 200 and "kafka needs three brokers" in r.text
    assert f"/memory/alpha/node/{nid}" in kw_client.get("/memory/alpha/search?q=kafka").text
    assert client.get("/memory/alpha/sessions").status_code == 200
    assert client.get("/memory").status_code == 200
    assert client.get("/memory/general/search?q=x").status_code == 200


def _stale(c):
    s = session_data(c)
    set_session(c, {**s, "auth_time": s["auth_time"] - 1000})


def _text(db, schema, fid):
    with db.admin_tx() as c:
        return c.execute(
            f"SELECT text, status, version FROM {schema}.facts WHERE id = %s", (fid,)
        ).fetchone()


def test_forced_general_edit_needs_recent_login(fresh_db, client, queued_item):
    fid = _fact(fresh_db, "general", "plain general text")
    _stale(client)
    path = f"/memory/general/fact/{fid}/edit"
    r = _post(client, path, text="use zorblax for x", status="current", force="1")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login?fresh=1")
    assert _text(fresh_db, "general", fid)[0] == "plain general text"
    assert _audit(fresh_db, "fact.edit") == []
    # an unforced general edit is not sensitive
    r = _post(client, path, text="plain general text again", status="current")
    assert r.status_code == 303 and _flashes(client)[-1] == "fact saved"


def test_status_only_change_keeps_text_and_skips_checks(fresh_db, client, queued_item):
    nid = _node(fresh_db)
    fid = _fact(fresh_db, "alpha", "line one\nline two", nid)
    r = _post(
        client, f"/memory/alpha/fact/{fid}/edit", text="line one\r\nline two", status="superseded"
    )
    assert r.status_code == 303 and _flashes(client)[-1] == "fact saved"
    assert _text(fresh_db, "ws_alpha", fid) == ("line one\nline two", "superseded", 2)
    with fresh_db.admin_tx() as c:
        assert (
            c.execute("SELECT count(*) FROM control.jobs WHERE kind = 'embed'").fetchone()[0] == 0
        )
    # a general fact whose old text the rules would now flag: a status change is not flagged
    gid = _fact(fresh_db, "general", "use zorblax for x")
    _post(client, f"/memory/general/fact/{gid}/edit", text="use zorblax for x", status="superseded")
    assert _flashes(client)[-1] == "fact saved"
    assert _text(fresh_db, "general", gid) == ("use zorblax for x", "superseded", 2)
    # nothing changed at all: no new version
    _post(client, f"/memory/general/fact/{gid}/edit", text="use zorblax for x", status="superseded")
    assert _flashes(client)[-1] == "no changes"
    assert _text(fresh_db, "general", gid)[2] == 2
    # a real edit sent with CRLF is stored with LF
    _post(client, f"/memory/alpha/fact/{fid}/edit", text="line one\r\nline 2", status="current")
    assert _text(fresh_db, "ws_alpha", fid) == ("line one\nline 2", "current", 3)


def test_restore_general_runs_rules_and_force_is_sensitive(fresh_db, client, queued_item):
    from treant import owner

    gid = _fact(fresh_db, "general", "use zorblax for x")
    owner.edit_fact(fresh_db, "cli", "general", gid, "use a cluster for x", None)
    page = client.get(f"/memory/general/fact/{gid}/history").text
    assert page.count('name="force"') == 2  # the edit form and the restore form
    path = f"/memory/general/fact/{gid}/restore"
    _post(client, path, version="1")
    assert _flashes(client)[-1].startswith("flagged")
    assert _text(fresh_db, "general", gid)[0] == "use a cluster for x"
    _stale(client)
    r = _post(client, path, version="1", force="1")
    assert r.headers["location"].startswith("/login?fresh=1")
    assert _text(fresh_db, "general", gid)[0] == "use a cluster for x"
    s = session_data(client)
    set_session(client, {**s, "auth_time": s["auth_time"] + 1000})
    _post(client, path, version="1", force="1")
    assert _flashes(client)[-1] == "fact restored"
    assert _text(fresh_db, "general", gid) == ("use zorblax for x", "current", 3)


def test_sessions_index_lists_workspaces(client):
    assert 'href="/sessions"' in client.get("/").text
    r = client.get("/sessions")
    assert r.status_code == 200
    for ws in ("alpha", "beta", "gamma"):
        assert f'href="/memory/{ws}/sessions"' in r.text


def test_huge_offset_is_clamped_not_500(client):
    for path in (
        "/memory/alpha",
        "/memory/alpha/sessions",
        "/queue",
        "/jobs?status=all",
        "/audit",
        "/mistakes",
    ):
        assert (
            client.get(
                f"{path}{'&' if '?' in path else '?'}offset=9999999999999999999999999"
            ).status_code
            == 200
        )


def test_bad_fact_id_and_missing_version_in_posts_are_flashes(fresh_db, client):
    r = _post(client, "/memory/alpha/fact/not-a-uuid/edit", text="x", status="current")
    assert r.status_code == 303 and _flashes(client)[-1] == "invalid id"
    fid = _fact(fresh_db, "alpha", "first")
    r = _post(client, f"/memory/alpha/fact/{fid}/restore", version="99")
    assert r.status_code == 303 and _flashes(client)[-1] == "version not found"


def test_archived_workspace_is_still_browsable(fresh_db, client):
    from treant import owner

    nid = _node(fresh_db, "kafka", "gamma")
    owner.archive_workspace(fresh_db, "cli", "gamma")
    for path in (
        "/memory/gamma",
        f"/memory/gamma/node/{nid}",
        "/memory/gamma/sessions",
        "/memory/gamma/merges",
        "/workspaces/gamma",
    ):
        assert client.get(path).status_code == 200, path


def test_graph_page_draws_clickable_nodes_without_script_or_inline_style(fresh_db, client):
    a = _node(fresh_db, "api")
    b = _node(fresh_db, "<b>postgres</b>")
    with fresh_db.ws_tx("alpha") as c:
        graph.add_edge(c, "alpha", a, b, "uses")
    r = client.get("/memory/alpha/graph")
    assert r.status_code == 200
    body = r.text
    assert "<svg" in body and f'href="/memory/alpha/graph?focus={a}&amp;depth=1"' in body
    assert "&lt;b&gt;postgres&lt;/b&gt;" in body and "<b>postgres" not in body
    assert ">uses<" in body
    assert "<script" not in body and "style=" not in body
    assert 'href="/memory/alpha/graph"' in client.get("/memory/alpha").text


def test_graph_page_empty_and_unknown(fresh_db, client):
    assert "No nodes yet" in client.get("/memory/alpha/graph").text
    assert client.get("/memory/nosuchws/graph").status_code == 404


def test_graph_overview_hides_unlinked_and_filters(fresh_db, client):
    a, b = _node(fresh_db, "api"), _node(fresh_db, "postgres")
    _node(fresh_db, "lonely-node")
    with fresh_db.ws_tx("alpha") as c:
        graph.add_edge(c, "alpha", a, b, "uses")
    body = client.get("/memory/alpha/graph").text
    assert "lonely-node" not in body and "1 unlinked node hidden" in body
    assert "lonely-node" in client.get("/memory/alpha/graph?unlinked=1").text
    body = client.get("/memory/alpha/graph?q=api&type=db").text
    assert ">api<" in body and 'name="type" value="db" checked' in body


def test_graph_focus_view(fresh_db, client):
    a, b, far = _node(fresh_db, "api"), _node(fresh_db, "postgres"), _node(fresh_db, "far-away")
    with fresh_db.ws_tx("alpha") as c:
        graph.add_edge(c, "alpha", a, b, "uses")
        graph.add_edge(c, "alpha", b, far, "uses")
    body = client.get(f"/memory/alpha/graph?focus={a}").text
    assert "Around api" in body and "far-away" not in body
    assert f'href="/memory/alpha/node/{a}"' in body  # the centre opens the node page
    assert f'href="/memory/alpha/graph?focus={b}&amp;depth=1"' in body  # others re-centre
    assert "far-away" in client.get(f"/memory/alpha/graph?focus={a}&depth=2").text
    assert client.get("/memory/alpha/graph?focus=not-a-uuid").status_code == 404
    assert f'href="/memory/alpha/graph?focus={a}"' in client.get(f"/memory/alpha/node/{a}").text


def test_graph_overview_shows_only_hubs_until_filtered(fresh_db, client, monkeypatch):
    from treant.dashboard.routes import memory

    monkeypatch.setattr(memory, "OVERVIEW_LIMIT", 2)
    hub = _node(fresh_db, "hub")
    with fresh_db.ws_tx("alpha") as c:
        for name in ("s1", "s2", "s3"):
            graph.add_edge(
                c, "alpha", hub, graph.add_node(c, "alpha", "instance", "db", name), "uses"
            )
    assert "Showing the 2 best-connected of 4 nodes" in client.get("/memory/alpha/graph").text
    assert "best-connected" not in client.get("/memory/alpha/graph?type=db").text


# --- fact duplicates ---------------------------------------------------------------------------


def _dup(db, ns, text, angle, age_days=0):
    """A fact with a stored vector at ``angle`` (cosine similarity of two is cos(a - b))."""
    import math

    fid = _fact(db, ns, text)
    v = [0.0] * 768
    v[0], v[1] = math.cos(angle), math.sin(angle)
    schema = "general" if ns == "general" else f"ws_{ns}"
    with db.admin_tx() as c:
        c.execute(
            f"INSERT INTO {schema}.embeddings (item_kind, item_id, vec) VALUES ('fact', %s, %s)",
            (fid, "[" + ",".join(map(str, v)) + "]"),
        )
        c.execute(
            f"UPDATE {schema}.facts SET created_at = now() - make_interval(days => %s) "
            "WHERE id = %s",
            (age_days, fid),
        )
    return fid


def _fact_status(db, ns, fid):
    schema = "general" if ns == "general" else f"ws_{ns}"
    with db.admin_tx() as c:
        return c.execute(
            f"SELECT status, text FROM {schema}.facts WHERE id = %s", (fid,)
        ).fetchone()


def test_duplicates_page_lists_groups_with_newest_preselected(fresh_db, client):
    old = _dup(fresh_db, "alpha", "deploy <b>with</b> helmfile", 0.0, age_days=3)
    new = _dup(fresh_db, "alpha", "deploy using helmfile", 0.1)
    body = client.get("/memory/alpha/duplicates").text
    assert "deploy &lt;b&gt;with&lt;/b&gt; helmfile" in body and "deploy using helmfile" in body
    assert 'action="/memory/alpha/duplicates/merge"' in body
    assert 'action="/memory/alpha/duplicates/dismiss"' in body
    assert f'name="keep" value="{new}" checked' in body
    assert f'name="keep" value="{old}">' in body
    assert "Not duplicates" in body
    assert "No duplicates" in client.get("/memory/beta/duplicates").text
    index = client.get("/memory").text
    assert "/memory/general/duplicates" in index and "/memory/alpha/duplicates" in index
    assert "/memory/alpha/duplicates" in client.get("/memory/alpha").text
    assert "/memory/general/duplicates" in client.get("/memory/general").text
    for path in ("/memory/nowhere/duplicates", "/memory/Bad/duplicates"):
        assert client.get(path).status_code == 404, path


def test_duplicates_merge_needs_csrf_then_merges(fresh_db, client):
    old = _dup(fresh_db, "alpha", "old wording", 0.0, age_days=1)
    new = _dup(fresh_db, "alpha", "new wording", 0.1)
    path = "/memory/alpha/duplicates/merge"
    r = client.post(path, data={"keep": new, "ids": [new, old]})
    assert r.status_code == 403
    assert _fact_status(fresh_db, "alpha", old)[0] == "current"
    r = _post(client, path, keep=new, ids=[new, old], text="")
    assert r.status_code == 303 and r.headers["location"] == "/memory/alpha/duplicates"
    assert _flashes(client) == ["merged 1 fact"]
    assert _fact_status(fresh_db, "alpha", old)[0] == "superseded"
    assert _fact_status(fresh_db, "alpha", new) == ("current", "new wording")
    assert _audit(fresh_db, "fact.merge")[-1] == (
        "dashboard:alice",
        {"ns": "alpha", "keep": new, "dropped": [old], "edited": False},
    )
    assert "No duplicates" in client.get("/memory/alpha/duplicates").text


def test_duplicates_merge_with_edited_text_in_general(fresh_db, client):
    a = _dup(fresh_db, "general", "use a lock file", 0.0, age_days=1)
    b = _dup(fresh_db, "general", "commit the lock file", 0.1)
    assert "commit the lock file" in client.get("/memory/general/duplicates").text
    r = _post(
        client, "/memory/general/duplicates/merge", keep=a, ids=[a, b], text="commit lock files"
    )
    assert r.status_code == 303 and r.headers["location"] == "/memory/general/duplicates"
    assert _fact_status(fresh_db, "general", a) == ("current", "commit lock files")
    assert _fact_status(fresh_db, "general", b)[0] == "superseded"
    assert _audit(fresh_db, "fact.merge")[-1][1]["edited"] is True


def test_duplicates_merge_bad_forms_are_flashes(fresh_db, client):
    a = _dup(fresh_db, "alpha", "a", 0.0)
    b = _dup(fresh_db, "alpha", "b", 0.1)
    path = "/memory/alpha/duplicates/merge"
    for data, msg in (
        ({"ids": [a, b]}, "pick the wording to keep"),
        ({"keep": "00000000-0000-0000-0000-000000000000", "ids": [a, b]}, "pick the wording"),
        ({"keep": a, "ids": [a]}, "nothing to merge"),
        ({"keep": a, "ids": [a, "junk"]}, "invalid id"),
    ):
        r = _post(client, path, **data)
        assert r.status_code == 303 and r.headers["location"] == "/memory/alpha/duplicates"
        assert _flashes(client)[-1].startswith(msg), data
    assert _fact_status(fresh_db, "alpha", b)[0] == "current"
    assert _audit(fresh_db, "fact.merge") == []


def test_not_duplicates_hides_the_group_for_this_session_only(fresh_db, client):
    _dup(fresh_db, "alpha", "first wording", 0.0)
    _dup(fresh_db, "alpha", "second wording", 0.1)
    key = browse_key(fresh_db)
    path = "/memory/alpha/duplicates/dismiss"
    assert client.post(path, data={"key": key}).status_code == 403
    r = _post(client, path, key=key)
    assert r.status_code == 303 and r.headers["location"] == "/memory/alpha/duplicates"
    body = client.get("/memory/alpha/duplicates").text
    assert "first wording" not in body and "1 group hidden for this session" in body
    _post(client, path, key="not a key")
    assert _flashes(client)[-1] == "invalid group"
    with _logged_in(fresh_db, FakeEmbedder()) as other:
        assert "first wording" in other.get("/memory/alpha/duplicates").text
    with fresh_db.admin_tx() as c:  # nothing stored
        assert c.execute(
            "SELECT count(*) FROM control.audit WHERE action LIKE 'fact.%'"
        ).fetchone() == (0,)


def browse_key(db, ns="alpha"):
    from treant import browse

    (group,) = browse.duplicates(db, ns)
    return group["key"]
