import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from treant_client.mcp_http import RawSession, TreantError, TreantSession


class Fake:
    def __init__(self):
        self.requests = []  # (method, path, headers, body)
        self.mode = "ok"
        self.version = "2025-06-18"  # protocolVersion the server answers initialize with
        self.sessions = 0
        self.expired: set[str] = set()  # session ids answered with 404


@pytest.fixture
def fake():
    state = Fake()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n)) if n else None

        def _send(self, code, body=b"", ctype="application/json", headers=()):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_DELETE(self):
            state.requests.append(("DELETE", self.path, dict(self.headers), None))
            self._send(200)

        def do_POST(self):
            msg = self._body()
            state.requests.append(("POST", self.path, dict(self.headers), msg))
            if state.mode == "401":
                return self._send(401, b"unauthorized", "text/plain")
            if state.mode == "500":
                return self._send(500, b"SECRET-BODY", "text/plain")
            if state.mode == "slow":
                time.sleep(3)
            if state.mode == "redirect":
                return self._send(302, b"", headers=[("Location", "http://127.0.0.1:1/")])
            if self.headers.get("Mcp-Session-Id") in state.expired:
                return self._send(404, b"gone", "text/plain")
            m = msg.get("method")
            if m == "initialize":
                state.sessions += 1
                body = {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "result": {"protocolVersion": state.version},
                }
                other = {"jsonrpc": "2.0", "id": 999, "result": {}}
                sse = (
                    f"event: message\ndata: {json.dumps(other)}\n\n"
                    f"event: message\ndata: {json.dumps(body)}\n\n"
                )
                sid = f"sess-{state.sessions}"
                return self._send(200, sse.encode(), "text/event-stream", [("Mcp-Session-Id", sid)])
            if m == "notifications/initialized":
                return self._send(202)
            if m == "tools/call":
                name = msg["params"]["name"]
                if name == "boom":
                    payload = {"error": "nope"}
                else:
                    payload = {"echo": msg["params"]["arguments"]}
                if name == "text":
                    result = {"content": [{"type": "text", "text": json.dumps(payload)}]}
                else:
                    result = {"content": [], "structuredContent": payload}
                if name == "huge":
                    result = {"content": [], "structuredContent": {"x": "y" * 5000}}
                rid = 999 if name == "wrongid" else msg["id"]
                body = {"jsonrpc": "2.0", "id": rid, "result": result}
                return self._send(200, json.dumps(body).encode())
            self._send(400, b"bad")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state.url = f"http://127.0.0.1:{srv.server_address[1]}/mcp"
    yield state
    srv.shutdown()
    srv.server_close()


def test_sse_and_json_responses(fake):
    with TreantSession(fake.url, "tok-test", 2) as s:
        assert s.call_tool("echo", {"a": 1}) == {"echo": {"a": 1}}
        assert s.call_tool("text", {"b": 2}) == {"echo": {"b": 2}}


def test_session_header_sent_and_deleted(fake):
    with TreantSession(fake.url, "tok-test", 2) as s:
        s.call_tool("echo", {})
    posts = [r for r in fake.requests if r[0] == "POST"]
    assert "Mcp-Session-Id" not in posts[0][2]
    for r in posts[1:]:
        assert r[2]["Mcp-Session-Id"] == "sess-1"
        assert r[2]["Authorization"] == "Bearer tok-test"
        assert "text/event-stream" in r[2]["Accept"]
    init = posts[0][3]["params"]
    assert init["protocolVersion"] == "2025-06-18"
    assert init["clientInfo"]["name"] == "treant-client"
    assert fake.requests[-1][0] == "DELETE"
    assert fake.requests[-1][2]["Mcp-Session-Id"] == "sess-1"


def test_tool_error_raises(fake):
    with TreantSession(fake.url, "tok-test", 2) as s, pytest.raises(TreantError) as e:
        s.call_tool("boom", {})
    assert e.value.category == "tool_error"


def test_401_raises_http_401(fake):
    fake.mode = "401"
    with pytest.raises(TreantError) as e, TreantSession(fake.url, "tok-test", 2):
        pass
    assert e.value.category == "http_401"


