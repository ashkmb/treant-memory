import uuid

from treant import graph, restore

WS = "alpha"


def _tag() -> str:
    return uuid.uuid4().hex[:8]


def _fact(db, text, type_="lesson", node=None, hide="reconcile", ns=WS):
    tx = db.publisher_tx if ns == "general" else db.admin_tx
    with tx() as conn:
        fid = graph.add_fact(conn, ns, type_, text, node)
        if hide:
            graph.set_status(conn, ns, "fact", fid, "superseded", hide)
    return fid


def _node(db, name, hide=True, aliases=()):
    with db.admin_tx() as conn:
        nid = graph.add_node(conn, WS, "instance", "cluster", name, list(aliases))
        if hide:
            graph.set_status(conn, WS, "node", nid, "superseded", "reconcile")
    return nid


def _status(db, table, item_id, ns=WS):
    schema = "general" if ns == "general" else f"ws_{ns}"
    tx = db.publisher_tx if ns == "general" else db.admin_tx
    with tx() as conn:
        return conn.execute(
            f"SELECT status FROM {schema}.{table} WHERE id = %s", (item_id,)
        ).fetchone()[0]


def _reasons(db, kind, item_id):
    with db.admin_tx() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT reason FROM ws_alpha.versions WHERE item_kind = %s AND item_id = %s "
                "ORDER BY version",
                (kind, item_id),
            )
        ]


def test_restores_reconcile_superseded_fact(fresh_db):
    fid = _fact(fresh_db, "pin the chart version")
    counts = restore.run(fresh_db, WS)
    assert counts["facts_restored"] == 1
    assert _status(fresh_db, "facts", fid) == "current"
    assert _reasons(fresh_db, "fact", fid) == ["reconcile", "restore"]


def test_leaves_facts_hidden_for_other_reasons(fresh_db):
    by_ref = _fact(fresh_db, "old way of doing it", hide="superseded")
    retracted = _fact(fresh_db, "a wrong claim", hide=None)
    edited = _fact(fresh_db, "reconciled then closed by the owner")
    with fresh_db.admin_tx() as conn:
        graph.retract(conn, WS, "fact", retracted)
        graph.set_status(conn, WS, "fact", edited, "superseded", "superseded")
    counts = restore.run(fresh_db, WS)
    assert counts == dict.fromkeys(restore.COUNTS, 0)
    assert _status(fresh_db, "facts", by_ref) == "superseded"
    assert _status(fresh_db, "facts", retracted) == "retracted"
    assert _status(fresh_db, "facts", edited) == "superseded"


def test_never_restores_open_thread(fresh_db):
    fid = _fact(fresh_db, "finish the migration", type_="open_thread")
    counts = restore.run(fresh_db, WS)
    assert counts["facts_open_thread"] == 1 and counts["facts_restored"] == 0
    assert _status(fresh_db, "facts", fid) == "superseded"


def test_duplicate_of_current_fact_stays_superseded(fresh_db):
    _fact(fresh_db, "always pass an explicit kube context", hide=None)
    dup = _fact(fresh_db, "always pass an explicit kube context flag")
    other_type = _fact(fresh_db, "always pass an explicit kube context", type_="warning")
    counts = restore.run(fresh_db, WS)
    assert counts["facts_duplicate"] == 1 and counts["facts_restored"] == 1
    assert _status(fresh_db, "facts", dup) == "superseded"
    assert _status(fresh_db, "facts", other_type) == "current"  # another type is another fact


def test_newest_of_similar_superseded_facts_wins(fresh_db):
    older = _fact(fresh_db, "the deploy runs every night at two")
    newer = _fact(fresh_db, "the deploy runs every night at three")
    counts = restore.run(fresh_db, WS)
    assert (counts["facts_restored"], counts["facts_duplicate"]) == (1, 1)
    assert _status(fresh_db, "facts", newer) == "current"
    assert _status(fresh_db, "facts", older) == "superseded"


def test_node_restored_with_its_fact(fresh_db):
    nid = _node(fresh_db, "eks-main")
    fid = _fact(fresh_db, "runs version 1.33", node=nid)
    counts = restore.run(fresh_db, WS)
    assert (counts["nodes_restored"], counts["facts_restored"]) == (1, 1)
    assert _status(fresh_db, "nodes", nid) == "current"
    assert _status(fresh_db, "facts", fid) == "current"


