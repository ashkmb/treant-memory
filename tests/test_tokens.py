import uuid

import pytest

from treant import migrate, owner, tokens


def test_token_roundtrip(db):
    raw = tokens.create_token(db, "alpha", "rt", {"read", "create"})
    assert raw.startswith("trt_")
    ctx = tokens.verify_token(db, raw)
    assert ctx is not None
    assert ctx.workspace == "alpha"
    assert ctx.actions == frozenset({"read", "create"})
    assert ctx.token_id == raw.split("_")[1]
    listed = [t for t in tokens.list_tokens(db, "alpha") if t["id"] == ctx.token_id]
    assert listed and listed[0]["actions"] == ["create", "read"]
    assert listed[0]["last_used_at"] is not None
    assert "secret_sha256" not in listed[0]


def test_verify_rejects_malformed(db):
    raw = tokens.create_token(db, "alpha", "m", {"read"})
    _, tid, secret = raw.split("_", 2)
    wrong = f"trt_{tid}_{'x' * 43}"
    for bad in [
        "",
        "trt_x",
        f"Bearer {raw}",
        wrong,
        f"trt_{tid}_{secret[:-1]}",
        f"trt_{tid.upper()}_{secret}",
        f"trt_{tid}_{secret}extra",
        "trt__",
        "x" * 500,
    ]:
        assert tokens.verify_token(db, bad) is None
    assert tokens.verify_token(db, raw) is not None


def test_revoked_token_fails(db):
    raw = tokens.create_token(db, "alpha", "rv", {"read"})
    ctx = tokens.verify_token(db, raw)
    assert ctx is not None
    owner.revoke_token(db, "cli", ctx.token_id)
    assert tokens.verify_token(db, raw) is None
    for gone in (ctx.token_id, "0123456789ab"):  # already revoked; no such token
        with pytest.raises(owner.Refused, match="token not found"):
            owner.revoke_token(db, "cli", gone)
    listed = [t for t in tokens.list_tokens(db, "alpha") if t["id"] == ctx.token_id]
    assert listed[0]["revoked_at"] is not None


def test_archived_workspace_token_fails(db, admin_dsn, prefix):
    name = "arch" + uuid.uuid4().hex[:8]
    migrate.create_workspace(admin_dsn, prefix, name)
    raw = tokens.create_token(db, name, "ar", {"read"})
    assert tokens.verify_token(db, raw) is not None
    with db.control_tx() as conn:
        conn.execute("UPDATE control.workspaces SET status = 'archived' WHERE name = %s", (name,))
    assert tokens.verify_token(db, raw) is None
    with pytest.raises(ValueError):
        tokens.create_token(db, name, "again", {"read"})
    with pytest.raises(ValueError):
        tokens.create_token(db, "nosuchws", "x", {"read"})


def test_hash_not_secret_stored(db):
    raw = tokens.create_token(db, "alpha", "h", {"read"})
    _, tid, secret = raw.split("_", 2)
    with db.control_tx() as conn:
        row = conn.execute(
            "SELECT secret_sha256 FROM control.tokens WHERE id = %s", (tid,)
        ).fetchone()
    assert row[0] != secret
    assert len(row[0]) == 64
    assert secret not in row[0]


def test_unknown_action_rejected(db):
    with pytest.raises(ValueError):
        tokens.create_token(db, "alpha", "bad", {"read", "admin"})
    with pytest.raises(ValueError):
        tokens.create_token(db, "alpha", "empty", set())


def test_hash_is_sha256_of_the_secret(db):
    import hashlib

    raw = tokens.create_token(db, "alpha", "h2", {"read"})
    _, tid, secret = raw.split("_", 2)
    with db.control_tx() as conn:
        stored = conn.execute(
            "SELECT secret_sha256 FROM control.tokens WHERE id = %s", (tid,)
        ).fetchone()[0]
    assert stored == hashlib.sha256(secret.encode()).hexdigest()


def test_unknown_id_still_compares_against_the_dummy_hash(db, monkeypatch):
    seen = []
    real = tokens.hmac.compare_digest

    def spy(a, b):
        seen.append(a)
        return real(a, b)

    monkeypatch.setattr(tokens.hmac, "compare_digest", spy)
    assert tokens.verify_token(db, f"trt_{'0' * 12}_{'x' * 43}") is None
    assert seen == [tokens._DUMMY_HASH]


def test_last_used_at_is_written_at_most_once_a_minute(db):
    raw = tokens.create_token(db, "alpha", "lu", {"read"})
    tid = raw.split("_")[1]

    def set_age(seconds):
        with db.control_tx() as conn:
            return conn.execute(
                "UPDATE control.tokens SET last_used_at = now() - make_interval(secs => %s) "
                "WHERE id = %s RETURNING last_used_at",
                (seconds, tid),
            ).fetchone()[0]

    def last_used():
        with db.control_tx() as conn:
            return conn.execute(
                "SELECT last_used_at FROM control.tokens WHERE id = %s", (tid,)
            ).fetchone()[0]

    recent = set_age(30)
    assert tokens.verify_token(db, raw) is not None
    assert last_used() == recent  # inside the 60 s window: not rewritten
    old = set_age(120)
    assert tokens.verify_token(db, raw) is not None
    assert last_used() > old
