import pytest

from treant.scrub import Scrubbed, scrub

R = "[removed]"


def _one(text: str) -> Scrubbed:
    out = scrub(text)
    assert R in out.text, out.text
    assert out.removed >= 1
    return out


@pytest.mark.parametrize(
    "secret",
    [
        "sk-" + "a1" * 20,
        "sk-ant-" + "Ab3" * 12,
        "ghp_" + "A1b2" * 9,
        "gho_" + "A1b2" * 9,
        "github_pat_" + "A1b2C3" * 6,
        "glpat-" + "x9" * 10,
        "xoxb-" + "1234-" + "abcd" * 3,
        "xoxp-" + "1234-" + "abcd" * 3,
        "AKIA" + "ABCDEFGH12345678",
        "ASIA" + "ABCDEFGH12345678",
        "ATATT" + "3xFfGF0" + "z" * 10,
    ],
)
def test_known_prefix_tokens(secret):
    out = _one(f"the key is {secret} ok")
    assert secret not in out.text
    assert out.text == f"the key is {R} ok"
    assert out.removed == 1


def test_pem_block():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nline one\nline two\n-----END RSA PRIVATE KEY-----"
    out = scrub(f"before\n{pem}\nafter")
    assert out.text == f"before\n{R}\nafter"
    assert out.removed == 1


def test_jwt():
    jwt = "eyJ" + "a" * 12 + "." + "eyJ" + "b" * 12 + "." + "c" * 12
    out = scrub(f"token {jwt} end")
    assert jwt not in out.text
    assert R in out.text


def test_bearer():
    out = scrub("Authorization: Bearer " + "abc.def-ghi_jkl" * 2)
    assert "abc.def" not in out.text
    assert R in out.text


def test_credentials_in_url():
    out = scrub("clone https://bob:hunter2@git.example.org/repo.git now")
    assert "hunter2" not in out.text and "bob" not in out.text
    assert "git.example.org/repo.git" in out.text
    assert out.removed == 1


@pytest.mark.parametrize(
    "line",
    [
        "password: hunter2",
        "passwd=hunter2",
        "SECRET = hunter2",
        'token: "hunter2"',
        "api_key=hunter2",
        "api-key: hunter2",
        "apikey=hunter2",
    ],
)
def test_key_value(line):
    out = scrub(line)
    assert "hunter2" not in out.text
    assert R in out.text


def test_secret_in_url_query():
    out = scrub("see https://x.io/a?id=5&token=" + "abc123" * 3 + "&page=2 for more")
    assert "abc123" not in out.text
    assert out.text.startswith("see https://x.io/a?id=5&token=")
    assert "&page=2 for more" in out.text
    assert out.removed == 1


@pytest.mark.parametrize("name", ["key", "secret", "password", "sig", "signature", "access_token"])
def test_secret_query_names(name):
    out = scrub(f"https://x.io/a?{name}=zzz999")
    assert "zzz999" not in out.text


def test_secret_in_code_block():
    text = (
        "```bash\nexport API_KEY=hunter2\ncurl -H 'Authorization: Bearer "
        + ("q" * 24)
        + "' https://api.example.com\n```"
    )
    out = scrub(text)
    assert "hunter2" not in out.text
    assert "q" * 24 not in out.text
    assert out.text.startswith("```bash\n") and out.text.endswith("\n```")
    assert "https://api.example.com" in out.text


def test_ipv4():
    out = scrub("host 10.0.12.255 and 192.0.2.1.")
    assert out.text == f"host {R} and {R}."
    assert out.removed == 2


def test_ipv4_not_versions():
    text = "versions 1.13.0 and 0.8.8 and 1.2.3.4.5 build 300.1.1.1"
    out = scrub(text)
    assert "1.13.0" in out.text and "0.8.8" in out.text
    assert "300.1.1.1" in out.text


def test_ipv6():
    out = scrub("addr 2001:db8::1 and fe80::1ff:fe23:4567:890a here")
    assert out.text == f"addr {R} and {R} here"


def test_ipv6_not_times():
    text = "at 03:00 and 12:30:45 UTC"
    assert scrub(text).text == text


