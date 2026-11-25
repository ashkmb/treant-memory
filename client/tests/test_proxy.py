import io
import json
from typing import ClassVar

from treant_client.mcp_http import TreantError
from treant_client.proxy import run_proxy


def _run(lines, cwd, cfg, monkeypatch, **kw):
    monkeypatch.delenv("PWD", raising=False)
    out = io.StringIO()
    inp = io.StringIO("".join(json.dumps(m) + "\n" for m in lines))
    rc = run_proxy(inp, out, str(cwd), cfg, **kw)
    return rc, [json.loads(x) for x in out.getvalue().splitlines()]


def _boom(*a, **k):
    raise AssertionError("must not be called")


MSGS = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {}},
]


def _assert_local(replies):
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert replies[0]["result"]["serverInfo"] == {"name": "treant", "version": "off"}
    assert replies[0]["result"]["capabilities"] == {"tools": {}}
    assert "instructions" not in replies[0]["result"]
    assert replies[1]["result"] == {"tools": []}
    assert replies[2]["error"]["code"] == -32601


def test_unmapped_folder_answers_locally_without_network(cfg_tmp, tmp_path, monkeypatch):
    rc, replies = _run(
        MSGS, tmp_path, cfg_tmp, monkeypatch, token_getter=_boom, session_factory=_boom
    )
    assert rc == 0
    _assert_local(replies)


def test_mapped_without_token_answers_locally(cfg_tmp, monkeypatch):
    cwd = next(p for p, w in cfg_tmp.folders.items() if w == "work")
    rc, replies = _run(
        MSGS, cwd, cfg_tmp, monkeypatch, token_getter=lambda ws: None, session_factory=_boom
    )
    assert rc == 0
    _assert_local(replies)


class FakeSession:
    instances: ClassVar[list] = []

    def __init__(self, url, token, timeout):
        self.url, self.token, self.sent, self.closed = url, token, [], False
        FakeSession.instances.append(self)

    def send(self, message):
        self.sent.append(message)
        if "id" not in message:
            return None
        return {"jsonrpc": "2.0", "id": message["id"], "result": {"echo": message["method"]}}

    def close(self):
        self.closed = True


def test_forwards_and_preserves_ids(cfg_tmp, monkeypatch):
    FakeSession.instances = []
    cwd = next(p for p, w in cfg_tmp.folders.items() if w == "startup")
    asked = []
    rc, replies = _run(
        MSGS,
        cwd,
        cfg_tmp,
        monkeypatch,
        token_getter=lambda ws: asked.append(ws) or "tok-test",
        session_factory=FakeSession,
    )
    assert rc == 0 and asked == ["startup"]
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert replies[1]["result"] == {"echo": "tools/list"}
    (s,) = FakeSession.instances
    assert s.token == "tok-test" and s.url == cfg_tmp.url
    assert [m.get("method") for m in s.sent] == [m["method"] for m in MSGS]
    assert s.closed


def test_upstream_error_becomes_jsonrpc_error_and_reconnects(cfg_tmp, monkeypatch):
    FakeSession.instances = []

    class Flaky(FakeSession):
        def send(self, message):
            if len(FakeSession.instances) == 1:
                raise TreantError("timeout")
            return super().send(message)

    cwd = next(p for p, w in cfg_tmp.folders.items() if w == "volunteer")
    _, replies = _run(
        MSGS[:1] + MSGS[2:3],
        cwd,
        cfg_tmp,
        monkeypatch,
        token_getter=lambda ws: "tok-test",
        session_factory=Flaky,
    )
    assert replies[0]["id"] == 1
    assert replies[0]["error"] == {"code": -32000, "message": "treant unavailable (timeout)"}
    assert replies[1]["result"] == {"echo": "tools/list"}
    assert len(FakeSession.instances) == 2 and FakeSession.instances[0].closed


def test_notifications_get_no_reply(cfg_tmp, tmp_path, monkeypatch):
    note = [{"jsonrpc": "2.0", "method": "notifications/initialized"}]
    _, replies = _run(note, tmp_path, cfg_tmp, monkeypatch, session_factory=_boom)
    assert replies == []
    cwd = next(p for p, w in cfg_tmp.folders.items() if w == "work")
    FakeSession.instances = []

    class Failing(FakeSession):
        def send(self, message):
            raise TreantError("unreachable")

    _, replies = _run(
        note, cwd, cfg_tmp, monkeypatch, token_getter=lambda ws: "tok-test", session_factory=Failing
    )
    assert replies == []


def test_reconnect_replays_handshake_before_next_request(cfg_tmp, monkeypatch):
    FakeSession.instances = []

    class Stateful(FakeSession):
        def __init__(self, *a):
            super().__init__(*a)
            self.initialized = False

        def send(self, message):
            if message.get("method") == "initialize":
                self.initialized = True
            elif not self.initialized:
                raise TreantError("http_400")
            if message.get("method") == "tools/list" and len(FakeSession.instances) == 1:
                raise TreantError("timeout")
            return super().send(message)

    cwd = next(p for p, w in cfg_tmp.folders.items() if w == "volunteer")
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "x"}}
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    msgs = [
        init,
        note,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {}},
    ]
    _, replies = _run(
        msgs,
        cwd,
        cfg_tmp,
        monkeypatch,
        token_getter=lambda ws: "tok-test",
        session_factory=Stateful,
    )
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert "result" in replies[0]
    assert replies[1]["error"]["message"] == "treant unavailable (timeout)"
    assert replies[2]["result"] == {"echo": "tools/call"}
    second = FakeSession.instances[1]
    assert [m.get("method") for m in second.sent] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]
    assert second.sent[0]["params"] == {"protocolVersion": "x"}