def test_timeout_category(fake):
    fake.mode = "slow"
    with pytest.raises(TreantError) as e, TreantSession(fake.url, "tok-test", 0.5):
        pass
    assert e.value.category == "timeout"


def test_unreachable_category():
    with pytest.raises(TreantError) as e, TreantSession("http://127.0.0.1:1/mcp", "tok-test", 1):
        pass
    assert e.value.category == "unreachable"


def test_error_message_has_no_body_or_token(fake):
    fake.mode = "500"
    with pytest.raises(TreantError) as e, TreantSession(fake.url, "tok-test", 2):
        pass
    assert e.value.category == "http_500"
    assert "SECRET-BODY" not in str(e.value)
    assert "tok-test" not in str(e.value)


def test_redirect_not_followed(fake):
    fake.mode = "redirect"
    with pytest.raises(TreantError) as e, TreantSession(fake.url, "tok-test", 2):
        pass
    assert e.value.category == "http_302"


def test_raw_session_send_and_notification(fake):
    s = RawSession(fake.url, "tok-test", 2)
    r = s.send({"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {}})
    assert r["id"] == 7
    assert s.send({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert fake.requests[-1][2]["Mcp-Session-Id"] == "sess-1"


def test_protocol_version_header_is_the_negotiated_one(fake):
    fake.version = "2025-03-26"
    with TreantSession(fake.url, "tok-test", 2) as s:
        s.call_tool("echo", {})
    later = [r for r in fake.requests if r[0] == "POST"][1:]
    assert later and all(r[2]["Mcp-Protocol-Version"] == "2025-03-26" for r in later)


def test_json_response_with_another_id_is_a_protocol_error(fake):
    with TreantSession(fake.url, "tok-test", 2) as s, pytest.raises(TreantError) as e:
        s.call_tool("wrongid", {})
    assert e.value.category == "protocol"


def test_response_size_is_bounded(fake, monkeypatch):
    from treant_client import mcp_http

    monkeypatch.setattr(mcp_http, "MAX_RESPONSE", 1000)
    with TreantSession(fake.url, "tok-test", 2) as s, pytest.raises(TreantError) as e:
        s.call_tool("huge", {})
    assert e.value.category == "protocol"


def test_proxy_replay_headers_after_an_expired_session(fake, cfg_tmp, monkeypatch):
    # the proxy's reconnect, at the HTTP level: the replayed initialize goes out without a
    # session id, then everything carries the new one and the negotiated protocol version
    import io

    from treant_client.proxy import run_proxy

    monkeypatch.delenv("PWD", raising=False)
    cfg = type(cfg_tmp)(url=fake.url, folders=cfg_tmp.folders)
    cwd = next(p for p, w in cfg.folders.items() if w == "work")
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "v"}}
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "echo", "arguments": {}},
    }

    class Expiring:
        def __iter__(self):
            yield json.dumps(init) + "\n"
            fake.expired.add("sess-1")  # the server forgets the first session
            yield json.dumps(call) + "\n"
            yield json.dumps({**call, "id": 3}) + "\n"

    out = io.StringIO()
    run_proxy(Expiring(), out, cwd, cfg, token_getter=lambda ws: "tok-test")
    replies = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3]
    # the expired session is replaced and the call resent: no error reaches the client
    assert replies[1]["result"]["structuredContent"] == {"echo": {}}
    assert replies[2]["result"]["structuredContent"] == {"echo": {}}
    posts = [r for r in fake.requests if r[0] == "POST"]
    replay = [r for r in posts if r[3]["method"] == "initialize"][1]
    assert "Mcp-Session-Id" not in replay[2] and replay[3]["params"] == {"protocolVersion": "v"}
    after = posts[posts.index(replay) + 1 :]
    assert [r[3]["method"] for r in after] == [
        "notifications/initialized",
        "tools/call",
        "tools/call",
    ]
    for r in after:
        assert r[2]["Mcp-Session-Id"] == "sess-2"
        assert r[2]["Mcp-Protocol-Version"] == "2025-06-18"
