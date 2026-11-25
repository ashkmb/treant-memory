import pytest

from treant.config import Settings


def test_from_env_requires_database_url():
    with pytest.raises(ValueError, match="TREANT_DATABASE_URL"):
        Settings.from_env({"TREANT_ADMIN_DATABASE_URL": "x", "TREANT_EMBED_URL": "y"})


def test_from_env_defaults():
    s = Settings.from_env(
        {"TREANT_DATABASE_URL": "a", "TREANT_ADMIN_DATABASE_URL": "b", "TREANT_EMBED_URL": "c"}
    )
    assert (s.role_prefix, s.embed_dims, s.bind) == ("treant", 768, "127.0.0.1:8000")


def test_missing_error_never_leaks_other_values():
    with pytest.raises(ValueError) as e:
        Settings.from_env({"TREANT_ADMIN_DATABASE_URL": "secret-dsn", "TREANT_EMBED_URL": "y"})
    assert "secret-dsn" not in str(e.value)


def test_allowed_hosts_parsed():
    base = {"TREANT_DATABASE_URL": "a", "TREANT_ADMIN_DATABASE_URL": "b", "TREANT_EMBED_URL": "c"}
    s = Settings.from_env({**base, "TREANT_ALLOWED_HOSTS": "treant.example.com, 127.0.0.1:8780"})
    assert s.allowed_hosts == ("treant.example.com", "127.0.0.1:8780")
    assert Settings.from_env(base).allowed_hosts == ()


@pytest.mark.parametrize("bad", ["", "Treant", "1x", "a-b", "x" * 25])
def test_invalid_role_prefix_rejected_at_load(bad):
    base = {"TREANT_DATABASE_URL": "a", "TREANT_ADMIN_DATABASE_URL": "b", "TREANT_EMBED_URL": "c"}
    with pytest.raises(ValueError, match="TREANT_ROLE_PREFIX"):
        Settings.from_env({**base, "TREANT_ROLE_PREFIX": bad})
