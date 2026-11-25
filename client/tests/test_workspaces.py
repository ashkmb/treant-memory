import os

from treant_client.workspaces import resolve


def root(cfg):
    return os.path.dirname(next(p for p, w in cfg.folders.items() if w == "work"))


def test_longest_match_wins(cfg_tmp):
    r = root(cfg_tmp)
    assert resolve(f"{r}/Personal", cfg_tmp) == "personal"
    assert resolve(f"{r}/Personal/Club/sub", cfg_tmp) == "club"
    assert resolve(f"{r}/Personal/other", cfg_tmp) == "personal"


def test_sibling_prefix_does_not_match(cfg_tmp):
    assert resolve(f"{root(cfg_tmp)}/Personal2/x", cfg_tmp) is None
    assert resolve(f"{root(cfg_tmp)}/Personal2", cfg_tmp) is None


def test_outside_map_is_none(cfg_tmp, tmp_path):
    assert resolve(str(tmp_path), cfg_tmp) is None
    assert resolve("/", cfg_tmp) is None


def test_symlinked_cwd_resolves(cfg_tmp, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(f"{root(cfg_tmp)}/Work")
    assert resolve(str(link), cfg_tmp) == "work"


def test_trailing_slash_and_exact_root(cfg_tmp):
    r = root(cfg_tmp)
    assert resolve(f"{r}/Work/", cfg_tmp) == "work"
    assert resolve(f"{r}/Work", cfg_tmp) == "work"


def test_empty_cwd_is_none(cfg_tmp):
    assert resolve("", cfg_tmp) is None
