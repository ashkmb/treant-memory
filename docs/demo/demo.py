"""Tiny MCP client for the README demo (docs/demo/demo.tape). Standard library only.

    TREANT_TOKEN=... python3 docs/demo/demo.py save
    TREANT_TOKEN=... python3 docs/demo/demo.py wait
    TREANT_TOKEN=... python3 docs/demo/demo.py brief billing-api
    TREANT_TOKEN=... python3 docs/demo/demo.py search "why were invoices sent twice?"

It talks to a quick start server (http://127.0.0.1:8000/mcp) the same way an agent does. The
session it saves is made up for the demo.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

URL = os.environ.get("TREANT_URL", "http://127.0.0.1:8000/mcp")
CYAN, DIM, GREEN, BOLD, RESET = "\033[36m", "\033[2m", "\033[32m", "\033[1m", "\033[0m"

SESSION = {
    "journal": "Fixed duplicate invoice emails in the billing-api repo.",
    "decisions": [
        (
            "billing-api: the nightly invoice job takes a Postgres advisory lock,"
            " so only one worker runs it at a time"
        )
    ],
    "lessons": ["Two workers starting the invoice job at once sent every invoice twice"],
    "procedures": ["Deploy billing-api with `make deploy`, after `make deploy DRY_RUN=1`"],
    "open_threads": ["billing-api: alert when the invoice job runs longer than 10 minutes"],
    "warnings": [
        "Never run the billing-api invoice job by hand in production: it emails customers"
    ],
}


class Client:
    def __init__(self, token: str) -> None:
        self.token = token
        self.session: str | None = None
        self._post(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "treant-demo", "version": "0"},
            },
        )
        self._post("notifications/initialized", None, notify=True)

    def _post(self, method: str, params: dict | None, notify: bool = False) -> dict | None:
        body: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            body["id"] = 1
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(URL, json.dumps(body).encode(), headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            self.session = resp.headers.get("Mcp-Session-Id") or self.session
            raw = resp.read().decode()
        if notify:
            return None
        for line in raw.splitlines():
            if line.startswith("data: "):
                raw = line[6:]
        msg = json.loads(raw)
        if "error" in msg:
            sys.exit(f"{method}: {msg['error']}")
        return msg["result"]

    def tool(self, name: str, args: dict) -> dict:
        shown = ", ".join(f"{k}={json.dumps(v)}" for k, v in args.items() if k != "sections")
        print(f"{CYAN}→ {name}({shown}){RESET}")
        result = self._post("tools/call", {"name": name, "arguments": args})
        if result.get("isError"):
            sys.exit(f"{name}: {result['content']}")
        return result["structuredContent"]


def save(c: Client) -> None:
    for key, value in SESSION.items():
        items = value if isinstance(value, list) else [value]
        for item in items:
            print(f"  {DIM}{key:<13}{RESET}{item}")
    out = c.tool("save_session", {"client_session_id": "demo-monday", "sections": SESSION})
    print(f"{GREEN}✓ saved{RESET} session {out['session_id'][:8]}, revision {out['revision']}")


def wait(c: Client) -> None:
    # Until the worker has distilled the session: the brief then has the repo's facts.
    for _ in range(180):
        if (
            "advisory lock"
            in c._post("tools/call", {"name": "brief", "arguments": {"repo": "billing-api"}})[
                "structuredContent"
            ]["brief"]
        ):
            return
        time.sleep(2)
    sys.exit("distillation did not finish")


def brief(c: Client, repo: str) -> None:
    text = c.tool("brief", {"repo": repo})["brief"]
    for line in text.splitlines():
        print(f"{BOLD}{line}{RESET}" if line.startswith("#") else f"  {line}")
    print()


def search(c: Client, query: str) -> None:
    hits = c.tool("search", {"query": query, "limit": 3})
    for h in hits["hits"]:
        text = " ".join(h["text"].split())
        text = text if len(text) <= 88 else text[:87] + "…"
        print(f"  {DIM}{h['type']:<13}{h['score']:.2f}{RESET}  {text}")


def main() -> None:
    token = os.environ.get("TREANT_TOKEN") or sys.exit("set TREANT_TOKEN")
    cmd, args = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("", [])
    c = Client(token)
    if cmd == "save":
        save(c)
    elif cmd == "wait":
        wait(c)
    elif cmd == "brief" and len(args) == 1:
        brief(c, args[0])
    elif cmd == "search" and len(args) == 1:
        search(c, args[0])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
