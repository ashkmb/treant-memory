"""The client against the real server app (build_app under uvicorn)."""

import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fakes import FakeEmbedder

from treant.server import build_app
from treant.tokens import create_token

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "client" / "src"))

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
