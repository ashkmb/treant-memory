"""Folder-scoped stdio MCP proxy.

Started per session by Claude Code / Kiro. Maps its folder to a Treant workspace, takes that
workspace's token from the Keychain and forwards JSON-RPC to the server. Outside mapped folders
(or without a token) it answers locally with no tools and makes no network call.
"""

from __future__ import annotations

import json
import os
import sys
from typing import TextIO

from treant_client import log
from treant_client.config import Config, load_config
from treant_client.keychain import get_token
from treant_client.mcp_http import RawSession, TreantError
from treant_client.workspaces import resolve

TIMEOUT = 30.0


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


NO_URL = "Treant: no url configured in ~/.config/treant/config.toml"


def _local_reply(msg: dict, no_url: bool = False) -> dict | None:
    if "id" not in msg:
        return None
    method = msg.get("method")
    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "treant", "version": "off"},
        }
    elif method == "tools/list":
        result = {"tools": []}
    elif method == "ping":
        result = {}
    elif method == "tools/call" and no_url:
        result = {"content": [{"type": "text", "text": NO_URL}], "isError": True}
    else:
        return _error(msg["id"], -32601, "method not found")
    return {"jsonrpc": "2.0", "id": msg["id"], "result": result}


def run_proxy(
    stdin: TextIO,
    stdout: TextIO,
    cwd: str,
    cfg: Config,
    token_getter=get_token,
    session_factory=RawSession,
    *,
    use_project_dir: bool = True,
) -> int:
    # Claude Code sets CLAUDE_PROJECT_DIR to the session's project. PWD is ignored: it is
    # inherited from whatever shell launched the client and can name another workspace. Kiro
    # (use_project_dir=False) never sets it, so there it can only be inherited from a Claude
    # Code shell that started Kiro, and names the other session's workspace.
    project = os.environ.get("CLAUDE_PROJECT_DIR") if use_project_dir else None
    ws = resolve(project or cwd, cfg)
    no_url = cfg.url is None
    token = token_getter(ws) if ws and not no_url else None
    online = bool(ws and token)
    if not online:
        status = "no_url" if no_url else "no_token" if ws else "unmapped"
        log.event("proxy_local", workspace=ws or "", status=status)
    session = None
    init_params: dict | None = None  # client's initialize params, replayed on reconnect

    def write(obj: dict) -> None:
        stdout.write(json.dumps(obj) + "\n")
        stdout.flush()

    try:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                write(_error(None, -32700, "parse error"))
                continue
            if not isinstance(msg, dict):
                write(_error(None, -32600, "invalid request"))
                continue
            if not online:
                reply = _local_reply(msg, no_url)
                if reply:
                    write(reply)
                continue
            if msg.get("method") == "initialize":
                init_params = msg.get("params") or {}

            def forward(msg=msg, init_params=init_params) -> dict | None:
                nonlocal session
                if session is None:
                    session = session_factory(cfg.url, token, TIMEOUT)
                    if init_params is not None and msg.get("method") != "initialize":
                        session.send(
                            {
                                "jsonrpc": "2.0",
                                "id": "treant-reinit",
                                "method": "initialize",
                                "params": init_params,
                            }
                        )
                        session.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                return session.send(msg)

            try:
                had_session = session is not None
                try:
                    resp = forward()
                except TreantError as e:
                    # 404 on a live session: the server expired it. MCP says start a new
                    # session and resend; do that once, then report what still fails.
                    if not (e.category == "http_404" and had_session and init_params is not None):
                        raise
                    session.close()
                    session = None
                    resp = forward()
            except TreantError as e:
                if session is not None:
                    session.close()
                    session = None
                log.event("proxy_error", workspace=ws, category=e.category)
                if "id" in msg:
                    write(_error(msg["id"], -32000, f"treant unavailable ({e.category})"))
                continue
            if "id" in msg and resp is not None:
                resp["id"] = msg["id"]
                write(resp)
    finally:
        if session is not None:
            session.close()
    return 0


def main(client: str = "claude") -> int:
    """``client`` is ``claude`` or ``kiro`` (the ``--client`` flag the installer writes)."""
    try:
        cfg = load_config()
    except Exception as e:  # noqa: BLE001 - a broken config answers locally, never crashes
        log.event("proxy_error", category=f"config_{type(e).__name__}")
        cfg = Config()
    return run_proxy(sys.stdin, sys.stdout, os.getcwd(), cfg, use_project_dir=client != "kiro")
