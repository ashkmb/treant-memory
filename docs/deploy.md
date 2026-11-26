# Deploying Treant

This guide takes a Linux host with Docker to a running Treant: the MCP server, the worker,
the owner dashboard, nightly backups and a nightly isolation test. Every file it refers to
is in `deploy/`:

| File | What |
|---|---|
| `deploy/compose.prod.yml` | The stack: `db`, `embed` (optional), `server`, `worker`, `dashboard`, `admin` (one-shot CLI) |
| `deploy/env/*.env.example` | One env file per trust level; copy to `/etc/treant/*.env` |
| `deploy/scripts/treant` | Owner CLI wrapper (runs the `admin` service) |
| `deploy/scripts/backup.sh`, `restore-check.sh`, `purge.sh`, `isolation.sh` | Operations |
| `deploy/systemd/*.service`, `*.timer` | Nightly backup and isolation test |
| `deploy/caddy/Caddyfile.example` | TLS reverse proxy for the MCP endpoint and the dashboard |

The paths below assume a checkout of this repository at `/opt/treant`, env files in
`/etc/treant` and the embedding model in `/var/lib/treant/models`. Change them in one place
each: the compose variables `ENV_DIR` and `EMBED_MODEL_DIR`, `COMPOSE_FILE` for the scripts
and the systemd units. Those three are read by Docker Compose and the scripts only; Treant
itself reads nothing but the `TREANT_*` variables listed here.

## How the pieces fit

| Service | Command | Listens | Talks to |
|---|---|---|---|
| `db` | Postgres 17 + pgvector | inside the stack only | |
| `embed` | llama.cpp server (profile `embed`) | inside the stack only | |
| `server` | `treant serve` | `127.0.0.1:8000` on the host | `db`, embedding service |
| `worker` | `treant worker` | nothing | `db`, embedding service, LLM gateway, Jev judge |
| `dashboard` | `treant dashboard` | `127.0.0.1:8001` on the host | `db`, embedding service, OIDC provider |
| `admin` | `treant <args>` (profile `admin`, one-shot) | nothing | `db`, embedding service |

Data model in one paragraph: every workspace gets its own schema (`ws_<name>`) and its own
database role; `general` holds shared concepts and know-how; `control` holds tokens, jobs,
the review queue, audit and metrics. A client holds a bearer token bound to one workspace.
A save is stored as a session revision and journal entry, then the worker embeds it,
distills it into graph nodes and facts with an LLM, and queues the general half for the
gate. The gate runs rules, then an LLM judge, then the Jev judge; only an item that passes
all three is published to `general` (or waits for the owner in `review` mode).

Only the worker calls models. The server has no route to the internet: it sits on an
internal network plus a published-port network with IP masquerading off, and its DNS
resolves service names only.

## Requirements

Each requirement below is stated as the contract the code expects. File references are to
`src/treant/`.

### Host

- Linux with Docker Engine and the Compose plugin (v2.24 or newer: `compose.prod.yml` uses
  `env_file` entries with `path` and `required`).
- bash 4.4 or newer, `flock` (util-linux) and `restic` for the scripts.
- About 2 GB of RAM for the stack with the bundled embedder (memory limits: db 1 GB, embed
  2 GB, server and worker 512 MB each, dashboard 256 MB).
- The image is built from this repository (`Dockerfile`: Python 3.13 slim, non-root user
  10001, entrypoint `treant`). No image is published to a registry.

### Postgres 17 and pgvector

- Postgres 17 with the pgvector extension, version 0.8 or newer (the schemas use
  `vector(768)` columns with HNSW indexes, `vector_cosine_ops`). The stack uses
  `pgvector/pgvector:0.8.7-pg17-bookworm`, the same image the test suite runs against.
- No other extension is needed.
- Two connection strings, two privilege levels:
  - `TREANT_ADMIN_DATABASE_URL`: a **superuser**. Used only by the owner CLI for
    `bootstrap` (creates the `vector` extension, the roles and the `control` and `general`
    schemas), `upgrade`, `workspace create`, `workspace purge`, `workspace purge-verify`
    and `schemas` (`migrate.py`, `cli.py`).
  - `TREANT_DATABASE_URL`: the **login role** `<TREANT_ROLE_PREFIX>_login`, which
    `bootstrap` creates with `NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    NOINHERIT` and the password from `TREANT_LOGIN_PASSWORD` (stored as a SCRAM verifier
    computed client-side). Every running process uses this one. It switches to a per-workspace
    or per-function role with `SET LOCAL ROLE` for each transaction (`db.py`), so a query
    for one workspace runs with that workspace's grants only.
