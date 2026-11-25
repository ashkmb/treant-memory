"""Fact duplicates: grouping by embedding similarity, the owner merge, the CLI counts."""

import math
import uuid

import pytest
from psycopg.types.json import Jsonb

from treant import browse, cli, graph, owner
from treant.errors import InvalidInput

DIMS = 768


def _schema(ns):
    return "general" if ns == "general" else f"ws_{ns}"


def vec(angle: float, plane: int = 0) -> str:
    """A unit vector at ``angle`` in the plane of dimensions ``plane`` and ``plane + 1``: two
    vectors of the same plane have cosine similarity ``cos(a - b)``, of different planes 0."""
    v = [0.0] * DIMS
    v[plane], v[plane + 1] = math.cos(angle), math.sin(angle)
    return "[" + ",".join(repr(x) for x in v) + "]"


def add_fact(db, ns, text, angle=None, type_="lesson", node=None, revision=None, plane=0):
    tx = db.publisher_tx if ns == "general" else (lambda: db.ws_tx(ns))
    with tx() as c:
        fid = graph.add_fact(c, ns, type_, text, node_id=node, revision_id=revision)
    if angle is not None:
        with db.admin_tx() as c:
            c.execute(
                f"INSERT INTO {_schema(ns)}.embeddings (item_kind, item_id, vec) "
                "VALUES ('fact', %s, %s::vector)",
                (fid, vec(angle, plane)),
            )
    return fid


def _revision(db, ws="alpha"):
    with db.admin_tx() as c:
        sid = c.execute(
            f"INSERT INTO ws_{ws}.sessions (token_id, client_session_id) VALUES ('t', %s) "
            "RETURNING id::text",
            (uuid.uuid4().hex,),
        ).fetchone()[0]
        return c.execute(
            f"INSERT INTO ws_{ws}.revisions (session_id, n, sections) VALUES (%s, 1, %s) "
            "RETURNING id::text",
            (sid, Jsonb({})),
        ).fetchone()[0]


def _support(db, fid, revision, ws="alpha"):
    with db.ws_tx(ws) as c:
        graph.add_support(c, ws, "fact", fid, revision)


def _supported_by(db, fid, ws="alpha"):
    with db.admin_tx() as c:
        return {
            r[0]
            for r in c.execute(
                f"SELECT revision_id::text FROM ws_{ws}.support "
                "WHERE item_kind = 'fact' AND item_id = %s",
                (fid,),
            )
        }


def _row(db, ns, fid):
    with db.admin_tx() as c:
        return c.execute(
            f"SELECT text, status, version FROM {_schema(ns)}.facts WHERE id = %s", (fid,)
        ).fetchone()


def _reasons(db, ns, fid):
    with db.admin_tx() as c:
        return [
            r[0]
            for r in c.execute(
                f"SELECT reason FROM {_schema(ns)}.versions WHERE item_kind = 'fact' "
                "AND item_id = %s ORDER BY version",
                (fid,),
            )
        ]


def _audit(db, action):
    with db.admin_tx() as c:
        return c.execute(
            "SELECT actor, detail FROM control.audit WHERE action = %s ORDER BY id", (action,)
        ).fetchall()


def _ids(groups):
    return [{f["id"] for f in g["facts"]} for g in groups]


# --- grouping ---------------------------------------------------------------------------------


def test_transitive_pairs_form_one_group_largest_first(fresh_db):
    a = add_fact(fresh_db, "alpha", "deploy with helmfile", 0.0)
    b = add_fact(fresh_db, "alpha", "deploy using helmfile", 0.4)  # cos 0.92 with a and c
    c = add_fact(fresh_db, "alpha", "helmfile does the deploys", 0.8)  # cos 0.70 with a
    d = add_fact(fresh_db, "alpha", "the cluster runs on spot", 0.0, plane=4)
    e = add_fact(fresh_db, "alpha", "cluster nodes are spot", 0.2, plane=4)
    add_fact(fresh_db, "alpha", "unrelated", 0.0, plane=8)
    groups = browse.duplicates(fresh_db, "alpha")
    assert _ids(groups) == [{a, b, c}, {d, e}]
    assert groups[0]["type"] == "lesson" and len(groups[0]["key"]) >= 12
    assert groups[0]["key"] != groups[1]["key"]


