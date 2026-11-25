import pytest
from fakes import FakeEmbedder

from treant import brief, jobs, search, sessions, worker
from treant.errors import InvalidInput
from treant.sessions import SECTIONS, save_session


def test_second_save_is_revision_two(db, ctx_a):
    r1 = save_session(db, ctx_a, "s-1", {"journal": "did x"})
    r2 = save_session(db, ctx_a, "s-1", {"journal": "did x and y"})
    assert (r1.session_id, r1.revision, r2.revision) == (r2.session_id, 1, 2)


def test_same_session_id_other_token_is_new_session(db, ctx_a, ctx_a2):
    first = save_session(db, ctx_a, "s-1", {})
    second = save_session(db, ctx_a2, "s-1", {})
    assert first.session_id != second.session_id


def test_unknown_section_kept_in_journal(db, ctx_a):
    res = save_session(db, ctx_a, "s-mood", {"journal": "j", "mood": "x"})
    with db.ws_tx("alpha") as conn:
        text = conn.execute(
            "SELECT j.text FROM ws_alpha.journal_entries j "
            "JOIN ws_alpha.revisions r ON r.id = j.revision_id "
            "WHERE r.session_id = %s AND r.n = %s",
            (res.session_id, res.revision),
        ).fetchone()[0]
    assert "## mood\nx" in text
    assert text.startswith("j")
    assert len(SECTIONS) == 11