def test_node_with_current_namesake_maps_and_fact_moves(fresh_db):
    old = _node(fresh_db, "EKS-Main")
    twin = _node(fresh_db, "the main cluster", hide=False, aliases=["eks-main"])
    moved = _fact(fresh_db, "runs version 1.33", node=old)
    _fact(fresh_db, "nodes are spot instances", node=twin, hide=None)
    dup = _fact(fresh_db, "nodes are spot instance", node=old)
    counts = restore.run(fresh_db, WS)
    assert (counts["nodes_mapped"], counts["nodes_restored"]) == (1, 0)
    assert (counts["facts_restored"], counts["facts_duplicate"]) == (1, 1)
    assert _status(fresh_db, "nodes", old) == "superseded"
    with fresh_db.admin_tx() as conn:
        row = conn.execute(
            "SELECT node_id::text, status FROM ws_alpha.facts WHERE id = %s", (moved,)
        ).fetchone()
    assert row == (twin, "current")
    assert _status(fresh_db, "facts", dup) == "superseded"


def test_fact_on_node_hidden_for_another_reason_is_an_orphan(fresh_db):
    nid = _node(fresh_db, "decommissioned", hide=False)
    fid = _fact(fresh_db, "was the build host", node=nid)
    with fresh_db.admin_tx() as conn:
        graph.retract(conn, WS, "node", nid)
    counts = restore.run(fresh_db, WS)
    assert counts["facts_orphan"] == 1 and counts["facts_restored"] == 0
    assert _status(fresh_db, "facts", fid) == "superseded"


def test_local_concept_without_current_general_concept_stays_hidden(fresh_db):
    with fresh_db.publisher_tx() as conn:
        live = graph.add_node(conn, "general", "concept", "tool", "helm")
        gone = graph.add_node(conn, "general", "concept", "tool", "kustomize")
    with fresh_db.admin_tx() as conn:
        ok = graph.add_node(conn, WS, "local_concept", "tool", "our helm", refines=live)
        orphan = graph.add_node(conn, WS, "local_concept", "tool", "our kustomize", refines=gone)
        for nid in (ok, orphan):
            graph.set_status(conn, WS, "node", nid, "superseded", "reconcile")
    with fresh_db.publisher_tx() as conn:
        graph.retract(conn, "general", "node", gone)
    fid = _fact(fresh_db, "kustomize overlays live in deploy", node=orphan)
    counts = restore.run(fresh_db, WS)
    assert (counts["nodes_restored"], counts["nodes_orphan"], counts["facts_orphan"]) == (1, 1, 1)
    assert _status(fresh_db, "nodes", ok) == "current"
    assert _status(fresh_db, "nodes", orphan) == "superseded"
    assert _status(fresh_db, "facts", fid) == "superseded"


def test_edge_restored_only_with_current_endpoints(fresh_db):
    a, b = _node(fresh_db, "svc-a"), _node(fresh_db, "svc-b")
    c = _node(fresh_db, "svc-c", hide=False)
    with fresh_db.admin_tx() as conn:
        good = graph.add_edge(conn, WS, a, b, "uses")
        bad = graph.add_edge(conn, WS, a, c, "uses")
        graph.set_status(conn, WS, "edge", good, "superseded", "reconcile")
        graph.set_status(conn, WS, "edge", bad, "superseded", "reconcile")
        graph.retract(conn, WS, "node", c)
    counts = restore.run(fresh_db, WS)
    assert (counts["edges_restored"], counts["edges_skipped"]) == (1, 1)
    assert _status(fresh_db, "edges", good) == "current"
    assert _status(fresh_db, "edges", bad) == "superseded"


def test_dry_run_writes_nothing_then_second_real_run_is_a_noop(fresh_db):
    nid = _node(fresh_db, "eks-dry")
    fid = _fact(fresh_db, "a fact to bring back", node=nid)
    _fact(fresh_db, "an open thread", type_="open_thread")
    dry = restore.run(fresh_db, WS, dry_run=True)
    assert _status(fresh_db, "facts", fid) == "superseded"
    assert _status(fresh_db, "nodes", nid) == "superseded"
    with fresh_db.admin_tx() as conn:
        assert conn.execute(
            "SELECT count(*) FROM control.audit WHERE action = 'restore'"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM control.jobs WHERE key = 'restore:alpha'"
        ).fetchone() == (0,)
    real = restore.run(fresh_db, WS)
    assert real == dry and real["facts_restored"] == 1
    again = restore.run(fresh_db, WS)
    assert again == {**dict.fromkeys(restore.COUNTS, 0), "facts_open_thread": 1}
    with fresh_db.admin_tx() as conn:
        assert conn.execute(
            "SELECT kind, workspace FROM control.jobs WHERE key = 'restore:alpha'"
        ).fetchall() == [("embed", "alpha")]
        audit = conn.execute(
            "SELECT detail FROM control.audit WHERE action = 'restore' ORDER BY id LIMIT 1"
        ).fetchone()[0]
    assert audit == {"namespace": "alpha", **real}