def _workspace_asked(cfg, cwd, monkeypatch, pwd=None, project=None, **kw):
    FakeSession.instances = []
    asked = []
    out = io.StringIO()
    inp = io.StringIO(json.dumps(MSGS[0]) + "\n")
    if pwd is None:
        monkeypatch.delenv("PWD", raising=False)
    else:
        monkeypatch.setenv("PWD", pwd)
    if project is None:
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", project)
    run_proxy(
        inp,
        out,
        str(cwd),
        cfg,
        token_getter=lambda ws: asked.append(ws) or "tok-test",
        session_factory=FakeSession,
        **kw,
    )
    return asked


def test_pwd_is_ignored(cfg_tmp, monkeypatch):
    work = next(p for p, w in cfg_tmp.folders.items() if w == "work")
    personal = next(p for p, w in cfg_tmp.folders.items() if w == "personal")
    assert _workspace_asked(cfg_tmp, work, monkeypatch, pwd=personal) == ["work"]


def test_claude_project_dir_wins_over_cwd(cfg_tmp, monkeypatch):
    work = next(p for p, w in cfg_tmp.folders.items() if w == "work")
    personal = next(p for p, w in cfg_tmp.folders.items() if w == "personal")
    assert _workspace_asked(cfg_tmp, personal, monkeypatch, pwd=personal, project=work) == ["work"]


def test_kiro_proxy_ignores_an_inherited_claude_project_dir(cfg_tmp, monkeypatch):
    # Kiro started from a Claude Code shell inherits that session's CLAUDE_PROJECT_DIR; the
    # Kiro proxy must use its own folder, or it would use (and save to) the other workspace.
    work = next(p for p, w in cfg_tmp.folders.items() if w == "work")
    personal = next(p for p, w in cfg_tmp.folders.items() if w == "personal")
    asked = _workspace_asked(cfg_tmp, personal, monkeypatch, project=work, use_project_dir=False)
    assert asked == ["personal"]


def test_cli_mcp_proxy_client_flag(monkeypatch):
    from treant_client import cli, proxy

    seen = []
    monkeypatch.setattr(proxy, "main", lambda client="claude": seen.append(client) or 0)
    assert cli.main(["mcp-proxy"]) == 0
    assert cli.main(["mcp-proxy", "--client", "kiro"]) == 0
    assert seen == ["claude", "kiro"]


def test_main_kiro_ignores_claude_project_dir(cfg_tmp, monkeypatch, tmp_path):
    from treant_client import proxy

    work = next(p for p, w in cfg_tmp.folders.items() if w == "work")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", work)
    seen = {}

    def fake_run(stdin, stdout, cwd, cfg, *, use_project_dir=True):
        seen["use_project_dir"] = use_project_dir
        return 0

    monkeypatch.setattr(proxy, "load_config", lambda: cfg_tmp)
    monkeypatch.setattr(proxy, "run_proxy", fake_run)
    assert proxy.main("kiro") == 0 and seen["use_project_dir"] is False
    assert proxy.main() == 0 and seen["use_project_dir"] is True


def test_main_answers_locally_when_config_load_fails(monkeypatch, tmp_path):
    from treant_client import proxy

    def boom():
        raise ValueError("bad toml")

    monkeypatch.setattr(proxy, "load_config", boom)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(MSGS[0]) + "\n"))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert proxy.main() == 0
    reply = json.loads(out.getvalue())
    assert reply["id"] == 1 and reply["result"]["serverInfo"]["version"] == "off"


def _expiring(expire_first: bool, expire_all: bool = False):
    class Expiring(FakeSession):
        def send(self, message):
            first = len(FakeSession.instances) == 1
            if message.get("method") == "tools/call" and (expire_all or (expire_first and first)):
                raise TreantError("http_404")  # the server expired this session
            return super().send(message)

    return Expiring


_HANDSHAKE = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "x"}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {}},
]


def test_expired_session_reconnects_and_retries_once(cfg_tmp, monkeypatch):
    """A 404 means the server expired the session: start a new one and resend, no error."""
    FakeSession.instances = []
    cwd = next(p for p, w in cfg_tmp.folders.items() if w == "volunteer")
    _, replies = _run(
        _HANDSHAKE,
        cwd,
        cfg_tmp,
        monkeypatch,
        token_getter=lambda ws: "tok-test",
        session_factory=_expiring(True),
    )
    assert [r["id"] for r in replies] == [1, 2]
    assert replies[1]["result"] == {"echo": "tools/call"}
    first, second = FakeSession.instances
    assert first.closed
    assert [m.get("method") for m in second.sent] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]
    assert second.sent[0]["params"] == {"protocolVersion": "x"}


def test_expired_session_retry_is_not_repeated(cfg_tmp, monkeypatch):
    FakeSession.instances = []
    cwd = next(p for p, w in cfg_tmp.folders.items() if w == "volunteer")
    _, replies = _run(
        _HANDSHAKE,
        cwd,
        cfg_tmp,
        monkeypatch,
        token_getter=lambda ws: "tok-test",
        session_factory=_expiring(True, expire_all=True),
    )
    assert replies[1]["error"]["message"] == "treant unavailable (http_404)"
    assert len(FakeSession.instances) == 2