def test_email():
    out = scrub("mail bob.smith+x@example.co.uk now")
    assert out.text == f"mail {R} now"


def test_aws_account_id():
    out = scrub("account 123456789012 is prod")
    assert out.text == f"account {R} is prod"


def test_long_digit_runs_other_than_12_kept():
    text = "id 1234567890 and 12345678901234"
    assert scrub(text).text == text


@pytest.mark.parametrize(
    "host",
    [
        "svc.prod.internal",
        "nas.local",
        "box.lan",
        "router.home.arpa",
    ],
)
def test_internal_hostnames(host):
    out = scrub(f"connect to {host} please")
    assert out.text == f"connect to {R} please"


@pytest.mark.parametrize("host", ["db1.acme-dataplace.com", "git.acme-dataplace.com"])
def test_workspace_host_term_removed(host):
    out = scrub(f"connect to {host} please", terms=["acme"])
    assert out.text == f"connect to {R} please"


def test_no_terms_keeps_unrelated_hosts():
    text = "open https://api.example-acme.io/v1 and db1.acme-dataplace.com"
    assert scrub(text, terms=[]).text == text
    assert scrub("open https://api.example-acme.io/v1").text == (
        "open https://api.example-acme.io/v1"
    )


def test_host_term_is_escaped():
    out = scrub("reach a.b.example and axb.example now", terms=["a.b"])
    assert "axb.example" in out.text
    assert out.text == f"reach {R} and axb.example now"


def test_long_blob():
    blob = "Zk3" * 14
    out = scrub(f"value={blob} and `{blob}`")
    assert blob not in out.text
    assert out.removed == 2


def test_long_run_needs_digit_and_letter():
    text = "word " + "a" * 40 + " digits " + "1" * 40
    assert scrub(text).text == text


def test_long_url_path_survives():
    text = "see https://example.com/" + "Ab1" * 20 + "/page for details"
    assert scrub(text).text == text


def test_plain_prose_unchanged():
    text = (
        "## Question\nWhich release fixed the leak in Foo?\n\n"
        "## Findings\n- Foo 0.8.8 fixed it; v1.13.0 added tests (2026-10-30).\n"
        "- Run `pip install foo==0.8.8` at 03:00 or 12:30:45.\n"
        "- Python 3.13.1 docs: https://docs.python.org/3/library/re.html\n\n"
        "## Sources\n- https://docs.python.org/3/\n"
        "- https://github.com/org/repo/releases/tag/v1.13.0\n"
    )
    out = scrub(text)
    assert out.text == text
    assert out.removed == 0
    assert out.confident is True


def test_many_secrets_not_confident():
    text = "\n".join("ghp_" + "A1b2" * 9 + f" n{i}" for i in range(13))
    out = scrub(text)
    assert out.removed == 13
    assert out.confident is False


def test_twelve_secrets_confident():
    text = "\n".join("ghp_" + "A1b2" * 9 + f" n{i}" for i in range(12))
    out = scrub(text)
    assert out.removed == 12
    assert out.confident is True


def test_removed_marker_is_stable():
    out = scrub("x " + R + " y")
    assert out.text == "x [removed] y" and out.removed == 0


@pytest.mark.parametrize(
    "line",
    [
        "SECRET_KEY=hunter2",
        "private_key=hunter2",
        "access_key=abc123",
        "client_secret: x1y2z3",
        "AWS_SECRET_ACCESS_KEY=short1",
        "db-password: hunter2",
        "GITHUB_TOKEN=hunter2",
    ],
)
def test_compound_key_names(line):
    out = scrub(line)
    assert "hunter2" not in out.text and "abc123" not in out.text
    assert "x1y2z3" not in out.text and "short1" not in out.text
    assert out.removed == 1
    assert out.text.endswith(R)


def test_quoted_value_with_spaces():
    out = scrub('password: "my secret pass phrase" and more')
    assert out.text == f"password: {R} and more"
    out = scrub("secret='two words' next")
    assert out.text == f"secret={R} next"


