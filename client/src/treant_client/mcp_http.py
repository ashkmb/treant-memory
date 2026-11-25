"""Minimal MCP streamable-HTTP client (stdlib only).

Errors carry a category and a fixed message; response bodies and tokens never appear in them.
"""

import contextlib
import json
import socket
import urllib.error
import urllib.request

from treant_client import __version__

PROTOCOL_VERSION = "2025-06-18"
MAX_RESPONSE = 16 * 1024 * 1024  # bytes; a larger reply is a protocol error, not a memory spike


class TreantError(Exception):
    """category: unreachable | timeout | http_<code> | protocol | tool_error"""

    def __init__(self, category: str, message: str = ""):
        super().__init__(f"treant {category}" + (f": {message}" if message else ""))
        self.category = category


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _is_timeout(e: BaseException) -> bool:
    return isinstance(e, TimeoutError | socket.timeout) or (
        isinstance(e, urllib.error.URLError) and isinstance(e.reason, TimeoutError | socket.timeout)
    )


def _parse_sse(text: str, want_id) -> dict | None:
    for block in text.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(
            line[5:].lstrip(" ") for line in block.split("\n") if line.startswith("data:")
        )
        if not data:
            continue
        try:
            msg = json.loads(data)
        except ValueError:
            continue
        if isinstance(msg, dict) and (want_id is None or msg.get("id") == want_id):
            return msg
    return None


class RawSession:
    def __init__(self, url: str, token: str, timeout: float):
        self.url = url
        self._token = token
        self.timeout = timeout
        self.session_id: str | None = None
        self.protocol_version = PROTOCOL_VERSION  # replaced by what initialize negotiates
        self._next_id = 0

    def _headers(self) -> dict:
        h = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
            h["MCP-Protocol-Version"] = self.protocol_version
        return h

    def _open(self, req):
        try:
            return _OPENER.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            e.close()
            raise TreantError(f"http_{e.code}") from None
        except Exception as e:  # noqa: BLE001
            if _is_timeout(e):
                raise TreantError("timeout") from None
            if isinstance(e, urllib.error.URLError | OSError):
                raise TreantError("unreachable") from None
            raise TreantError("protocol") from None

    def send(self, message: dict) -> dict | None:
        """POST one JSON-RPC message; the matching response, or None for notifications."""
        req = urllib.request.Request(
            self.url, data=json.dumps(message).encode(), headers=self._headers(), method="POST"
        )
        want_id = message.get("id")
        with self._open(req) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self.session_id = sid
            try:
                raw = resp.read(MAX_RESPONSE + 1)
            except Exception as e:  # noqa: BLE001
                if _is_timeout(e):
                    raise TreantError("timeout") from None
                raise TreantError("unreachable") from None
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if want_id is None:
            return None
        if len(raw) > MAX_RESPONSE:
            raise TreantError("protocol", "response too large")
        text = raw.decode("utf-8", "replace")
        if ctype == "text/event-stream":
            msg = _parse_sse(text, want_id)
        else:
            try:
                msg = json.loads(text)
            except ValueError:
                msg = None
        if not isinstance(msg, dict) or msg.get("id") != want_id:
            raise TreantError("protocol", "no matching response")
        if message.get("method") == "initialize":
            result = msg.get("result")
            version = result.get("protocolVersion") if isinstance(result, dict) else None
            if isinstance(version, str) and version:
                self.protocol_version = version
        return msg

    def close(self) -> None:
        if not self.session_id:
            return
        req = urllib.request.Request(self.url, headers=self._headers(), method="DELETE")
        try:
            with contextlib.suppress(Exception), _OPENER.open(req, timeout=self.timeout):
                pass
        finally:
            self.session_id = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class TreantSession(RawSession):
    def __enter__(self):
        try:
            resp = self._rpc(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "treant-client", "version": __version__},
                },
            )
            if "result" not in resp:
                raise TreantError("protocol", "initialize failed")
            self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except BaseException:
            self.close()
            raise
        return self

    def _rpc(self, method: str, params: dict) -> dict:
        self._next_id += 1
        return self.send(
            {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        )

    def call_tool(self, name: str, args: dict) -> dict:
        resp = self._rpc("tools/call", {"name": name, "arguments": args})
        result = resp.get("result")
        if not isinstance(result, dict):
            raise TreantError("protocol", "tool call failed")
        payload = result.get("structuredContent")
        if not isinstance(payload, dict):
            payload = None
            for block in result.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    try:
                        payload = json.loads(block.get("text", ""))
                    except ValueError:
                        payload = None
                    break
        if not isinstance(payload, dict):
            raise TreantError(
                "tool_error" if result.get("isError") else "protocol", "no usable tool result"
            )
        if "error" in payload or result.get("isError"):
            raise TreantError("tool_error")
        return payload