def test_group_lists_facts_newest_first_with_node_name(fresh_db):
    with fresh_db.ws_tx("alpha") as c:
        nid = graph.add_node(c, "alpha", "instance", "cluster", "eks-main")
    old = add_fact(fresh_db, "alpha", "old wording", 0.0, node=nid)
    new = add_fact(fresh_db, "alpha", "new wording", 0.1)
    with fresh_db.admin_tx() as c:
        c.execute(
            "UPDATE ws_alpha.facts SET created_at = now() - interval '2 days' WHERE id = %s",
            (old,),
        )
    (group,) = browse.duplicates(fresh_db, "alpha")
    first, second = group["facts"]
    assert (first["id"], second["id"]) == (new, old)
    assert first["text"] == "new wording" and first["node_name"] is None
    assert second["node_name"] == "eks-main" and second["node_id"] == nid
    assert second["type"] == "lesson" and second["created_at"] < first["created_at"]


def test_different_types_never_group(fresh_db):
    add_fact(fresh_db, "alpha", "pin chart versions", 0.0, type_="lesson")
    add_fact(fresh_db, "alpha", "pin chart versions", 0.0, type_="warning")
    assert browse.duplicates(fresh_db, "alpha") == []


def test_superseded_and_retracted_facts_are_ignored(fresh_db):
    keep = add_fact(fresh_db, "alpha", "a", 0.0)
    gone = add_fact(fresh_db, "alpha", "a again", 0.0)
    pulled = add_fact(fresh_db, "alpha", "a once more", 0.0)
    with fresh_db.admin_tx() as c:
        graph.set_status(c, "alpha", "fact", gone, "superseded", "superseded")
        graph.retract(c, "alpha", "fact", pulled)
    assert browse.duplicates(fresh_db, "alpha") == []
    assert keep  # the one current fact has no partner


def test_threshold_is_respected(fresh_db):
    a = add_fact(fresh_db, "alpha", "x", 0.0)
    b = add_fact(fresh_db, "alpha", "y", 0.45)  # cos 0.900
    assert _ids(browse.duplicates(fresh_db, "alpha")) == [{a, b}]  # default 0.88
    assert _ids(browse.duplicates(fresh_db, "alpha", threshold=0.89)) == [{a, b}]
    assert browse.duplicates(fresh_db, "alpha", threshold=0.91) == []
    for bad in (0, 1.5, -1, "x", float("nan")):
        with pytest.raises(InvalidInput, match="threshold"):
            browse.duplicates(fresh_db, "alpha", threshold=bad)


def test_facts_without_vectors_and_other_workspaces_do_not_group(fresh_db):
    add_fact(fresh_db, "alpha", "same", 0.0)
    add_fact(fresh_db, "alpha", "same, no vector yet")
    add_fact(fresh_db, "beta", "same", 0.0)
    assert browse.duplicates(fresh_db, "alpha") == []


def test_general_namespace_and_unknown_namespaces(fresh_db):
    a = add_fact(fresh_db, "general", "use a lock file", 0.0)
    b = add_fact(fresh_db, "general", "commit the lock file", 0.1)
    assert _ids(browse.duplicates(fresh_db, "general")) == [{a, b}]
    with pytest.raises(InvalidInput, match="unknown namespace"):
        browse.duplicates(fresh_db, "Bad Name")
    with pytest.raises(owner.Refused, match="workspace not available"):
        browse.duplicates(fresh_db, "nosuch")


# --- merge --------------------------------------------------------------------------------------


