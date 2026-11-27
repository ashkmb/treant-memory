"""Provenance formatting: dates, the earliest session and the newest confirmation."""

from datetime import UTC, datetime, timedelta, timezone

from treant.provenance import format_source, lookup


def dt(*args: int, tz=UTC) -> datetime:
    return datetime(*args, tzinfo=tz)


def test_saved_only_without_revisions():
    assert format_source(dt(2026, 11, 20, 23, 30), [], False) == {"saved": "2026-11-20"}


def test_earliest_session_newest_date():
    revs = [(dt(2026, 11, 26), "s2"), (dt(2026, 11, 21), "s1")]
    assert format_source(dt(2026, 11, 21), revs, False) == {
        "saved": "2026-11-21",
        "confirmed": "2026-11-26",
        "session": "s1",
    }


def test_general_never_has_session():
    revs = [(dt(2026, 11, 26), "s2"), (dt(2026, 11, 21), "s1")]
    assert format_source(dt(2026, 11, 21), revs, True) == {"saved": "2026-11-21"}


def test_utc_date():
    saved = dt(2026, 11, 20, 23, 30, tz=timezone(timedelta(hours=-5)))
    assert format_source(saved, [], False) == {"saved": "2026-11-21"}


def test_session_truncated():
    out = format_source(dt(2026, 11, 21), [(dt(2026, 11, 21), "x" * 200)], False)
    assert out["session"] == "x" * 80


def test_confirmed_date_in_utc():
    late = dt(2026, 11, 26, 22, 0, tz=timezone(timedelta(hours=-5)))
    out = format_source(dt(2026, 11, 21), [(late, "s1")], False)
    assert out["confirmed"] == "2026-11-27"


class _NoQuery:
    def execute(self, *args, **kwargs):
        raise AssertionError("no query expected")


def test_lookup_without_items_runs_no_query():
    assert lookup(_NoQuery(), "ws_alpha", []) == {}


def test_lookup_skips_other_schemas():
    assert (
        lookup(_NoQuery(), "ws_alpha", [("ws_beta", "fact", "x"), ("control", "fact", "y")]) == {}
    )