- `TREANT_ROLE_PREFIX` (default `treant`, pattern `[a-z][a-z0-9_]{0,23}`) prefixes every
  role: `<prefix>_login`, `<prefix>_admin`, `<prefix>_control`, `<prefix>_gate`,
  `<prefix>_publisher` and `<prefix>_ws_<workspace>`. Roles are cluster-wide, so two Treant
  installs can share a Postgres cluster only with different prefixes.
- Put the passwords into the DSNs unencoded only if they are alphanumeric; otherwise
  percent-encode them.

### Embedding service

- Contract (`embed.py`): `POST <TREANT_EMBED_URL>/v1/embeddings` with JSON
  `{"model": <TREANT_EMBED_MODEL>, "input": [<string>, ...]}`, up to 32 inputs per request,
  each at most 5000 characters plus a prefix. The reply must be the OpenAI shape
  `{"data": [{"index": i, "embedding": [float, ...]}, ...]}` with one item per input. Every
  vector must have exactly `TREANT_EMBED_DIMS` numbers; Treant normalises them itself.
- **No authentication header is sent.** Keep the embedding service on a private network
  that only Treant can reach (in the stack it is on the internal `backend` network).
- Timeouts: 120 s for a document batch, 15 s for a search query. A slow embedder makes
  searches fail fast rather than hang.
- **The dimension is fixed at 768.** The vector columns are declared `vector(768)` in
  `sql/workspace.sql` and `sql/general.sql`, so `TREANT_EMBED_DIMS` must be 768 (its
  default). The client also adds EmbeddingGemma's task prefixes (`task: search result |
  query: ` for queries, `title: none | text: ` for documents), so the intended model is
  **EmbeddingGemma 300M** (`TREANT_EMBED_MODEL` default `embeddinggemma-300m`). Another
  768-dimension model will run, but its retrieval quality is untested with these prefixes.
  There is no re-embed command: choose the model before the first save.
- A public option, bundled as the `embed` profile: the llama.cpp server with the
  `ggml-org/embeddinggemma-300M-GGUF` model (`embeddinggemma-300M-Q8_0.gguf`), mean pooling,
  2048-token context. A llama.cpp server with one model loaded answers whatever the `model`
  field says (check this with your version). Any other OpenAI-compatible embeddings server works if it serves a 768-dimension
  model under the name in `TREANT_EMBED_MODEL`.
- Who needs it: `serve`, `worker`, `dashboard` (search), and the CLI commands
  `canary plant` and `isolation-test`. All of them refuse to start without
  `TREANT_EMBED_URL`.

### LLM gateway (distillation, rewrites, LLM judge)

- Contract (`backends.py`): `POST <TREANT_LITELLM_URL>/chat/completions` with header
  `Authorization: Bearer <key>` and JSON `{"model": <alias>, "messages": [...],
  "temperature": 0, "max_tokens": N}`. The reply text is read from
  `choices[0].message.content`. Any OpenAI-compatible endpoint works; the variable is named
  after LiteLLM because that is the reference setup. `TREANT_LITELLM_URL` is the base URL
  including the version path, for example `https://llm.example.com/v1`.
- Three uses, all from the worker only:
  - distill: turns each saved revision into nodes, edges and facts; `max_tokens` 16000,
    model = the workspace's `alias` setting (default `default`); the reply must be a JSON
    object.
  - LLM judge: the first gate judge; `max_tokens` 2000, model = the workspace's
    `judge_alias` (default `default`); the reply must hold `{"ok": true|false}`.
  - rewrite: rewrites a fact that failed the gate (at most 2 times); `max_tokens` 4000.
  Reasoning models spend part of `max_tokens` on hidden reasoning; that is why the caps
  are high. Timeout 60 s per call.
- The model names `default` (or whatever you set with `treant workspace set <ws> --alias A
  --judge-alias A`) must exist on the gateway.
- **One key per workspace**, `TREANT_LITELLM_KEY_<WORKSPACE IN UPPER CASE>`, for example
  `TREANT_LITELLM_KEY_ACME`. The worker picks the key of the workspace whose job it runs.
  Separate keys give per-workspace spend and budget limits on the gateway, and let you
  revoke one workspace's model access without touching the others. With LiteLLM, create one
  virtual key per workspace (`POST /key/generate` with a `key_alias`, the allowed `models`
  and a `max_budget`). A gateway without per-key accounting still needs a non-empty value
  per workspace; the same key in every variable is allowed.
- **Without a gateway.** If `TREANT_LITELLM_URL` or a workspace's key is missing, that
  workspace's distill jobs fail as `model_unavailable`, are retried (12 attempts) and end
  `dead`. Saves, the journal, embeddings and search keep working. To run with no LLM at
  all, set every workspace to `treant workspace set <ws> --backend none`: then nothing from
  that workspace is ever sent to a model, distill jobs complete without work, there is no
  graph distillation and no general know-how from it (memory is the saved journal,
  searchable by meaning and keywords). `--backend none` is also the right setting for a
  workspace whose content must never leave the host.