def test_general_fact_restored_by_publisher(fresh_db):
    fid = _fact(fresh_db, "rotate tokens on a schedule", ns="general")
    gone = _fact(fresh_db, "rotate tokens weekly", ns="general", hide="superseded")
    counts = restore.run(fresh_db, "general")
    assert counts["facts_restored"] == 1
    assert _status(fresh_db, "facts", fid, "general") == "current"
    assert _status(fresh_db, "facts", gone, "general") == "superseded"


def test_other_workspace_is_untouched(fresh_db):
    with fresh_db.admin_tx() as conn:
        other = graph.add_fact(conn, "beta", "lesson", "beta stays hidden")
        graph.set_status(conn, "beta", "fact", other, "superseded", "reconcile")
    restore.run(fresh_db, WS)
    with fresh_db.admin_tx() as conn:
        assert conn.execute(
            "SELECT status FROM ws_beta.facts WHERE id = %s", (other,)
        ).fetchone() == ("superseded",)


def test_cli_prints_counts_only(fresh, fresh_db, capsys):
    from treant import cli

    _fact(fresh_db, "words that must not be printed")
    capsys.readouterr()
    assert cli.main(["restore", "alpha", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "facts_restored=1" in out and "(dry run)" in out and "words" not in out
    assert cli.main(["restore", "alpha"]) == 0
    assert "facts_restored=1" in capsys.readouterr().out
    assert cli.main(["restore", "general", "--dry-run"]) == 0
    assert cli.main(["restore", "nosuchws"]) == 1
    assert cli.main(["restore", "Bad Name"]) == 1


def test_edge_of_mapped_node_moves_to_the_current_namesake(fresh_db):
    old = _node(fresh_db, "svc-old")
    twin = _node(fresh_db, "SVC-old", hide=False)
    other = _node(fresh_db, "svc-other")
    with fresh_db.admin_tx() as conn:
        moved = graph.add_edge(conn, WS, old, other, "uses")
        dup = graph.add_edge(conn, WS, other, old, "depends_on")
        loop = graph.add_edge(conn, WS, old, twin, "part_of")
        for eid in (moved, dup, loop):
            graph.set_status(conn, WS, "edge", eid, "superseded", "reconcile")
    with fresh_db.admin_tx() as conn:
        graph.add_edge(conn, WS, other, twin, "depends_on")  # dangling until other is restored
    counts = restore.run(fresh_db, WS)
    assert (counts["nodes_mapped"], counts["edges_restored"], counts["edges_skipped"]) == (1, 1, 2)
    with fresh_db.admin_tx() as conn:
        row = conn.execute(
            "SELECT src::text, dst::text, status FROM ws_alpha.edges WHERE id = %s", (moved,)
        ).fetchone()
    assert row == (twin, other, "current")
    assert _status(fresh_db, "edges", dup) == "superseded"  # the namesake already has it
    assert _status(fresh_db, "edges", loop) == "superseded"  # would point at itself
    assert _reasons(fresh_db, "edge", moved)[-2:] == ["restore", "restore"]


def test_fact_merged_as_a_duplicate_is_not_restored(fresh_db):
    from treant import owner

    keep = _fact(fresh_db, "pin the chart version", hide=None)
    # Once hidden by reconcile, then restored (worded apart enough not to count as a duplicate).
    merged = _fact(fresh_db, "chart versions are fixed in the helmfile")
    assert restore.run(fresh_db, WS)["facts_restored"] == 1
    owner.merge_facts(fresh_db, "cli", WS, keep, [merged])
    hidden = _fact(fresh_db, "a fact reconcile hid")
    counts = restore.run(fresh_db, WS)
    assert counts["facts_restored"] == 1  # only the reconcile one
    assert _status(fresh_db, "facts", hidden) == "current"
    assert _status(fresh_db, "facts", merged) == "superseded"
    assert _reasons(fresh_db, "fact", merged) == ["reconcile", "restore", "merge"]