def test_save_and_job_are_one_transaction(db, ctx_a, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("enqueue failed")

    monkeypatch.setattr(jobs, "enqueue", boom)
    with pytest.raises(RuntimeError):
        save_session(db, ctx_a, "s-atomic", {"journal": "x"})
    with db.ws_tx("alpha") as conn:
        n = conn.execute(
            "SELECT count(*) FROM ws_alpha.sessions WHERE client_session_id = %s", ("s-atomic",)
        ).fetchone()[0]
        revs = conn.execute(
            "SELECT count(*) FROM ws_alpha.revisions r JOIN ws_alpha.sessions s "
            "ON s.id = r.session_id WHERE s.client_session_id = %s",
            ("s-atomic",),
        ).fetchone()[0]
    assert (n, revs) == (0, 0)


def test_save_enqueues_embed_job(db, ctx_a):
    res = save_session(db, ctx_a, "s-job", {"journal": "x"})
    with db.ws_tx("alpha") as conn:
        rev_id = str(
            conn.execute(
                "SELECT id FROM ws_alpha.revisions WHERE session_id = %s AND n = %s",
                (res.session_id, res.revision),
            ).fetchone()[0]
        )
    with db.control_tx() as conn:
        row = conn.execute(
            "SELECT kind, payload FROM control.jobs WHERE workspace = 'alpha' AND key = %s "
            "AND kind = 'embed'",
            (rev_id,),
        ).fetchone()
    assert row == ("embed", {"revision_id": rev_id})


def test_rejects_oversized_and_empty_session_id(db, ctx_a):
    with pytest.raises(InvalidInput):
        save_session(db, ctx_a, "", {})
    with pytest.raises(InvalidInput):
        save_session(db, ctx_a, "x" * 201, {})
    with pytest.raises(InvalidInput):
        save_session(db, ctx_a, "s", ["not", "a", "dict"])  # type: ignore[arg-type]
    with pytest.raises(InvalidInput):
        save_session(db, ctx_a, "s-big", {"journal": "y" * 200_001})
    with pytest.raises(InvalidInput):
        save_session(db, ctx_a, "s-big", {"journal": "y" * 100_000, "facts": ["z" * 100_001]})


AWS_KEY = "AKIA" + "ABCDEFGH12345678"


def _rev(db, res):
    with db.ws_tx("alpha") as conn:
        return conn.execute(
            "SELECT r.id, r.status, r.removed, r.sections, j.text FROM ws_alpha.revisions r "
            "JOIN ws_alpha.journal_entries j ON j.revision_id = r.id "
            "WHERE r.session_id = %s AND r.n = %s",
            (res.session_id, res.revision),
        ).fetchone()


def _jobs(db, rev_id):
    with db.control_tx() as conn:
        return sorted(
            r[0]
            for r in conn.execute(
                "SELECT kind FROM control.jobs WHERE workspace = 'alpha' AND key = %s",
                (str(rev_id),),
            )
        )


def test_save_scrubs_sections(db, ctx_a):
    res = save_session(
        db,
        ctx_a,
        "s-scrub",
        {
            "journal": f"used {AWS_KEY}",
            "facts": ["host 10.1.2.3", {"k": f"v {AWS_KEY}"}],
            "mood": AWS_KEY,
        },
    )
    rev_id, status, removed, sections, text = _rev(db, res)
    assert status == "stored" and removed == 4
    assert AWS_KEY not in str(sections) and AWS_KEY not in text
    assert sections["facts"] == ["host [removed]", {"k": "v [removed]"}]
    assert "[removed]" in text
    assert _jobs(db, rev_id) == ["distill", "embed"]


def test_save_scrubs_with_workspace_terms(db, ctx_a):
    with db.admin_tx() as conn:
        conn.execute(
            "UPDATE control.workspaces SET settings = %s WHERE name = 'alpha'",
            ('{"rules": {"terms": ["acme"]}}',),
        )
    try:
        res = save_session(db, ctx_a, "s-terms", {"journal": "git.acme-corp.io"})
        assert _rev(db, res)[4] == "[removed]"
    finally:
        with db.admin_tx() as conn:
            conn.execute("UPDATE control.workspaces SET settings = '{}' WHERE name = 'alpha'")


def test_save_unsure_quarantines(db, ctx_a):
    res = save_session(db, ctx_a, "s-unsure", {"journal": "plain text"}, unsure=True)
    rev_id, status, *_ = _rev(db, res)
    assert res.quarantined and status == "quarantined"
    assert _jobs(db, rev_id) == []


def test_save_not_confident_quarantines(db, ctx_a):
    noisy = " ".join(f"10.0.0.{i}" for i in range(13))
    res = save_session(db, ctx_a, "s-noisy", {"journal": noisy})
    rev_id, status, removed, _, text = _rev(db, res)
    assert res.quarantined and status == "quarantined" and removed == 13
    assert "10.0.0." not in text
    assert _jobs(db, rev_id) == []


def test_quarantined_revision_is_invisible(db, ctx_a):
    res = save_session(db, ctx_a, "s-hidden", {"journal": "zanzibar quokka migration"}, unsure=True)
    emb = FakeEmbedder()
    while worker.run_once(db, emb):
        pass
    hits = search.search(db, emb, ctx_a, "zanzibar quokka migration").hits
    assert not any("zanzibar" in str(h) for h in hits)
    assert "zanzibar" not in brief.brief(db, ctx_a, None)
    assert _jobs(db, _rev(db, res)[0]) == []


def test_release_enqueues_jobs(db, ctx_a):
    res = save_session(db, ctx_a, "s-rel", {"journal": "x"}, unsure=True)
    rev_id = _rev(db, res)[0]
    sessions.release(db, "alpha", str(rev_id), actor="cli")
    assert _rev(db, res)[1] == "stored"
    assert _jobs(db, rev_id) == ["distill", "embed"]
    with db.control_tx() as conn:
        detail = conn.execute(
            "SELECT detail FROM control.audit WHERE action = 'quarantine.release' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert detail == {"workspace": "alpha", "revision_id": str(rev_id)}
    with pytest.raises(InvalidInput):
        sessions.release(db, "alpha", str(rev_id), actor="cli")


def test_drop_marks_dropped(db, ctx_a):
    res = save_session(db, ctx_a, "s-drop", {"journal": "x"}, unsure=True)
    rev_id = _rev(db, res)[0]
    sessions.drop(db, "alpha", str(rev_id), actor="cli")
    assert _rev(db, res)[1] == "dropped"
    assert _jobs(db, rev_id) == []
    with db.control_tx() as conn:
        n = conn.execute(
            "SELECT count(*) FROM control.audit WHERE action = 'quarantine.drop'"
        ).fetchone()[0]
    assert n >= 1
    with pytest.raises(InvalidInput):
        sessions.drop(db, "alpha", str(rev_id), actor="cli")
    with pytest.raises(InvalidInput):
        sessions.drop(db, "alpha", "not-a-uuid", actor="cli")


def _set_status(db, rev_id, status):
    with db.admin_tx() as conn:
        conn.execute("UPDATE ws_alpha.revisions SET status = %s WHERE id = %s", (status, rev_id))


def test_quarantined_journal_not_in_keyword_search(db, ctx_a):
    word = "xylophonequarry"
    for status in ("quarantined", "dropped"):
        res = save_session(db, ctx_a, f"s-kw-{status}", {"journal": f"{word} {status}"})
        _set_status(db, _rev(db, res)[0], status)
    down = FakeEmbedder(down=True)
    assert not [h for h in search.search(db, down, ctx_a, word).hits if word in str(h)]


def test_quarantined_journal_not_in_vector_search(db, ctx_a):
    word = "marmalademountain"
    res = save_session(db, ctx_a, "s-vec", {"journal": f"{word} notes"})
    emb = FakeEmbedder()
    while worker.run_once(db, emb):
        pass
    rev_id = _rev(db, res)[0]
    assert [h for h in search.search(db, emb, ctx_a, word).hits if word in str(h)]
    for status in ("quarantined", "dropped"):
        _set_status(db, rev_id, status)
        assert not [h for h in search.search(db, emb, ctx_a, word).hits if word in str(h)]


def test_section_keys_are_scrubbed(db, ctx_a):
    key = "password=hunter2abc123"
    res = save_session(db, ctx_a, "s-key", {"journal": "j", key: "v"})
    _, _, _, sections, text = _rev(db, res)
    assert "hunter2abc123" not in str(sections) and "hunter2abc123" not in text
    assert sections["journal"] == "j"


def test_scrubbed_key_collision_keeps_both(db, ctx_a):
    res = save_session(
        db,
        ctx_a,
        "s-coll",
        {"password=hunter2abc123": "one", "password=other99zzz11": "two"},
    )
    sections = _rev(db, res)[3]
    assert sorted(sections.values()) == ["one", "two"]
    assert len(sections) == 2


def test_numeric_scalars_are_scrubbed(db, ctx_a):
    res = save_session(db, ctx_a, "s-num", {"journal": 123456789012, "facts": 123456789012, "n": 7})
    _, _, removed, sections, text = _rev(db, res)
    assert "123456789012" not in str(sections) and "123456789012" not in text
    assert sections["facts"] == "[removed]"
    assert sections["n"] == 7
    assert removed == 2


def test_drop_blanks_text_and_embeddings(db, ctx_a):
    res = save_session(db, ctx_a, "s-blank", {"journal": "leakyjournal words", "facts": "f"})
    emb = FakeEmbedder()
    while worker.run_once(db, emb):
        pass
    rev_id = _rev(db, res)[0]
    _set_status(db, rev_id, "quarantined")
    sessions.drop(db, "alpha", str(rev_id), actor="cli")
    _, status, _, sections, text = _rev(db, res)
    assert (status, sections, text) == ("dropped", {}, "")
    with db.ws_tx("alpha") as conn:
        n = conn.execute(
            "SELECT count(*) FROM ws_alpha.embeddings e JOIN ws_alpha.journal_entries j "
            "ON e.item_id = j.id WHERE j.revision_id = %s",
            (rev_id,),
        ).fetchone()[0]
    assert n == 0


def test_bad_workspace_name_is_invalid_input(db):
    for call in (
        lambda: sessions.quarantined_sections(db, "Bad!", "x"),
        lambda: sessions.release(db, "Bad!", "x", actor="cli"),
        lambda: sessions.drop(db, "Bad!", "x", actor="cli"),
    ):
        with pytest.raises(InvalidInput):
            call()
    try:
        sessions.quarantined_sections(db, "Bad!", "x")
    except InvalidInput as e:
        assert type(e) is not ValueError and str(e) == "workspace"


def test_unknown_key_length_counts_toward_the_cap(db, ctx_a):
    with pytest.raises(InvalidInput, match="too_large"):
        save_session(db, ctx_a, "s-keys", {"k" * 150_000: "v", "j" * 60_000: "w"})


@pytest.mark.parametrize(
    ("csid", "sections"),
    [
        ("s-nul\x00", {"journal": "x"}),
        ("s-nul", {"journal": "a\x00b"}),
        ("s-nul", {"odd\x00key": "x"}),
        ("s-nul", {"facts": ["ok", {"deep": "n\x00ul"}]}),
        ("s-nul", {"facts": {"k\x00": 1}}),
    ],
)
def test_nul_is_invalid_input(db, ctx_a, csid, sections):
    with pytest.raises(InvalidInput):
        save_session(db, ctx_a, csid, sections)