### Jev judge

- What it is to the code (`backends.py`, `gate.py`, `criteria.toml`): an HTTP scoring
  service and the second judge on every item headed for `general`. It is asked only after
  the rules found nothing and the LLM judge said yes; an item is published only when both
  judges pass. It is never asked about workspace content that stays in the workspace.
- Request: `POST <TREANT_JEV_URL>/v1/systemone` with `Authorization: Bearer
  <TREANT_JEV_API_KEY>` and JSON:

  ```json
  {"model": "<TREANT_JEV_MODEL>",
   "state": "<the fact text, or a concept's name, aliases and type>",
   "questions": {"general": {"type": "noul", "instructions": "<question from criteria.toml>"}}}
  ```

- Reply: HTTP 200 with `{"answers": {"general": {"noul": <number from 0 to 1>}}}`. A score
  of at least `jev_threshold` (0.8 in `criteria.toml`) passes. A non-number, a boolean or a
  value outside 0..1 counts as the judge being unavailable, never as a pass. Timeout 20 s.
- Defaults (`config.py`): `TREANT_JEV_URL=https://api.typesafe.ai`,
  `TREANT_JEV_MODEL=jev-1.13.0`. The default endpoint is a third-party internet service:
  the checked text leaves your network. It has already passed the rules and the LLM judge,
  but decide whether that is acceptable for your workspaces.
- **It is required for automatic publishing.** With `TREANT_JEV=on` (the default) and no
  `TREANT_JEV_API_KEY`, every gate job that reaches Jev fails as `judge_unavailable`, is
  retried up to 20 times and ends `dead`; its queue item stays `pending`. Everything else
  works: saves, search, briefs, distillation into the workspace graph.
- Ways to run without it:
  1. `TREANT_JEV=off` (recommended). The gate never calls Jev. An item that passes the
     rules and the LLM judge goes to `waiting_review` whatever the workspace's publish
     mode; one judge never publishes on its own. The owner publishes it with
     `treant general approve <id>` (after `treant general show <id>`) or from the
     dashboard's review queue, or drops it with `treant general drop <id>`. The owner's
     approval runs the rules again and refuses on a finding unless `--yes` is given. Any
     value other than `on` or `off` stops the CLI at start and makes gate jobs fail as
     `settings_invalid`.
  2. A replacement judge. Point `TREANT_JEV_URL` at your own service that implements the
     request and reply above (any bearer value in `TREANT_JEV_API_KEY`). A judge that always
     answers 1.0 turns the gate into "rules plus LLM judge"; do that knowingly.
  3. `--backend none` for a workspace: none of its content reaches any model, and its
     general items are held as `blocked` with reason `judge:none`.

### OIDC provider (dashboard)

- Flow (`dashboard/identity.py`): authorization code with PKCE (S256), confidential client,
  token endpoint auth `client_secret_post`, scopes `openid profile email groups`. Discovery
  at `<issuer without trailing slash>/.well-known/openid-configuration`; the discovery
  document's `issuer` must equal `TREANT_OIDC_ISSUER` byte for byte.
- Client settings to register:
  - client id `TREANT_OIDC_CLIENT_ID` (default `treant`) and a secret
    (`TREANT_OIDC_CLIENT_SECRET`);
  - redirect URI `<TREANT_DASHBOARD_URL>/auth/callback`, for example
    `https://treant-admin.example.com/auth/callback`.
- Claims used: the ID token must carry `auth_time` (integer) and `sub`; the userinfo
  response must carry the same `sub`, `preferred_username` (a string up to 128
  characters) and `groups` (a list of strings). Login succeeds only when
  `preferred_username` is in `TREANT_DASHBOARD_OWNERS` and `TREANT_DASHBOARD_GROUP`
  (default `treant`) is in `groups`.
- Re-authentication: sensitive actions (approving queue items, forced edits and restores,
  quarantine release, workspace settings and archive, token create and revoke) need a login
  no older than `TREANT_DASHBOARD_REAUTH_S` seconds (default 300). A stale login is sent
  back to the provider with `prompt=login` and `max_age=0`, and the dashboard refuses the
  callback unless the new `auth_time` is later than the moment the re-auth started. The
  provider must honour `prompt=login`.
- Sign-out ends the provider session too when the discovery document has an
  `end_session_endpoint` on the issuer's own origin.
- The session is a signed cookie (`treant_dash`, Secure, SameSite=Lax, 8 hours absolute,
  no provider tokens inside), signed with `TREANT_DASHBOARD_SESSION_SECRET` (32+
  characters).
