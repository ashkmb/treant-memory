import pytest

from treant import stale
from treant.errors import InvalidInput

TEXT = "Entry point is `old_name.py`; helpers in `keep.py` and `load_config()`."


def test_constants():
    assert stale.MAX_MISSING == 10
    assert stale.MAX_REPO_FACTS == 200
    assert stale.MAX_REPO == 200
    assert stale.MAX_SUBJECT == 200
    assert (stale.TOKEN_MIN, stale.TOKEN_MAX) == (3, 120)
    assert stale.PART_OF_DEPTH == 5


def test_clean_missing_keeps_tokens_from_the_fact_text_only():
    out = stale.clean_missing(
        [{"token": "old_name.py", "commit": "abc1234", "subject": "rename old_name to new_name"}],
        TEXT,
        [],
    )
    assert out == [
        {"token": "old_name.py", "commit": "abc1234", "subject": "rename old_name to new_name"}
    ]
    with pytest.raises(InvalidInput):  # not in the fact text: file contents never pass
        stale.clean_missing([{"token": "def secret_helper"}], TEXT, [])


def test_clean_missing_caps_at_ten_and_validates_fields():
    text = " ".join(f"file{i:02d}.py" for i in range(12))
    many = [{"token": f"file{i:02d}.py", "commit": "abc1234", "subject": "s"} for i in range(12)]
    out = stale.clean_missing(many, text, [])
    assert len(out) == stale.MAX_MISSING
    assert [m["token"] for m in out] == [f"file{i:02d}.py" for i in range(10)]

    long_ok = "x" * 120
    short = "ab"
    long_bad = "x" * 121
    big_text = f"{long_ok} {short} {long_bad}"
    assert stale.clean_missing([{"token": long_ok}], big_text, [])[0]["token"] == long_ok
    for bad in (short, long_bad):
        with pytest.raises(InvalidInput):
            stale.clean_missing([{"token": bad}], big_text, [])
    with pytest.raises(InvalidInput):
        stale.clean_missing([{"token": 123}], TEXT, [])
    with pytest.raises(InvalidInput):
        stale.clean_missing([{}], TEXT, [])

    with pytest.raises(InvalidInput):
        stale.clean_missing([{"token": "keep.py", "commit": "xyz"}], TEXT, [])
    with pytest.raises(InvalidInput):
        stale.clean_missing([{"token": "keep.py", "commit": 1234567}], TEXT, [])
    with pytest.raises(InvalidInput):
        stale.clean_missing([{"token": "keep.py", "subject": 5}], TEXT, [])

    none_ok = stale.clean_missing([{"token": "keep.py", "commit": None, "subject": None}], TEXT, [])
    assert none_ok == [{"token": "keep.py", "commit": None, "subject": None}]
    absent = stale.clean_missing([{"token": "keep.py"}], TEXT, [])
    assert absent == [{"token": "keep.py", "commit": None, "subject": None}]

    one_line = stale.clean_missing([{"token": "keep.py", "subject": "first\nsecond"}], TEXT, [])
    assert one_line[0]["subject"] == "first"
    capped = stale.clean_missing([{"token": "keep.py", "subject": "a" * 300}], TEXT, [])
    assert capped[0]["subject"] == "a" * 200

    upper = stale.clean_missing([{"token": "keep.py", "commit": "ABC1234"}], TEXT, [])
    assert upper[0]["commit"] == "abc1234"

    for bad in ("keep.py", {"token": "keep.py"}, None, [], ["keep.py"], [None]):
        with pytest.raises(InvalidInput):
            stale.clean_missing(bad, TEXT, [])


def test_clean_missing_duplicate_token_keeps_first():
    out = stale.clean_missing(
        [
            {"token": "keep.py", "commit": "abc1234", "subject": "first"},
            {"token": "keep.py", "commit": "def5678", "subject": "second"},
        ],
        TEXT,
        [],
    )
    assert out == [{"token": "keep.py", "commit": "abc1234", "subject": "first"}]


def test_clean_missing_scrubs_the_subject():
    out = stale.clean_missing(
        [
            {
                "token": "keep.py",
                "commit": "abc1234",
                "subject": "rotate ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
            }
        ],
        TEXT,
        [],
    )
    assert "ghp_" not in (out[0]["subject"] or "")  # unsure scrub -> subject None


def test_clean_missing_unsure_scrub_drops_the_subject_but_keeps_the_flag():
    noisy = " ".join(f"ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r{i}" for i in range(20))
    out = stale.clean_missing(
        [{"token": "keep.py", "commit": "abc1234", "subject": noisy[: stale.MAX_SUBJECT]}],
        TEXT,
        [],
    )
    assert len(out) == 1
    assert out[0]["token"] == "keep.py"
    assert out[0]["subject"] is None or "ghp_" not in out[0]["subject"]


def test_marker_text():
    m = [{"token": "check_job.py", "commit": None, "subject": None}]
    assert stale.marker(m, "abc1234def") == "(in doubt: check_job.py not found at abc1234)"
    assert (
        stale.marker(m * 3, "abc1234def")
        == "(in doubt: check_job.py and 2 more not found at abc1234)"
    )


def test_valid_commit_and_repo():
    assert stale.valid_commit("ABC1234") == "abc1234"
    assert stale.valid_commit("a" * 64) == "a" * 64
    for bad in ("abc123", "a" * 65, "xyz1234", "", None, 1234567, "abc1234\n"):
        with pytest.raises(InvalidInput):
            stale.valid_commit(bad)
    assert stale.valid_repo("acme/app") == "acme/app"
    assert stale.valid_repo("r" * 200) == "r" * 200
    for bad in ("", "a\nb", "r" * 201, None, 5):
        with pytest.raises(InvalidInput):
            stale.valid_repo(bad)


def test_text_sha256():
    assert stale.text_sha256("") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert stale.text_sha256("a") != stale.text_sha256("b")
