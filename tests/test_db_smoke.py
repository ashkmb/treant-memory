import conftest
import psycopg
import pytest


def test_pg_fixture_creates_database(pg):
    admin_dsn, login_dsn, prefix = pg
    with psycopg.connect(admin_dsn) as conn:
        assert conn.execute("select current_database()").fetchone()[0].startswith("treant_test_")
    assert login_dsn.count(f"{prefix}_login") == 1


def test_login_dsn_connects_as_the_login_role(db, login_dsn, prefix):
    with psycopg.connect(login_dsn) as conn:
        user, dbname = conn.execute("select current_user, current_database()").fetchone()
    assert user == f"{prefix}_login"
    assert dbname.startswith("treant_test_")


def test_pg_not_ready_skips_instead_of_erroring(monkeypatch):
    def refuse(*a, **k):
        raise psycopg.OperationalError("the database system is starting up")

    monkeypatch.setattr(conftest, "_ready", [])
    monkeypatch.setattr(conftest, "_port_open", lambda: True)
    monkeypatch.setattr(conftest.psycopg, "connect", refuse)
    monkeypatch.setattr(conftest.time, "sleep", lambda s: None)
    with pytest.raises(pytest.skip.Exception):
        conftest._require_pg()