- Authelia (4.38 or newer) example client; check the option names against your version's
  documentation:

  ```yaml
  identity_providers:
    oidc:
      clients:
        - client_id: treant
          client_name: Treant
          client_secret: '<hash of the secret, see Authelia docs>'
          public: false
          authorization_policy: two_factor
          require_pkce: true
          pkce_challenge_method: S256
          redirect_uris:
            - https://treant-admin.example.com/auth/callback
          scopes: [openid, profile, email, groups]
          response_types: [code]
          grant_types: [authorization_code]
          token_endpoint_auth_method: client_secret_post
  ```

  `TREANT_OIDC_ISSUER=https://auth.example.com` (Authelia's issuer is the origin, no path).
  Put the owner in a group named `treant`.
- Authentik example: an OAuth2/OpenID provider with client type Confidential, the redirect
  URI above (strict), a signing key, and the scope mappings `openid`, `profile` and `email`
  (Authentik's `profile` mapping carries `preferred_username` and `groups`). Bind it to an
  application with slug `treant` and add the owner to a group `treant`. The issuer is
  per application and keeps its trailing slash:
  `TREANT_OIDC_ISSUER=https://auth.example.com/application/o/treant/`. Authentik acts on
  `prompt=login` and ignores `max_age=0`; the `auth_time` check covers both.
- No provider: `TREANT_DASHBOARD_AUTH=none` turns login off; every request acts as the one
  user in `TREANT_DASHBOARD_OWNERS`, protected only by CSRF tokens and whoever can reach
  the port. Use it only on a dashboard that is reachable from your own machine alone.

### Reverse proxy, TLS and exposure

- Both apps listen on plain HTTP on host loopback; terminate TLS in a reverse proxy that
  passes the original `Host` header through (Caddy and nginx with `proxy_set_header Host
  $host` do). `deploy/caddy/Caddyfile.example` is a working starting point.
- MCP endpoint: streamable HTTP at `/mcp` (no trailing slash), plus `GET /healthz`
  (`ok`, no token). Every other request needs `Authorization: Bearer <token>`; a missing or
  bad token gets a bare `401 unauthorized`.
- `TREANT_ALLOWED_HOSTS` (comma-separated) turns on DNS-rebinding protection for `/mcp`:
  the `Host` header must equal one entry exactly, port included (`host:*` allows any port),
  otherwise 421; a browser `Origin` must be `https://<entry>`, otherwise 403. Requests
  without an `Origin` (CLI clients) pass. Empty means the check is off and the token is the
  only gate. Set it to your public host name, for example `treant.example.com`.
- Dashboard: answers only requests whose `Host` (port stripped) is the host of
  `TREANT_DASHBOARD_URL`, otherwise 400; `/healthz` is exempt. `TREANT_DASHBOARD_URL` must
  be `https://` with no path, and the cookie is Secure, so the dashboard only works
  through TLS.
- Exposure advice: keep both behind a network boundary you control (LAN, VPN or an
  identity-aware proxy). The MCP endpoint is token-protected and may be exposed more widely
  if your clients need it; expose only `/mcp` and `/healthz`. Keep the dashboard private:
  it can approve content into `general`, mint tokens and archive workspaces.

## Configuration reference

All variables Treant reads, by env file. Defaults are from `config.py` and
`dashboard/config.py`.

| Variable | File | Required | Default | Meaning |
|---|---|---|---|---|
| `TREANT_DATABASE_URL` | `treant.env` | yes | | Login-role DSN |
| `TREANT_ROLE_PREFIX` | `treant.env` | no | `treant` | Role name prefix |
| `TREANT_EMBED_URL` | `treant.env` | yes | | Embedding service base URL |
| `TREANT_EMBED_MODEL` | `treant.env` | no | `embeddinggemma-300m` | `model` in embedding requests |
| `TREANT_EMBED_DIMS` | `treant.env` | no | `768` | Must stay 768 |
| `TREANT_BIND` | `treant.env` | no | `127.0.0.1:8000` | Server listen address; `0.0.0.0:8000` in a container |
| `TREANT_ALLOWED_HOSTS` | `treant.env` | no | empty (check off) | Accepted `Host` values for `/mcp` |
| `TREANT_ADMIN_DATABASE_URL` | `admin.env` | for admin commands | | Superuser DSN |
| `TREANT_LOGIN_PASSWORD` | `admin.env` | for `bootstrap` | | Password set on the login role |
| `TREANT_LITELLM_URL` | `worker.env` | for distillation | | OpenAI-compatible base URL |
| `TREANT_LITELLM_KEY_<WS>` | `worker.env` | per workspace | | Gateway key of workspace `<ws>` |
| `TREANT_JEV` | `worker.env` | no | `on` | `off`: never call Jev, send every pass to review |
| `TREANT_JEV_API_KEY` | `worker.env` | for auto-publishing | | Jev bearer key |
| `TREANT_JEV_URL` | `worker.env` | no | `https://api.typesafe.ai` | Jev base URL |
| `TREANT_JEV_MODEL` | `worker.env` | no | `jev-1.13.0` | Jev model |
| `TREANT_DASHBOARD_URL` | `dashboard.env` | yes | | Public `https://` origin |
| `TREANT_DASHBOARD_AUTH` | `dashboard.env` | no | `oidc` | `oidc` or `none` |
| `TREANT_OIDC_ISSUER` | `dashboard.env` | with `oidc` | | Issuer URL |
| `TREANT_OIDC_CLIENT_ID` | `dashboard.env` | no | `treant` | Client id |
| `TREANT_OIDC_CLIENT_SECRET` | `dashboard.env` | with `oidc` | | Client secret |
| `TREANT_DASHBOARD_SESSION_SECRET` | `dashboard.env` | yes | | Cookie signing key, 32+ characters |
| `TREANT_DASHBOARD_OWNERS` | `dashboard.env` | yes | | Allowed usernames, comma-separated |
| `TREANT_DASHBOARD_GROUP` | `dashboard.env` | no | `treant` | Required group |
| `TREANT_DASHBOARD_BIND` | `dashboard.env` | no | `0.0.0.0:8001` | Dashboard listen address |
| `TREANT_DASHBOARD_REAUTH_S` | `dashboard.env` | no | `300` | Max login age for sensitive actions |

`db.env` holds the Postgres image's own `POSTGRES_USER`, `POSTGRES_PASSWORD` and
`POSTGRES_DB`; `backup.env` holds `RESTIC_PASSWORD` and the scripts' `BACKUP_REPO_BASE`
and `BACKUP_KEEP`.

The split matters: the server and dashboard never get a model key or the superuser DSN, the
worker never gets the superuser DSN, and only the one-shot `admin` container gets
`TREANT_ADMIN_DATABASE_URL` and `TREANT_LOGIN_PASSWORD`.

## Deploy with Docker Compose

Run everything below as root (or with sudo) on the host.

### 1. Source and image

```sh
git clone <this repository> /opt/treant
cd /opt/treant
export COMPOSE_FILE=/opt/treant/deploy/compose.prod.yml
docker compose build server          # builds treant:0.1.1 for every Treant service
install -m 0750 deploy/scripts/treant /usr/local/sbin/treant
```

### 2. Env files

```sh
install -d -m 0750 /etc/treant
for f in db treant admin worker dashboard backup; do
  install -m 0600 deploy/env/$f.env.example /etc/treant/$f.env
done
```

Edit each file and replace every `CHANGE_ME`. Generate the two database passwords with
`openssl rand -base64 36 | tr -d '/+='` (alphanumeric, so they can go into DSNs as they
are), the session secret with `openssl rand -base64 48`. The superuser password appears in
`db.env` and `admin.env`; the login password in `admin.env` (`TREANT_LOGIN_PASSWORD`) and
inside `TREANT_DATABASE_URL` in `treant.env`.

### 3. Embedding model (bundled embedder only)

Skip this if `TREANT_EMBED_URL` points at your own service.

```sh
install -d -m 0755 /var/lib/treant/models
curl -fL -o /var/lib/treant/models/embeddinggemma-300M-Q8_0.gguf \
  https://huggingface.co/ggml-org/embeddinggemma-300M-GGUF/resolve/0f741b5a6585bd53aeb15cd1372c56f2a0f65e12/embeddinggemma-300M-Q8_0.gguf
echo "b5ce9d77a3fc4b3b39ccb5643c36777911cc4eb46a66962eadfa3f5f60490d63  /var/lib/treant/models/embeddinggemma-300M-Q8_0.gguf" | sha256sum -c
export COMPOSE_PROFILES=embed      # keep this set in every shell that runs compose
```

The URL pins a commit of the model repository; check the model's license terms (Gemma)
before you use it.

### 4. Database and bootstrap

The server and worker log in as `<prefix>_login`, which exists only after bootstrap, so
start Postgres (and the embedder) alone first:

```sh
docker compose up -d db              # with COMPOSE_PROFILES=embed: docker compose up -d db embed
treant bootstrap                     # prints "bootstrap complete"
```

`bootstrap` is idempotent but rewrites the login role's password from
`TREANT_LOGIN_PASSWORD` every time it runs.

### 5. Workspaces

Workspace names match `[a-z][a-z0-9]{1,30}`; `general` and `control` are reserved.

```sh
for ws in personal acme startup; do
  treant workspace create "$ws" </dev/null
  treant canary plant "$ws" --if-missing </dev/null   # needs the embedder
done
```

Each workspace needs a key in `worker.env` (`TREANT_LITELLM_KEY_PERSONAL`, ...), or
`treant workspace set <ws> --backend none`.

Before the first save, give each workspace its deny-list rules. The gate already catches
ticket IDs, IP addresses, 12-digit account IDs, private host names and the instance names
stored in every workspace; add the terms only you know:

```sh
treant workspace set acme --term acme --domain acme.example.com --ticket-prefix ACME </dev/null
treant workspace set acme --publish review </dev/null    # optional: owner approves every general item
treant workspace show acme
```

`--term`, `--domain` and `--ticket-prefix` can be repeated; a given list replaces the
stored one (`--clear-terms` and friends empty it, `--reset` starts from the defaults).
Terms also drive the server-side scrubber that runs on every save.

### 6. Start the stack

```sh
docker compose up -d                 # db, server, worker, dashboard (+ embed with the profile)
docker compose ps
curl -fsS http://127.0.0.1:8000/healthz      # ok
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8000/mcp   # 401
curl -fsS http://127.0.0.1:8001/healthz      # ok
```

The dashboard refuses to start until `dashboard.env` is complete (OIDC issuer and secret,
session secret, owners). To bring it up later, start only `docker compose up -d db server
worker` for now.

Then configure the reverse proxy (`deploy/caddy/Caddyfile.example`) and check through it:
`https://treant.example.com/healthz` answers `ok`, `/mcp` without a token answers 401,
and `https://treant-admin.example.com/` redirects to `/login`.

If the LLM gateway runs in another Compose project on the same host, attach the worker to
that project's network with an override file and list both files in `COMPOSE_FILE`
(colon-separated):

```yaml
# /opt/treant/deploy/compose.override.yml
services:
  worker:
    networks: [backend, worker-egress, llm]
networks:
  llm:
    name: <the gateway project's network>
    external: true
```

### 7. Tokens

A token is bound to one workspace and a set of actions. It is printed once and stored only
as a hash.

| Action | MCP tools it allows |
|---|---|
| `create` | `save_session`, `report_mistake` |
| `read` | `search`, `brief`, `warnings`, `repo_facts`, `guard_check` (deprecated) |
| `update` | `update_fact`, `flag_stale` |
| `delete` | `retract` |

```sh
treant token create acme --label "laptop" --actions create,read,update,delete </dev/null
treant token list acme
treant token revoke <token id>
```

Give a client only what it needs: an agent that only reads memory gets `read`; an
automated job that saves summaries gets `create,read`. The dashboard's Tokens page creates
tokens too (shown once, after a fresh login).

### 8. Connect a client

The server speaks MCP over streamable HTTP at `https://treant.example.com/mcp` with an
`Authorization: Bearer <token>` header; any MCP client that can send that header works.

For Claude Code and Kiro, use the adapters in `client/` (see `client/README.md`): set
`url = "https://treant.example.com/mcp"` in `~/.config/treant/config.toml`, store each
workspace's token with `treant-client token set <ws>`, map folders to workspaces, then
`treant-client install`. The client also installs the command guard (`guard/README.md`),
which works without the server.

## Operations

All `treant` commands run through `/usr/local/sbin/treant` (the `admin` container). It
reads stdin: inside scripts and loops, give each call `</dev/null`.

### Health and monitoring

- `GET /healthz` on the server (`127.0.0.1:8000`) and the dashboard (`127.0.0.1:8001`)
  answers `ok` without a token. It shows the process is serving; it does not test the
  database or the embedder.
- The `db`, `server` and `dashboard` containers have Docker healthchecks; the worker has
  none (no port). Watch it through its jobs instead:
  - `treant jobs list --status dead`: jobs that ran out of attempts. After fixing the cause,
    `treant jobs retry --status dead [--kind K] [--workspace W]` (or one job by id).
  - `docker compose logs worker`: stage, job ids and error categories only, never content.
- Stamps for your alerting: `/var/lib/treant-backup/backup-ok` and
  `/var/lib/treant-isolation/isolation-ok` hold the epoch time of the last successful run.
  Alert when either is older than about 36 hours.
- `control.metrics` holds `isolation.faults` and `isolation.checks` per run plus search
  measurements; the dashboard's Measurements page shows them.
- Review queue and quarantine: `treant general list --status pending`,
  `treant quarantine list`, or the dashboard's home page counts.
- Stale-fact flags: `treant stale list <ws>`, or the dashboard's Stale page
  (`/memory/<ws>/stale`, workspaces only) with "Still true" and "Retract".

Job failure categories:

| Category | Meaning | Attempts |
|---|---|---|
| `model_unavailable` | gateway down, timed out, 429/5xx, or no URL/key configured | distill 12, gate 20 |
| `model_rejected` | a 4xx from the gateway, such as a bad key or unknown model | distill 12, gate 20 |
| `model_bad_reply` | the distill model answered without a JSON object | 3 |
| `judge_unavailable` | Jev or the LLM judge down, or Jev on without a key | 20 |
| `judge_rejected` | a judge refused the request (4xx) | 3 |
| `judge_bad_reply` | the LLM judge's reply held no `{"ok": ...}` | 3 |
| `settings_invalid` | a workspace's settings row does not parse | 20 |
| `embedder`, `internal` | embedding service down; anything else | embed 8, distill and apply 12, gate 20 |

### Backups

`deploy/scripts/backup.sh` takes one `pg_dump -Fc --schema=<s>` per schema (`control`,
`general` and every `ws_*`, archived ones included), all from one exported snapshot so
references between schemas stay consistent, checks each with `pg_restore --list`, and
pushes each to its own encrypted restic repository `<BACKUP_REPO_BASE>/<schema>`. One
repository per schema is what lets a purge delete one workspace's history. Retention is
`BACKUP_KEEP`, applied per schema.

```sh
install -d -m 0755 /var/lib/treant-backup
cp deploy/systemd/treant-backup.* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now treant-backup.timer
systemctl start treant-backup.service       # first run now
journalctl -u treant-backup.service -n 50
```

`BACKUP_REPO_BASE` can be a local path (a mounted backup disk; set `RequiresMountsFor=` in
the unit) or any restic repository URL (`sftp:`, `s3:`, `rest:`). Keep `RESTIC_PASSWORD`
somewhere off the host. The backup, restore check and purge share a lock file, so they
never overlap.

If you prefer a plain dump, `docker compose exec -T db pg_dump -U postgres -d treant -Fc
> treant.dump` backs up everything at once; you lose per-workspace deletion of history.

### Restore check

Run it after the first backup and then regularly:

```sh
/opt/treant/deploy/scripts/restore-check.sh
```

It restores every schema's latest snapshot into a scratch database
(`treant_restore_check`, same cluster, dropped afterwards) and compares per-table row
counts with the dump's own counts. Output is table names and counts only; `pg_restore`
errors are withheld because they can quote rows (`RESTORE_CHECK_SHOW_ERRORS=1` prints
them, on your own terminal).

### Full restore into a fresh stack

1. Deploy as above through step 5 (bootstrap and the same workspaces), then stop the
   writers: `docker compose stop server worker dashboard`.
2. Load the backup settings without printing them: `set -a; . /etc/treant/backup.env; set +a`.
3. For each schema, fetch its latest dump:
   `RESTIC_REPOSITORY=$BACKUP_REPO_BASE/<schema> restic dump --host treant --tag treant-<schema> latest /var/backups/treant/<schema>.dump > /root/<schema>.dump`
4. Empty the tables bootstrap and `workspace create` filled (a data-only restore on top of
   them fails on duplicate keys). Truncate all schemas together:

   ```sh
   docker compose exec -T db psql -U postgres -d treant -v ON_ERROR_STOP=1 -c "DO \$\$ DECLARE t text; BEGIN FOR t IN SELECT format('%I.%I', schemaname, tablename) FROM pg_tables WHERE schemaname IN ('control','general') OR schemaname LIKE 'ws\_%' LOOP EXECUTE 'TRUNCATE ' || t || ' CASCADE'; END LOOP; END \$\$"
   ```

5. Restore data only, `control` first, then `general`, then each `ws_*`:
   `docker compose exec -T db pg_restore -U postgres -d treant --data-only --disable-triggers --single-transaction < /root/<schema>.dump`
6. `treant upgrade`, then `docker compose up -d`, then the checks from step 6 and
   `treant isolation-test`.
7. `shred -u /root/*.dump` or at least `rm` them: they hold workspace data.

Rehearse this on a scratch host before you need it; `restore-check.sh` is the routinely
tested path.

### Nightly isolation test

`treant isolation-test` checks, for every ordered pair of active workspaces, that search,
brief, guard, graph walks and a direct schema read from one workspace cannot reach the
other's canary items, and that no canary text appears in `general`. It plants nothing:
every workspace needs a canary first (`treant canary plant <ws> --if-missing`, part of
step 5; do it for every new workspace). Exit 0 is clean, 1 means a fault, printed as
check, source and target workspace.

```sh
install -d -m 0755 /var/lib/treant-isolation
cp deploy/systemd/treant-isolation.* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now treant-isolation.timer
systemctl start treant-isolation.service
```

A failing run leaves the stamp stale; treat it as an incident, not a flaky test.

### Archiving and purging a workspace

```sh
treant workspace archive acme </dev/null           # revokes its tokens; data stays
/opt/treant/deploy/scripts/purge.sh acme           # irreversible; asks you to type the name
```

The purge needs an archived workspace. It drops the schema and role and deletes the
workspace's rows in `control` (tokens, jobs, queue, canaries, metrics), deletes its restic
repository `<BACKUP_REPO_BASE>/ws_acme` when that is a local path, and records the result
with `treant workspace purge-verify acme --repo-gone yes|no` (exit 0 only when schema,
role and repository are all gone). For a remote repository, delete it with your backend's
tools, then run that `purge-verify` command yourself. Kept on purpose: audit rows, and the
items it published to `general` with their provenance. Older snapshots of the `control`
and `general` repositories still hold its queue rows until retention expires them.
Running the script again after an interruption finishes the remaining steps.

### Key and secret rotation

- Workspace token: create a new one, put it in the client, then
  `treant token revoke <old id>`.
- Workspace gateway key: create the new key on the gateway, replace
  `TREANT_LITELLM_KEY_<WS>` in `worker.env`, `docker compose up -d --force-recreate worker`,
  then revoke the old key on the gateway.
- Jev key: replace `TREANT_JEV_API_KEY`, recreate the worker.
- Login password: set the new value in `TREANT_LOGIN_PASSWORD` (`admin.env`) and in
  `TREANT_DATABASE_URL` (`treant.env`), run `treant bootstrap`, then
  `docker compose up -d --force-recreate server worker dashboard`.
- Superuser password: Postgres reads `POSTGRES_PASSWORD` only when it creates the cluster.
  Change it in the database first, interactively so it never lands in a command line:
  `docker compose exec db psql -U postgres`, then `\password postgres`. Then update
  `db.env` and `TREANT_ADMIN_DATABASE_URL` in `admin.env`.
- OIDC client secret: change it at the provider and in `dashboard.env`, recreate the
  dashboard.
- Session secret: replace it and recreate the dashboard; every session ends.

### Upgrades

```sh
cd /opt/treant && git pull                 # or unpack the new release
docker compose build server
docker compose stop worker                 # no jobs against a half-upgraded schema
treant upgrade                             # idempotent schema upgrade of control, general, every workspace
docker compose up -d                       # recreates server, worker, dashboard on the new image
```

Take a backup first. `treant upgrade` is safe to run when nothing changed. Read the
release notes for one-off owner commands (for example `treant distill backfill <ws>
--dry-run` or `treant restore <ws> --dry-run`).

### Everyday owner commands

```
treant workspace list | show <ws> | set <ws> ... | archive <ws>
treant general list [--status pending|waiting_review|blocked|published|dropped|stale] [--workspace W]
treant general show <id> | approve <id> [--text "..."] [--yes] [--dry-run] | drop <id>
treant quarantine list [--workspace W] | show|release|drop <ws> <revision>
treant merges list <ws> | conflicts list <ws> | duplicates <ws|general>
treant stale list|show|keep|retract <ws> [id]
treant jobs list [--status dead] | retry <id> | retry --status dead [--kind K] [--workspace W]
treant canary list
```

`stale keep` marks a flagged fact as still right (it is not flagged again until its text changes);
`stale retract` retracts the fact. Neither is ever done automatically.

Every owner action that changes something writes `control.audit` with actor `cli`;
dashboard actions use `dashboard:<user>`. `general show` and `quarantine show` print stored text: run them in
your own terminal, not in logged automation.

## Security notes

- Tokens are shown once (`token create`, or the dashboard's no-store page) and stored only
  hashed. Never pass a token on a command line; the client prompts for it.
- Least privilege by construction: the running processes use the login role, which cannot
  create roles or bypass row-level security and reaches one workspace per transaction. Only
  the one-shot `admin` container has the superuser DSN. Only the worker holds model keys.
- Network: the server cannot open outbound connections or resolve external names; only the
  worker (gateway, Jev) and the dashboard (OIDC provider) have egress. Keep the embedding
  service and Postgres unpublished.
- Keep the dashboard private, behind OIDC with a second factor, on a host name that is not
  reachable from the internet. Do not use `TREANT_DASHBOARD_AUTH=none` on anything shared.
- Env files: root-owned, `0600` (`treant.env` may be `0640`), outside the repository
  checkout, never committed. The examples carry placeholders only.
- What leaves the host: with the default settings, distilled text goes to your LLM gateway
  and general candidates go to the Jev endpoint. Use `--backend none` for any workspace
  whose content must stay local.
- Logs carry categories, ids and counts, never note text, prompts, replies or keys. The
  dashboard has no access log because the OIDC callback's query string carries the code.
- Never run `docker volume prune` on the host: the database lives in the `pgdata` volume.
