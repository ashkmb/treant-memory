#!/usr/bin/env bash
# End-to-end check of the README quick start: start the compose stack, bootstrap, create a
# token, save a session over MCP and find it again with search. Run from the repository root
# with Docker running. CI runs it on every push; it leaves the stack running for the logs
# (clean up with: docker compose down -v).
#
#   scripts/quickstart-smoke.sh          # uses the image compose.yml names (pulled or built)
#   BUILD=1 scripts/quickstart-smoke.sh  # builds the image from this checkout first
set -euo pipefail

URL=http://127.0.0.1:8000
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

step() { printf '\n== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

if [ ! -f .env ]; then
  step "write .env with random passwords"
  sed -e "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=smoke-$(openssl rand -hex 12)/" \
      -e "s/^TREANT_LOGIN_PASSWORD=.*/TREANT_LOGIN_PASSWORD=smoke-$(openssl rand -hex 12)/" \
      .env.example > .env
fi

if [ "${BUILD:-0}" = 1 ]; then
  step "build the image"
  docker compose build
fi

step "start postgres and embed"
docker compose up -d --wait --wait-timeout 600 postgres embed

step "bootstrap, workspace, canary"
docker compose run --rm admin bootstrap
docker compose run --rm admin workspace create personal
docker compose run --rm admin canary plant personal

step "start treant and worker"
docker compose up -d treant worker
for _ in $(seq 1 60); do
  [ "$(curl -fsS "$URL/healthz" 2>/dev/null || true)" = ok ] && break
  sleep 2
done
[ "$(curl -fsS "$URL/healthz")" = ok ] || fail "/healthz did not answer ok"

step "create a token"
# The token is the last line of the output; it never goes to the log.
docker compose run --rm -T admin token create personal --label smoke \
  --actions create,read,update,delete 2>/dev/null | tail -n 1 > "$work/token"
[ -s "$work/token" ] || fail "no token printed"

step "save a session and find it over MCP"
python3 - "$URL/mcp" "$work/token" <<'PY'
import json, sys, time, urllib.request

url, token_file = sys.argv[1], sys.argv[2]
token = open(token_file).read().strip()
session = None


def call(method, params=None, notify=False):
    global session
    body = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        body["params"] = params
    if not notify:
        body["id"] = 1
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(url, json.dumps(body).encode(), headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        session = resp.headers.get("Mcp-Session-Id") or session
        raw = resp.read().decode()
    if notify:
        return None
    for line in raw.splitlines():
        if line.startswith("data: "):
            raw = line[6:]
    msg = json.loads(raw)
    if "error" in msg:
        sys.exit(f"FAIL: {method}: {msg['error']}")
    return msg["result"]


def tool(name, args):
    result = call("tools/call", {"name": name, "arguments": args})
    if result.get("isError"):
        sys.exit(f"FAIL: {name}: {result['content']}")
    return result["structuredContent"]


call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "quickstart-smoke", "version": "0"}})
call("notifications/initialized", notify=True)
names = {t["name"] for t in call("tools/list")["tools"]}
missing = {"save_session", "search", "brief", "warnings"} - names
if missing:
    sys.exit(f"FAIL: tools missing: {sorted(missing)}")

saved = tool("save_session", {
    "client_session_id": "smoke-1",
    "sections": {"summary": "Billing deploys run make deploy-billing after a dry run."},
})
print("saved session", saved["session_id"])

# The worker embeds the journal entry in the background; wait for a hybrid hit.
for _ in range(60):
    hits = tool("search", {"query": "how do billing deploys work?"})
    found = [h for h in hits["hits"] if "deploy-billing" in h["text"]]
    if found and hits["mode"] == "hybrid":
        print("search found the saved entry, mode", hits["mode"])
        break
    time.sleep(2)
else:
    sys.exit(f"FAIL: saved entry not found by hybrid search: {hits}")
PY

step "worker processed the embed jobs"
jobs=$(docker compose run --rm -T admin jobs list 2>/dev/null)
echo "$jobs"
echo "$jobs" | grep -qE $'\tembed\tdone\t' || fail "no embed job done"

step "quick start OK"
