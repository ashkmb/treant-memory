"""The client against the real server app (build_app under uvicorn)."""

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fakes import FakeEmbedder

from treant import brief, graph, owner
from treant.server import build_app
from treant.tokens import TokenContext, create_token

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "client" / "src"))
sys.path.insert(0, str(_ROOT / "guard" / "src"))  # treant_client's one dependency, stdlib only

from treant_client import stale as client_stale
from treant_client.config import Config
from treant_client.mcp_http import TreantError, TreantSession


@pytest.fixture
def live_url(db):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    cfg = uvicorn.Config(
        build_app(db, FakeEmbedder()), host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(cfg)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_brief_and_save_session_revisions(db, live_url):
    raw = create_token(db, "alpha", "client-proto", {"create", "read"})
    with TreantSession(live_url, raw, 10) as s:
        assert "brief" in s.call_tool("brief", {})
        args = {"client_session_id": "proto-1", "sections": {"journal": "client protocol test"}}
        assert s.call_tool("save_session", args)["revision"] == 1
        assert s.call_tool("save_session", args)["revision"] == 2


def test_bad_token_is_http_401(live_url):
    with pytest.raises(TreantError) as e, TreantSession(live_url, "tok-test", 5):
        pass
    assert e.value.category == "http_401"


def _git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)
    return r.stdout.strip()


def test_check_stale_end_to_end(db, live_url, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)  # no owner signing or hooks
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t@example.invalid")
    monkeypatch.setenv("TREANT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("TREANT_CLIENT_LOG_DIR", str(tmp_path / "logs"))

    raw = create_token(db, "alpha", "stale-e2e", {"create", "read", "update"})
    with db.ws_tx("alpha") as conn:
        node = graph.add_node(conn, "alpha", "instance", "repo", "e2e-demo")
        old = graph.add_fact(conn, "alpha", "fact", "Entry point is `old_name.py`.", node_id=node)
        kept = graph.add_fact(conn, "alpha", "fact", "Helpers are in `keep.py`.", node_id=node)

    acme = tmp_path / "Projects" / "acme"
    repo = acme / "e2e-demo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / "old_name.py").write_text("x = 1\n")
    (repo / "keep.py").write_text("y = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "initial")
    _git(repo, "mv", "old_name.py", "new_name.py")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "rename old_name to new_name")
    short, head = _git(repo, "rev-parse", "--short", "HEAD"), _git(repo, "rev-parse", "HEAD")
    cfg = Config(url=live_url, folders={str(acme): "alpha"})

    res = client_stale.run(
        str(repo),
        cfg,
        dry_run=False,
        use_memo=False,
        token_getter=lambda ws: raw,
        session_factory=TreantSession,
    )
    assert res.status == "ok" and res.flagged == 1 and res.checked == 2

    def flags(fact_id):
        with db.ws_tx("alpha") as conn:
            return conn.execute(
                "SELECT id, commit, missing, resolved_at FROM ws_alpha.stale_flags "
                "WHERE fact_id = %s ORDER BY id",
                (fact_id,),
            ).fetchall()

    [(flag_id, commit, missing, resolved_at)] = flags(old)
    assert resolved_at is None and commit == head
    assert missing == [
        {"token": "old_name.py", "commit": short, "subject": "rename old_name to new_name"}
    ]
    assert flags(kept) == []

    ctx = TokenContext("e2e0e2e0e2e0", "alpha", frozenset({"create", "read", "update"}))
    out = brief.brief(db, ctx, "e2e-demo")
    assert "Entry point is `old_name.py`. (in doubt: old_name.py not found at" in out
    owner.keep_stale(db, "cli", "alpha", flag_id)
    out = brief.brief(db, ctx, "e2e-demo")
    assert "Entry point is `old_name.py`." in out
    assert "Entry point is `old_name.py`. (in doubt:" not in out

    read_only = create_token(db, "alpha", "stale-e2e-ro", {"create", "read"})
    res = client_stale.run(
        str(repo),
        cfg,
        dry_run=False,
        use_memo=False,
        token_getter=lambda ws: read_only,
        session_factory=TreantSession,
    )
    assert res.status == "skipped_forbidden" and res.flagged == 0
    assert len(flags(old)) == 1  # no new row
