from pathlib import Path

from treant_client import config as cfgmod
from treant_client.config import load_config


def test_defaults_are_neutral(tmp_path):
    cfg = load_config(tmp_path / "nope.toml", tmp_path / "no-guard")
    assert cfg.url is None
    assert cfg.kiro_agent is None
    assert cfg.scrub_host_terms == {}
    assert cfg.folders == {}
    for name in (
        "DEFAULT_URL",
        "DEFAULT_FOLDERS",
        "DEFAULT_SCRUB_HOST_TERMS",
        "DEFAULT_KIRO_AGENT",
    ):
        assert not hasattr(cfgmod, name)


def test_owner_config_restores_old_behaviour(tmp_path):
    """A config.toml with explicit values reproduces the shape the built-in defaults had."""
    f = tmp_path / "c.toml"
    f.write_text(
        'url = "https://treant.example.com/mcp"\nkiro_agent = "me"\n'
        '[folders]\n"~/work" = "work"\n"~/work/side" = "side"\n'
        '[scrub_host_terms]\nwork = ["acme"]\n'
    )
    cfg = load_config(f, tmp_path / "no-guard")
    home = str(Path.home())
    assert cfg.url == "https://treant.example.com/mcp"
    assert cfg.kiro_agent == "me"
    assert cfg.scrub_host_terms == {"work": ["acme"]}
    assert cfg.folders == {f"{home}/work": "work", f"{home}/work/side": "side"}


def test_folders_table_replaces_defaults(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('[folders]\n"/x/one" = "one"\n')
    cfg = load_config(f, tmp_path / "no-guard")
    assert cfg.folders == {"/x/one": "one"}


def test_file_keys_override(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text(
        'url = "https://other/mcp"\nkiro_agent = "bob"\n'
        '[scrub_host_terms]\nacme = ["acme", "acme-corp"]\n'
    )
    cfg = load_config(f, tmp_path / "no-guard")
    assert cfg.url == "https://other/mcp"
    assert cfg.kiro_agent == "bob"
    assert cfg.scrub_host_terms == {"acme": ["acme", "acme-corp"]}
    assert cfg.folders == {}


def test_tilde_in_folders_is_expanded(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('[folders]\n"~/proj" = "p"\n')
    assert load_config(f, tmp_path / "no-guard").folders == {str(Path.home() / "proj"): "p"}


def _guard_toml(tmp_path, text):
    gdir = tmp_path / "g"
    gdir.mkdir()
    (gdir / "guard.toml").write_text(text)
    return gdir


def test_folders_come_from_guard_config_first(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('[folders]\n"/x/one" = "one"\n')
    gdir = _guard_toml(tmp_path, '[workspaces]\n"~/g" = "gee"\n"/y/two" = "two"\n')
    cfg = load_config(f, gdir)
    assert cfg.folders == {str(Path.home() / "g"): "gee", "/y/two": "two"}


def test_guard_config_without_workspaces_falls_back_to_folders(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('[folders]\n"/x/one" = "one"\n')
    gdir = _guard_toml(tmp_path, '[allow.one]\nkube_context = ["a"]\n')
    assert load_config(f, gdir).folders == {"/x/one": "one"}
    assert load_config(tmp_path / "nope.toml", gdir).folders == {}


def test_invalid_guard_config_falls_back_to_folders(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('[folders]\n"/x/one" = "one"\n')
    gdir = _guard_toml(tmp_path, '[workspaces\n"/y/two" = "two"\n')
    assert load_config(f, gdir).folders == {"/x/one": "one"}


def test_tilde_user_guard_key_falls_back_without_raising(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('[folders]\n"/x/one" = "one"\n')
    gdir = _guard_toml(tmp_path, '[workspaces]\n"~Documents/Projects/Work" = "work"\n')
    assert load_config(f, gdir).folders == {"/x/one": "one"}
    assert load_config(tmp_path / "nope.toml", gdir).folders == {}


def test_about_comes_from_valid_guard_config(tmp_path):
    gdir = _guard_toml(tmp_path, '[workspaces]\n"/y/two" = "two"\n[about]\ntwo = "Second."\n')
    assert load_config(tmp_path / "nope.toml", gdir).about == {"two": "Second."}


def test_about_empty_without_or_with_invalid_guard_config(tmp_path):
    assert load_config(tmp_path / "nope.toml", tmp_path / "no-guard").about == {}
    gdir = _guard_toml(tmp_path, "not = [toml\n")
    cfg = load_config(tmp_path / "nope.toml", gdir)
    assert cfg.about == {} and cfg.folders == {}