def test_url_credentials_empty_user():
    out = scrub("redis://:pw@cache.example.org:6379/0")
    assert "pw@" not in out.text
    assert "cache.example.org:6379/0" in out.text


def test_unterminated_pem_to_end_of_text():
    out = scrub("intro\n-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\nkqhkiG9w0B")
    assert out.text == f"intro\n{R}"


def test_unterminated_pem_stops_at_fence():
    text = "```\n-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1r\n```\nafter"
    out = scrub(text)
    assert out.text == f"```\n{R}\n```\nafter"


def test_bearer_prose_survives():
    text = "Use Bearer authentication for the API."
    assert scrub(text).text == text


def test_bearer_long_without_digit_removed():
    out = scrub("Bearer " + "abcdefghij" * 2)
    assert out.text == f"Bearer {R}"


@pytest.mark.parametrize(
    "text",
    [
        "/usr/local/lib/python3/site-packages/foo",
        "test_some_very_long_function_name_for_case_2",
        "my-long-article-slug-about-things-2026-release-notes",
    ],
)
def test_blob_rule_spares_paths_and_slugs(text):
    full = f"see {text} here"
    assert scrub(full).text == full


def test_blob_removes_mixed_case_and_hex():
    mixed = "Qw3rTy9" * 6
    hexed = "0123456789abcdef" * 4
    out = scrub(f"a {mixed} b {hexed} c")
    assert out.text == f"a {R} b {R} c"


@pytest.mark.parametrize("name", ["settings.local.json", ".env.local", "next.config.local"])
def test_local_files_survive(name):
    text = f"edit {name} now"
    assert scrub(text).text == text


def test_real_local_hosts_still_removed():
    out = scrub("nas.local, db.internal and router.lan.")
    assert out.text == f"{R}, {R} and {R}."


def test_second_pass_residue_not_confident():
    # stripping an inner span exposes a new match on the second pass
    text = "10.0.0.1" + "AKIA" + "ABCDEFGH12345678"
    out = scrub(text)
    assert out.removed >= 1
    assert scrub(out.text).removed > 0
    assert out.confident is False


def test_clean_output_is_confident_and_idempotent():
    out = scrub("password=hunter2")
    assert scrub(out.text).removed == 0
    assert out.confident is True


# --- review round: more token shapes -------------------------------------------------

TG = "123456789" + ":" + "AA" + "Hd9_x-" * 5 + "Zq3"  # 9 digits : 35 chars
GOOGLE = "AIza" + "Sy" + "B4c_D-e" * 4 + "F5g6h"  # AIza + 35 chars


def test_fake_secret_shapes():
    assert len(TG.split(":")[1]) == 35
    assert len(GOOGLE) == 4 + 35


@pytest.mark.parametrize(
    "secret",
    [
        TG,
        GOOGLE,
        "hf_" + "AbCdEfGhIjKlMnOpQrStUvWxYz" + "aBcDeFgH",
        "sk_live_" + "51HxYz" + "Ab12Cd34Ef56Gh78",
        "rk_live_" + "51HxYz" + "Ab12Cd34Ef56Gh78",
        "sk_test_" + "51HxYz" + "Ab12Cd34Ef56Gh78",
        "glsa_" + "Ab3dEf6hIj9lMn2pQr5tUv8xYz1bCd4f" + "_" + "0a1b2c3d",
        "npm_" + "Ab3dEf6hIj9lMn2pQr5tUv8xYz1bCd4fGh7j",
        "pypi-" + "AgEIcHlwaS5vcmc" + "CJGE3ZjQ5Yi1h" * 4 + "_-xY",
        "Ab3_" * 9,
        "x9-Kq2_Lm8Zp4-Wn7Rt5_Yb6Vc3-Ud1Se0F",
    ],
)
def test_more_token_shapes_removed(secret):
    out = _one(f"use {secret} now")
    assert secret not in out.text
    assert out.text == f"use {R} now"
    assert out.removed == 1


def test_telegram_token_in_bot_url():
    out = scrub(f"curl https://api.telegram.org/bot{TG}/getMe")
    assert TG not in out.text
    assert out.text == f"curl https://api.telegram.org/bot{R}/getMe"


