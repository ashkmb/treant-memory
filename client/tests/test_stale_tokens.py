import pytest

from treant_client.stale_tokens import CHECKED_TYPES, Token, checkable, extract_tokens

CASES = [
    # (text, expected raw tokens)
    (
        "The hourly check lives in `src/app/jobs/check_job.py`; it runs hourly.",
        ["src/app/jobs/check_job.py"],
    ),
    ("Config is parsed by `load_config()` in the client.", ["load_config()"]),
    ("`TreantSession` opens the MCP session and sends initialize.", ["TreantSession"]),
    ("Brief markers come from `treant.stale.open_markers`.", ["treant.stale.open_markers"]),
    (
        "The page template is src/app/templates/conflicts.html now.",
        ["src/app/templates/conflicts.html"],
    ),
    ("See src/app/main.py.", ["src/app/main.py"]),
    ("The crash is at src/app/main.py:42 (see logs).", ["src/app/main.py"]),
    ("Set `TREANT_STATE_DIR` to move the state folder.", ["TREANT_STATE_DIR"]),
    ("Release gate is `.github/workflows/ci.yml`.", [".github/workflows/ci.yml"]),
    ("Install steps are in `README.md`.", ["README.md"]),
    ("`brief.py` caps output; `brief.py` also skips canaries.", ["brief.py"]),
    ("`git_toplevel()` is used, not ../other/repo/file.py.", ["git_toplevel()"]),
    # false cases
    ("Run tests with `make test` and `uv run pytest -q`.", []),
    ("Docs: https://example.com/docs/setup.md and `https://example.com/a/b.py`.", []),
    ("Mirror at example.com/acme/tool/blob/main/src/app.py is stale.", []),
    ("Caddy serves `agent.example.com` on the LAN.", []),
    ("Pinned `pgvector 0.8.6`, tag `v0.1.0`, Python `3.12`.", []),
    ("Edit `/etc/caddy/Caddyfile` and `~/.config/acme/config.toml` on the host.", []),
    ("We use `uv` and `ruff` for linting.", []),
    ("Owner prefers short answers in the personal workspace.", []),
    ("Old token `ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8` was rotated.", []),
    ("Pass `--dry-run` first.", []),
    ("Globs like `src/*.py` and `{name}.py` are not paths.", []),
    ("The `[removed]` value lived here.", []),
    ("Two letters `ab` are too short.", []),
    # extra rows
    ("The service unit is `deploy/acme.service`.", ["deploy/acme.service"]),
    ("Both `src/app/main.py` and src/app/main.py name one file.", ["src/app/main.py"]),
    (
        "See (src/app/cli.py) and docs/setup.md, then `parseArgs`.",
        ["src/app/cli.py", "docs/setup.md", "parseArgs"],
    ),
    ("Tracked as `alpha.beta.com` upstream.", []),
    ("Relative ../acme/app.py and ~/acme/app.py are skipped.", []),
]


@pytest.mark.parametrize("text,expected", CASES)
def test_extract_tokens_fixture_table(text, expected):
    assert [t.raw for t in extract_tokens(text)] == expected


def test_needles():
    (tok,) = extract_tokens("Config is parsed by `load_config()` in the client.")
    assert tok == Token("load_config()", "ident", "load_config")

    (tok,) = extract_tokens("The crash is at src/app/main.py:42 (see logs).")
    assert tok == Token("src/app/main.py", "path", "src/app/main.py")

    (tok,) = extract_tokens("Run `./scripts/run.sh` to deploy.")
    assert tok == Token("./scripts/run.sh", "path", "scripts/run.sh")

    (tok,) = extract_tokens("Everything lives under `src/app/`.")
    assert tok == Token("src/app/", "path", "src/app")

    (tok,) = extract_tokens("Brief markers come from `treant.stale.open_markers`.")
    assert tok == Token("treant.stale.open_markers", "ident", "treant.stale.open_markers")

    (tok,) = extract_tokens("Call `acme.client.connect()` first.")
    assert tok == Token("acme.client.connect()", "ident", "acme.client.connect")


def test_backticked_line_suffix_needle():
    (tok,) = extract_tokens("Crash at `src/app/main.py:42`.")
    assert tok.kind == "path"
    assert tok.needle == "src/app/main.py"


def test_at_most_ten_tokens():
    text = " ".join(f"`alpha_name_{i}`" for i in range(15))
    toks = extract_tokens(text)
    assert [t.raw for t in toks] == [f"alpha_name_{i}" for i in range(10)]


def test_host_terms_scrubbed():
    text = "Client is `acmecorp.Client.io` and `beta_cfg`."
    assert [t.raw for t in extract_tokens(text)] == ["acmecorp.Client.io", "beta_cfg"]
    assert [t.raw for t in extract_tokens(text, ["acmecorp"])] == ["beta_cfg"]


def test_every_raw_is_a_substring_of_the_text():
    for text, _ in CASES:
        for tok in extract_tokens(text):
            assert tok.raw in text
            assert 3 <= len(tok.raw) <= 120


def test_long_span_skipped():
    long = "a_" + "b" * 130
    assert extract_tokens(f"Name `{long}` here.") == []


def test_checked_types():
    assert "change" not in CHECKED_TYPES
    assert "dead_end" not in CHECKED_TYPES
    assert {"fact", "decision", "open_thread"} <= CHECKED_TYPES


@pytest.mark.parametrize(
    "ftype,text,ok",
    [
        ("fact", "Entry is `old_name.py`.", True),
        ("change", "Entry is `old_name.py`.", False),
        ("dead_end", "Tried `old_name.py`.", False),
        ("fact", "Renamed `check_job.py` to `hourly_check.py`.", False),
        ("decision", "`old_name.py` is no longer used.", False),
        ("fact", "It USED TO live in `alpha.py`.", False),
        ("warning", "Use `beta.py` instead of the old one.", False),
        ("fact", "The `removed_count` field is reported.", True),
        ("fact", "Unremoved items stay in `alpha.py`.", True),
        ("procedure", "Files moved to `beta/` last week.", False),
        ("preference", "The ruler moved too far in `alpha.py`.", True),
    ],
)
def test_checkable(ftype, text, ok):
    assert checkable(ftype, text) is ok