def test_merge_supersedes_drops_copies_support_and_audits_without_text(fresh_db):
    r1, r2, r3 = _revision(fresh_db), _revision(fresh_db), _revision(fresh_db)
    keep = add_fact(fresh_db, "alpha", "keep this wording", 0.0, revision=r1)
    d1 = add_fact(fresh_db, "alpha", "drop this one", 0.1, revision=r2)
    d2 = add_fact(fresh_db, "alpha", "and this one", 0.2, revision=r3)
    _support(fresh_db, keep, r1)
    _support(fresh_db, d1, r2)
    _support(fresh_db, d2, r3)
    _support(fresh_db, d2, r1)  # already supports keep: no duplicate row
    owner.merge_facts(fresh_db, "dashboard:a", "alpha", keep, [d1, d2])
    assert _row(fresh_db, "alpha", keep) == ("keep this wording", "current", 1)
    for d in (d1, d2):
        assert _row(fresh_db, "alpha", d)[1:] == ("superseded", 2)
        assert _reasons(fresh_db, "alpha", d) == ["merge"]
    assert _supported_by(fresh_db, keep) == {r1, r2, r3}
    assert _supported_by(fresh_db, d1) == {r2}  # copied, the dropped fact keeps its history
    (actor, detail), *_ = _audit(fresh_db, "fact.merge")
    assert actor == "dashboard:a"
    assert detail == {"ns": "alpha", "keep": keep, "dropped": sorted([d1, d2]), "edited": False}
    assert browse.duplicates(fresh_db, "alpha") == []


def test_merge_with_text_edits_kept_fact_first(fresh_db):
    keep = add_fact(fresh_db, "alpha", "old wording", 0.0)
    drop = add_fact(fresh_db, "alpha", "other wording", 0.1)
    owner.merge_facts(fresh_db, "cli", "alpha", keep, [drop], text="best wording")
    assert _row(fresh_db, "alpha", keep) == ("best wording", "current", 2)
    assert _reasons(fresh_db, "alpha", keep) == ["update"]
    with fresh_db.admin_tx() as c:
        assert c.execute(
            "SELECT key FROM control.jobs WHERE kind = 'embed' AND workspace = 'alpha'"
        ).fetchall() == [(f"fact:{keep}:2",)]
        assert (  # the stale vector is gone until the worker re-embeds
            c.execute(
                "SELECT count(*) FROM ws_alpha.embeddings WHERE item_id = %s", (keep,)
            ).fetchone()[0]
            == 0
        )
    detail = _audit(fresh_db, "fact.merge")[-1][1]
    assert detail["edited"] is True and "best wording" not in str(detail)


def test_merge_with_unchanged_text_is_not_an_edit(fresh_db):
    keep = add_fact(fresh_db, "alpha", "same wording", 0.0)
    drop = add_fact(fresh_db, "alpha", "other", 0.1)
    owner.merge_facts(fresh_db, "cli", "alpha", keep, [drop], text="same wording")
    assert _row(fresh_db, "alpha", keep) == ("same wording", "current", 1)
    assert _audit(fresh_db, "fact.merge")[-1][1]["edited"] is False


def test_merge_text_is_scrubbed_and_rolls_back(fresh_db):
    keep = add_fact(fresh_db, "alpha", "k", 0.0)
    drop = add_fact(fresh_db, "alpha", "d", 0.1)
    many = " ".join(f"10.0.0.{i}" for i in range(1, 15))
    with pytest.raises(InvalidInput, match="text"):
        owner.merge_facts(fresh_db, "cli", "alpha", keep, [drop], text=f"hosts {many}")
    assert _row(fresh_db, "alpha", drop)[1] == "current"


def test_merge_general_facts(fresh_db):
    keep = add_fact(fresh_db, "general", "use a lock file", 0.0)
    drop = add_fact(fresh_db, "general", "commit the lock file", 0.1)
    owner.merge_facts(fresh_db, "cli", "general", keep, [drop])
    assert _row(fresh_db, "general", drop)[1] == "superseded"
    assert _reasons(fresh_db, "general", drop) == ["merge"]
    assert _audit(fresh_db, "fact.merge")[-1] == (
        "cli",
        {"ns": "general", "keep": keep, "dropped": [drop], "edited": False},
    )