@pytest.mark.parametrize(
    "text",
    [
        "test_some_very_long_function_name_for_case_2",
        "my-long-article-slug-about-things-2026-release-notes",
        "My-Long-Article-Slug-About-Things-2026-Release-Notes",
        "Some_Python_Identifier_For_Case_42_And_More",
        "test_FooBar_handles_case_42_correctly_today",
        "MAX_CONFIDENT_REMOVALS_FOR_THE_V2_RELEASE",
        "hf_hub_download_function_from_the_library",
        "npm_config_cache_directory_setting_name",
        "/usr/local/lib/python3/site-packages/Foo_Bar_Module_V2",
        "the timestamp 12345678:30 and ratio 1:2",
    ],
)
def test_identifiers_slugs_and_paths_stay(text):
    full = f"see {text} here"
    out = scrub(full)
    assert out.text == full and out.removed == 0


@pytest.mark.parametrize(
    "line,secret",
    [
        ("the password is " + "hunter2hunter2", "hunter2hunter2"),
        ("token " + "abc123def456ghi", "abc123def456ghi"),
        ("The API key is " + "Zx81-k0Lp-99Qa", "Zx81-k0Lp-99Qa"),
        ("my secret IS " + "s3cr3t.v4lue.long", "s3cr3t.v4lue.long"),
        ("GITHUB_TOKEN " + "abcdefgh12345", "abcdefgh12345"),
    ],
)
def test_key_value_with_space_or_is(line, secret):
    out = scrub(line + " ok")
    assert secret not in out.text
    assert out.text.endswith(f"{R} ok")
    assert out.removed == 1


@pytest.mark.parametrize(
    "text",
    [
        "the token is used for login",
        "the password is shorter1",
        "a secret is stored at /etc/app/secret2026.conf now",
        "the key is understanding the problem",
        "tokens are counted per 1000 requests",
        "password is required for logins",
    ],
)
def test_key_value_prose_stays(text):
    out = scrub(text)
    assert out.text == text and out.removed == 0


@pytest.mark.parametrize(
    "host",
    [
        "acme-dataplace.atlassian.net",
        "git.acme-dataplace.com",
        "acme.slack.com",
        "grafana.ops.acme-internal.io",
        "ACME-dataplace.atlassian.net",
    ],
)
def test_any_acme_hostname_removed(host):
    out = scrub(f"open https://{host}/wiki/x now", terms=["acme"])
    assert out.text == f"open https://{R}/wiki/x now"


def test_acme_word_in_prose_stays():
    text = "At Acme. The Acme team uses acme's tools."
    assert scrub(text, terms=["acme"]).text == text


@pytest.mark.parametrize(
    "value",
    ["aB3dE/fG6hI+jK9lMn==", "Zx81/k0Lp=99Qa", "q9+/W8e7R6t5Y4u3"],
)
def test_key_value_base64_with_slash_removed(value):
    out = scrub(f"the secret is {value} ok")
    assert value not in out.text
    assert out.text == f"the secret is {R} ok"
    assert out.removed == 1


@pytest.mark.parametrize(
    "text",
    [
        "the token is at /var/lib/app2/token-file now",
        "token https://example.com/a?b=1234567890 here",
    ],
)
def test_key_value_paths_and_urls_with_slash_stay(text):
    assert scrub(text).text == text


def test_scrub_matches_harness_rules():
    cases = [
        "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
        "https://bob:hunter2@git.example.org/r.git",
        "sk-" + "a1" * 20,
        "AKIA" + "ABCDEFGH12345678",
        "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12,
        "10.1.2.3",
        "123456789012",
        "bob@example.org",
    ]
    for case in cases:
        out = scrub(f"x {case} y")
        assert R in out.text, case
        assert out.removed >= 1, case


def test_scrub_terms_generalise_host_rule():
    out = scrub("see git.acme-corp.io and acme itself", terms=["acme"])
    assert out.text == f"see {R} and acme itself"


def test_scrub_not_confident_over_12():
    assert scrub("".join(f"10.0.0.{i} " for i in range(13))).confident is False
