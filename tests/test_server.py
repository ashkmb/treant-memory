import asyncio
import json
import socket
import threading
import time

import pytest
import uvicorn
from fakes import FakeEmbedder
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from treant import worker
from treant.server import INSTRUCTIONS, build_app
from treant.tokens import create_token


@pytest.fixture
def live_server(db):
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


def test_end_to_end_save_then_search_over_http(db, live_server):
    import httpx2

    raw = create_token(db, "alpha", "e2e", {"create", "read"})

    async def run():
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {raw}"})
        async with http, Client(streamable_http_client(live_server, http_client=http)) as c:
            tools = {t.name for t in (await c.list_tools()).tools}
            descs = {t.name: t.description for t in (await c.list_tools()).tools}
            assert descs["guard_check"].startswith("Deprecated")
            assert tools == {
                "save_session", "search", "brief", "guard_check", "warnings",
                "update_fact", "retract", "report_mistake", "repo_facts", "flag_stale",
            }  # fmt: skip
            assert c.instructions == INSTRUCTIONS
            saved = await c.call_tool(
                "save_session",
                {"client_session_id": "e2e-1", "sections": {"journal": "tuned the heron cache"}},
            )
            assert saved.structured_content["revision"] == 1
            while await asyncio.to_thread(worker.run_once, db, FakeEmbedder()):
                pass
            found = await c.call_tool("search", {"query": "heron cache"})
            assert found.structured_content["hits"]
            only = await c.call_tool("brief", {"repo": "no-such-repo", "repo_only": True})
            assert only.structured_content == {"brief": "", "repo_only": True}

    asyncio.run(run())


def test_guard_check_tool_accepts_ansible_inventory(db, live_server):
    import httpx2

    raw = create_token(db, "alpha", "guard-ansible", {"read"})

    async def run():
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {raw}"})
        async with http, Client(streamable_http_client(live_server, http_client=http)) as c:
            res = await c.call_tool(
                "guard_check",
                {"command": "ansible-playbook s.yml", "cwd": "/x", "ansible_inventory": "inv/h"},
            )
            out = res.structured_content
            assert out["unresolved"] == []
            assert out["unlisted"] == [{"kind": "ansible_inventory", "value": "inv/h"}]

    asyncio.run(run())


def test_warnings_tool_over_http(db, live_server):
    import uuid

    import httpx2

    from treant import graph

    name = f"httpwarn{uuid.uuid4().hex[:8]}"
    with db.ws_tx("alpha") as c:
        n = graph.add_node(c, "alpha", "instance", "svc", name)
        graph.add_fact(c, "alpha", "warning", "drain first", node_id=n)
    raw = create_token(db, "alpha", "warn-http", {"read"})
    beta = create_token(db, "beta", "warn-http-b", {"read"})
    no_read = create_token(db, "alpha", "warn-http-c", {"create"})

    async def call(token):
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"})
        async with http, Client(streamable_http_client(live_server, http_client=http)) as c:
            res = await c.call_tool("warnings", {"names": [name.upper(), "other"]})
            return res.structured_content

    assert asyncio.run(call(raw)) == {"warnings": ["drain first"]}
    assert asyncio.run(call(beta)) == {"warnings": []}
    assert asyncio.run(call(no_read)) == {"error": "forbidden"}


def _parse(r):
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        for line in r.text.splitlines():
            if line.startswith("data:") and line[5:].strip():
                return json.loads(line[5:])
        return None
    return r.json() if r.content else None


def test_session_is_bound_to_creating_token(db, live_server):
    import httpx2

    tok_a = create_token(db, "alpha", "own-a", {"create", "read"})
    tok_b = create_token(db, "beta", "own-b", {"create", "read"})
    base = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with httpx2.Client(timeout=10) as c:
        ha = {**base, "Authorization": f"Bearer {tok_a}"}
        init = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "x", "version": "1"}},
        }  # fmt: skip
        r = c.post(live_server, headers=ha, json=init)
        assert r.status_code == 200, r.text
        sid = r.headers["mcp-session-id"]
        pv = _parse(r)["result"]["protocolVersion"]
        ha |= {"Mcp-Session-Id": sid, "MCP-Protocol-Version": pv}
        c.post(
            live_server, headers=ha, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        hb = {**base, "Authorization": f"Bearer {tok_b}", "Mcp-Session-Id": sid,
              "MCP-Protocol-Version": pv}  # fmt: skip
        call = {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "search", "arguments": {"query": "x"}},
        }  # fmt: skip
        assert c.post(live_server, headers=hb, json=call).status_code == 404
        assert c.delete(live_server, headers=hb).status_code == 404
        r = c.post(live_server, headers=ha, json={**call, "id": 4})
        assert r.status_code == 200 and "hits" in str(_parse(r))


def test_wrong_host_header_rejected_when_hosts_set(db):
    import httpx2

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    cfg = uvicorn.Config(
        build_app(db, FakeEmbedder(), allowed_hosts=("treant.example.com",)),
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(cfg)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    try:
        tok = create_token(db, "alpha", "hosts", {"create", "read"})
        url = f"http://127.0.0.1:{port}/mcp"
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {tok}",
        }
        init = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "x", "version": "1"}},
        }  # fmt: skip
        with httpx2.Client(timeout=10) as c:
            bad = c.post(url, headers={**headers, "Host": "evil.example"}, json=init)
            assert bad.status_code not in (200, 401)
            ok = c.post(url, headers={**headers, "Host": "treant.example.com"}, json=init)
            assert ok.status_code == 200, ok.text
            good_host = {**headers, "Host": "treant.example.com"}
            wrong_origin = c.post(
                url, headers={**good_host, "Origin": "https://evil.example"}, json=init
            )
            assert wrong_origin.status_code == 403
            right_origin = c.post(
                url, headers={**good_host, "Origin": "https://treant.example.com"}, json=init
            )
            assert right_origin.status_code == 200
            # the bearer check runs first: a bad token on a bad host is a plain 401
            both_bad = c.post(
                url,
                headers={**headers, "Host": "evil.example", "Authorization": "Bearer nope"},
                json=init,
            )
            assert both_bad.status_code == 401 and both_bad.text == "unauthorized"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
