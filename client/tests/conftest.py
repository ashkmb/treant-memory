import pytest

from treant_client.config import Config


@pytest.fixture
def cfg_tmp(tmp_path):
    root = tmp_path / "Projects"
    for sub in ("Work", "startup", "Personal/Club", "Volunteer", "Personal2"):
        (root / sub).mkdir(parents=True)
    folders = {
        str(root / "Work"): "work",
        str(root / "startup"): "startup",
        str(root / "Personal" / "Club"): "club",
        str(root / "Personal"): "personal",
        str(root / "Volunteer"): "volunteer",
    }
    return Config(
        url="https://example.invalid/mcp",
        folders=folders,
        scrub_host_terms={"work": ["acme"]},
        kiro_agent="me",
    )


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Keep logs, save locks and the summarizer folder out of the real home directory."""
    monkeypatch.setenv("TREANT_CLIENT_LOG_DIR", str(tmp_path / "_logs"))
    monkeypatch.setenv("TREANT_LOCK_DIR", str(tmp_path / "_locks"))
    monkeypatch.setenv("TREANT_STATE_DIR", str(tmp_path / "_state"))
    monkeypatch.delenv("TREANT_SUMMARIZER", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)


@pytest.fixture
def guard_cfg(cfg_tmp):
    """Guard config over the same folders: work lists kube context ``ops`` and its own folder."""
    from treant_guard.config import GuardConfig
    from treant_guard.rules import default_rules

    work = next(p for p, w in cfg_tmp.folders.items() if w == "work")
    return GuardConfig(
        rules=default_rules(),
        workspaces=dict(cfg_tmp.folders),
        allow={
            "work": {"kube_context": ["ops"], "terraform_dir": [work]},
            "personal": {"kube_context": ["home"]},
        },
        deny={},
        about={},
        mode="allowlist",  # hook plumbing tests: an unlisted context asks
    )
