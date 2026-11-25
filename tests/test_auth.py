import pytest
from fakes import FakeEmbedder
from starlette.testclient import TestClient

from treant import owner
from treant.server import build_app
from treant.tokens import create_token


@pytest.fixture
def client(db):
    with TestClient(build_app(db, FakeEmbedder())) as c:
        yield c


def test_bad_tokens_get_plain_401(db, client):
    raw = create_token(db, "alpha", "gone", {"read"})
    owner.revoke_token(db, "cli", raw.split("_")[1])
    headers = [
        {},
        {"Authorization": "Bearer nope"},
        {"Authorization": f"Bearer {raw}"},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer  double-space"},
        {"Authorization": "Bearer"},
    ]
    for h in headers:
        r = client.post("/mcp", headers=h, json={})
        assert r.status_code == 401, h
        assert "www-authenticate" not in r.headers
        assert r.text == "unauthorized"
        assert r.headers["content-type"].startswith("text/plain")


def test_healthz_open(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.text == "ok"


def test_scheme_is_case_insensitive(db, client):
    raw = create_token(db, "alpha", "case", {"read"})
    for scheme in ("bearer", "BEARER", "Bearer"):
        r = client.post("/mcp", headers={"Authorization": f"{scheme} {raw}"}, json={})
        assert r.status_code != 401, scheme


def test_verify_error_fails_closed(db, client, monkeypatch, caplog):
    from treant import auth

    raw = create_token(db, "alpha", "boom", {"read"})

    def boom(*a):
        raise RuntimeError("db detail " + raw)

    monkeypatch.setattr(auth, "verify_token", boom)
    r = client.post("/mcp", headers={"Authorization": f"Bearer {raw}"}, json={})
    assert r.status_code == 401 and r.text == "unauthorized"
    assert raw not in caplog.text and "db detail" not in caplog.text
    assert "RuntimeError" in caplog.text
