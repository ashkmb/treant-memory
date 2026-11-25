import json
import os
import stat

from treant_client import log


def test_log_drops_unlisted_fields(tmp_path, monkeypatch):
    d = tmp_path / "logs"
    monkeypatch.setenv("TREANT_CLIENT_LOG_DIR", str(d))
    log.event("guard", workspace="work", command="kubectl get pods", status="ok", count=2)
    line = json.loads((d / "client.log").read_text())
    assert "command" not in line
    assert line["workspace"] == "work" and line["status"] == "ok" and line["count"] == 2
    assert line["event"] == "guard"
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(d / "client.log").st_mode) == 0o600


def test_log_appends_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("TREANT_CLIENT_LOG_DIR", str(tmp_path))
    log.event("a")
    log.event("b", duration_ms=5)
    assert len((tmp_path / "client.log").read_text().splitlines()) == 2


def test_log_never_raises(tmp_path, monkeypatch):
    # the log folder cannot be created (a file is in the way): event() must swallow the error,
    # since every hook logs and a raising log would break the host tool
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("TREANT_CLIENT_LOG_DIR", str(blocker / "sub"))
    log.event("x", workspace="w")


def test_log_closes_the_descriptor_when_fdopen_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("TREANT_CLIENT_LOG_DIR", str(tmp_path))
    opened, closed = [], []
    real_open, real_close = os.open, os.close

    def track_open(*a, **k):
        fd = real_open(*a, **k)
        opened.append(fd)
        return fd

    def boom(*a, **k):
        raise OSError("fdopen")

    monkeypatch.setattr(log.os, "open", track_open)
    monkeypatch.setattr(log.os, "fdopen", boom)
    monkeypatch.setattr(log.os, "close", lambda fd: closed.append(fd) or real_close(fd))
    log.event("x")
    assert opened and closed == opened


def test_log_category_carries_no_text(tmp_path, monkeypatch):
    # categories are exception class names or fixed words, never a message
    from treant_client.hooks_claude import _category
    from treant_client.mcp_http import TreantError

    assert _category(TreantError("http_500", "secret body")) == "http_500"
    assert _category(RuntimeError("token=abc")) == "error"
    assert _category("timeout") == "timeout"