def test_merge_general_text_runs_gate_rules(fresh_db, queued_item):
    keep = add_fact(fresh_db, "general", "clean", 0.0)
    drop = add_fact(fresh_db, "general", "clean too", 0.1)
    with pytest.raises(owner.Flagged):
        owner.merge_facts(fresh_db, "cli", "general", keep, [drop], text="use zorblax now")
    assert _row(fresh_db, "general", drop)[1] == "current"
    assert _audit(fresh_db, "fact.merge") == []


def test_merge_refusals_change_nothing(fresh_db):
    keep = add_fact(fresh_db, "alpha", "k", 0.0)
    drop = add_fact(fresh_db, "alpha", "d", 0.1)
    warning = add_fact(fresh_db, "alpha", "w", 0.1, type_="warning")
    other_ws = add_fact(fresh_db, "beta", "b", 0.1)
    stale = add_fact(fresh_db, "alpha", "s", 0.1)
    with fresh_db.admin_tx() as c:
        graph.set_status(c, "alpha", "fact", stale, "superseded", "superseded")
    unknown = str(uuid.uuid4())
    cases = [
        ([warning], InvalidInput, "mixed fact types"),
        ([unknown], owner.Refused, "fact not found"),
        ([other_ws], owner.Refused, "fact not found"),
        ([drop, keep], InvalidInput, "keep is also dropped"),
        ([], InvalidInput, "nothing to merge"),
        ([stale], owner.Refused, "fact not current"),
        (["not-an-id"], InvalidInput, "invalid id"),
    ]
    for drops, exc, msg in cases:
        with pytest.raises(exc, match=msg):
            owner.merge_facts(fresh_db, "cli", "alpha", keep, drops)
    with pytest.raises(owner.Refused, match="fact not found"):
        owner.merge_facts(fresh_db, "cli", "alpha", other_ws, [drop])
    with pytest.raises(InvalidInput, match="unknown namespace"):
        owner.merge_facts(fresh_db, "cli", "Bad Name", keep, [drop])
    with pytest.raises(owner.Refused, match="workspace not available"):
        owner.merge_facts(fresh_db, "cli", "nosuch", keep, [drop])
    assert _row(fresh_db, "alpha", drop)[1:] == ("current", 1)
    assert _row(fresh_db, "alpha", warning)[1:] == ("current", 1)
    assert _audit(fresh_db, "fact.merge") == []


def test_merge_ignores_repeated_drop_ids(fresh_db):
    keep = add_fact(fresh_db, "alpha", "k", 0.0)
    drop = add_fact(fresh_db, "alpha", "d", 0.1)
    owner.merge_facts(fresh_db, "cli", "alpha", keep, [drop, drop.upper()])
    assert _reasons(fresh_db, "alpha", drop) == ["merge"]


# --- CLI ----------------------------------------------------------------------------------------


def test_cli_duplicates_prints_counts_only(fresh_db, capsys):
    add_fact(fresh_db, "alpha", "secret wording one", 0.0)
    add_fact(fresh_db, "alpha", "secret wording two", 0.4)
    add_fact(fresh_db, "alpha", "secret wording three", 0.8)
    add_fact(fresh_db, "alpha", "secret other one", 0.0, plane=4)
    add_fact(fresh_db, "alpha", "secret other two", 0.45, plane=4)  # cos 0.900
    assert cli.main(["duplicates", "alpha"]) == 0
    out = capsys.readouterr().out
    assert out.strip() == "groups=2 facts=5 sizes=3,2"
    assert "secret" not in out
    assert cli.main(["duplicates", "alpha", "--threshold", "0.91"]) == 0
    assert capsys.readouterr().out.strip() == "groups=1 facts=3 sizes=3"
    assert cli.main(["duplicates", "general"]) == 0
    assert capsys.readouterr().out.strip() == "groups=0 facts=0 sizes="


def test_cli_duplicates_refusals(fresh_db, capsys):
    assert cli.main(["duplicates", "nosuch"]) == 1
    assert "error: workspace not available" in capsys.readouterr().err
    assert cli.main(["duplicates", "alpha", "--threshold", "2"]) == 1
    assert "error: threshold" in capsys.readouterr().err
