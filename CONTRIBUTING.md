# Contributing

Thank you for helping. treant-memory is source-available (see the README's licence section),
and contributions are accepted under the [Contributor License Agreement](CLA.md): you keep the
copyright in your work and give the licensor, Ashkan Kamyab, a licence to use and relicense
it, including under commercial licences. Your first pull request asks you to sign it.

## Before you start

For anything larger than a small fix, open an issue first so we can agree on the approach.
Security problems go through [SECURITY.md](SECURITY.md), not public issues.

## Development

You need Python 3.12, [uv](https://docs.astral.sh/uv/) and Docker (for the test database).

```sh
docker compose -f compose.test.yml up -d    # Postgres 17 + pgvector on 127.0.0.1:55432

uv sync
uv run ruff check
uv run pytest -q

uv sync --directory client
uv run --directory client ruff check
uv run --directory client pytest -q

uv sync --directory guard
uv run --directory guard ruff check
uv run --directory guard pytest -q
```

The server's database tests skip when that Postgres is not reachable; run them before you send
a change that touches `src/`. CI runs all three suites with the database.

## Pull requests

- Keep each pull request to one change, with tests for new behaviour and bug fixes.
- `ruff check` and every test suite must pass.
- `client/` and `guard/` use the standard library only (the client depends on the guard); do not
  add runtime dependencies there.
- Never put real host names, account numbers, tokens or other private data in code, tests or
  fixtures; use `example.com`, `192.0.2.x` and made-up names.
- If you change dashboard templates or `css/app.css`, rebuild the stylesheet with
  `scripts/build-css.sh` and commit the result.
