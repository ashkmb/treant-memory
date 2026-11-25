import json
import os
import plistlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from treant_client import installer

BIN = "/opt/bin/treant-client"
EXISTING = {
    "theme": "dark",
    "hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": "cc-status start"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "cc-status stop"}]}],
    },
}


class Calls:
    def __init__(self, present=False, registered=None):
        self.calls = []
        self.present = present
        self.registered = registered if registered else (BIN if present else None)

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        rc = 0
        out = ""
        if cmd[:3] == ["claude", "mcp", "get"]:
            rc = 0 if self.registered else 1
            out = f"treant:\n  Command: {self.registered}\n" if self.registered else ""
        if cmd[:3] == ["claude", "mcp", "add-json"]:
            self.registered = json.loads(cmd[6])["command"]
        if cmd[:3] == ["claude", "mcp", "remove"]:
            self.registered = None
        if cmd[:2] == ["launchctl", "print"]:
            rc = 1
        return subprocess.CompletedProcess(cmd, rc, out, "")


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".claude").mkdir(parents=True)
    (h / ".claude" / "settings.json").write_text(json.dumps(EXISTING))
    (h / ".kiro" / "agents").mkdir(parents=True)
    (h / ".kiro" / "agents" / "me.json").write_text(
        json.dumps({"name": "me", "tools": ["*"], "hooks": {"preToolUse": [{"command": "x"}]}})
    )
    return h


CLAUDE = "/opt/homebrew/bin/claude"


def fake_which(name):
    return CLAUDE if name == "claude" else None


def _run(home, cfg, runner=None, binary=BIN, **kw):
    runner = runner or Calls()
    kw.setdefault("which", fake_which)
    out = []
    rc = installer.install(
        binary=binary,
        home=home,
        cfg=cfg,
        run=runner,
        out=out.append,
        uid=501,
        now=lambda: datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC),
        **kw,
    )
    return rc, runner, "".join(out)


def settings(home):
    return json.loads((home / ".claude" / "settings.json").read_text())


def test_install_adds_hooks_and_keeps_existing(home, cfg_tmp):
    rc, runner, _ = _run(home, cfg_tmp)
    assert rc == 0
    h = settings(home)["hooks"]
    assert h["Stop"] == EXISTING["hooks"]["Stop"]
    assert h["SessionStart"][0] == EXISTING["hooks"]["SessionStart"][0]
    assert h["SessionStart"][1]["hooks"][0] == {
        "type": "command",
        "command": f"{BIN} hook claude-session-start",
        "timeout": 7,
    }
    assert (
        h["PreToolUse"][0]["matcher"] == "Bash" and h["PreToolUse"][0]["hooks"][0]["timeout"] == 5
    )
    assert h["SessionEnd"][0]["hooks"][0]["timeout"] == 2
    assert h["PreCompact"][0]["hooks"][0]["command"] == f"{BIN} hook claude-pre-compact"
    assert settings(home)["theme"] == "dark"
    add = [c for c in runner.calls if c[:3] == ["claude", "mcp", "add-json"]]
    assert len(add) == 1 and add[0][3:6] == ["-s", "user", "treant"]
    assert json.loads(add[0][6]) == {"type": "stdio", "command": BIN, "args": ["mcp-proxy"]}


def test_install_is_idempotent(home, cfg_tmp):
    _run(home, cfg_tmp)
    snap = {p: p.read_text() for p in home.rglob("*") if p.is_file()}
    rc, runner, _ = _run(home, cfg_tmp, Calls(present=True))
    assert rc == 0
    assert {p: p.read_text() for p in home.rglob("*") if p.is_file()} == snap
    assert not [c for c in runner.calls if c[:3] == ["claude", "mcp", "add-json"]]


def test_dry_run_changes_nothing(home, cfg_tmp, tmp_path):
    before = {p: p.read_text() for p in home.rglob("*") if p.is_file()}
    calls = []

    def boom(*a, **k):
        calls.append(a)
        raise AssertionError("subprocess")

    out = []
    rc = installer.install(
        dry_run=True,
        binary=BIN,
        home=home,
        cfg=cfg_tmp,
        run=boom,
        out=out.append,
        uid=501,
        which=fake_which,
    )
    assert rc == 0 and not calls
    assert {p: p.read_text() for p in home.rglob("*") if p.is_file()} == before
    assert not (home / "Library").exists()
    text = "".join(out)
    assert "+++ b/" in text and "claude-session-start" in text


def test_backup_written(home, cfg_tmp):
    _run(home, cfg_tmp)
    bak = home / ".claude" / "settings.json.treant-bak-20261101T120000Z"
    assert json.loads(bak.read_text()) == EXISTING
    assert (home / ".kiro" / "agents" / "me.json.treant-bak-20261101T120000Z").exists()


def test_kiro_agent_merge_keeps_other_keys(home, cfg_tmp):
    _run(home, cfg_tmp)
    a = json.loads((home / ".kiro" / "agents" / "me.json").read_text())
    assert a["name"] == "me" and a["tools"] == ["*"]
    assert a["hooks"]["preToolUse"][0] == {"command": "x"}
    assert a["hooks"]["preToolUse"][1] == {
        "matcher": "execute_bash",
        "command": f"{BIN} hook kiro-guard",
    }
    assert a["hooks"]["agentSpawn"] == [{"command": f"{BIN} hook kiro-start"}]
    assert a["mcpServers"]["treant"] == {"command": BIN, "args": ["mcp-proxy", "--client", "kiro"]}


def test_plist_contents(home, cfg_tmp):
    _, runner, _ = _run(home, cfg_tmp)
    p = plistlib.loads((home / "Library/LaunchAgents/org.treant.kiro-sweep.plist").read_bytes())
    assert p["ProgramArguments"] == [BIN, "kiro-sweep"]
    assert p["StartInterval"] == 600 and p["RunAtLoad"] is False
    assert p["StandardOutPath"] == "/dev/null" and p["StandardErrorPath"] == "/dev/null"
    lc = [c[1:3] for c in runner.calls if c[0] == "launchctl" and c[1] != "print"]
    assert lc == [["bootout", "gui/501"], ["bootstrap", "gui/501"]]


def test_plist_path_reaches_claude(home, cfg_tmp):
    """launchd's PATH is /usr/bin:/bin:/usr/sbin:/sbin; the sweep's summarizer needs claude."""
    _run(home, cfg_tmp)
    p = plistlib.loads((home / "Library/LaunchAgents/org.treant.kiro-sweep.plist").read_bytes())
    assert p["EnvironmentVariables"] == {"PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"}


def test_plist_path_no_duplicate_when_claude_in_system_dir(home, cfg_tmp):
    _run(home, cfg_tmp, which=lambda n: "/usr/bin/claude" if n == "claude" else None)
    p = plistlib.loads((home / "Library/LaunchAgents/org.treant.kiro-sweep.plist").read_bytes())
    assert p["EnvironmentVariables"]["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"


def test_refuses_without_claude(home, cfg_tmp, capsys):
    rc, runner, _ = _run(home, cfg_tmp, which=lambda n: None)
    assert rc == 1 and "claude not found" in capsys.readouterr().err
    assert settings(home) == EXISTING
    assert not (home / "Library/LaunchAgents/org.treant.kiro-sweep.plist").exists()
    assert runner.calls == []


NOW_MS = int(datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)


def test_install_writes_kiro_watermark_once(home, cfg_tmp):
    since = Path(os.environ["TREANT_STATE_DIR"]) / "kiro-since"
    _run(home, cfg_tmp)
    assert int(since.read_text()) == NOW_MS
    since.write_text("0\n")  # the owner asked for a backfill
    _run(home, cfg_tmp)
    assert since.read_text() == "0\n"


def test_dry_run_does_not_write_kiro_watermark(home, cfg_tmp):
    _, _, out = _run(home, cfg_tmp, dry_run=True)
    assert not (Path(os.environ["TREANT_STATE_DIR"]) / "kiro-since").exists()
    assert "kiro-since" in out


def test_uninstall_restores_shape(home, cfg_tmp):
    orig = {p: p.read_text() for p in home.rglob("*") if p.is_file()}
    _run(home, cfg_tmp)
    runner = Calls(present=True)
    rc = installer.uninstall(home=home, cfg=cfg_tmp, run=runner, out=lambda s: None, uid=501)
    assert rc == 0
    assert settings(home) == EXISTING
    a = json.loads((home / ".kiro" / "agents" / "me.json").read_text())
    assert a == {"name": "me", "tools": ["*"], "hooks": {"preToolUse": [{"command": "x"}]}}
    assert not (home / "Library/LaunchAgents/org.treant.kiro-sweep.plist").exists()
    assert ["claude", "mcp", "remove", "-s", "user", "treant"] in runner.calls
    now = {p for p in home.rglob("*") if p.is_file()}
    assert {p for p in now if ".treant-bak-" not in p.name} == set(orig)
    for p, text in orig.items():
        assert json.loads(p.read_text()) == json.loads(text), p


def test_state_and_log_dirs_tightened(home, cfg_tmp, tmp_path):
    state = Path(os.environ["TREANT_STATE_DIR"])
    state.mkdir(mode=0o755)
    state.chmod(0o755)
    _run(home, cfg_tmp)
    assert state.stat().st_mode & 0o777 == 0o700
    assert Path(os.environ["TREANT_CLIENT_LOG_DIR"]).stat().st_mode & 0o777 == 0o700


def test_refuses_without_binary(home, cfg_tmp, capsys):
    rc = installer.install(
        home=home, cfg=cfg_tmp, which=lambda n: None, run=Calls(), out=lambda s: None
    )
    assert rc == 1 and "treant-client not found" in capsys.readouterr().err
    assert settings(home) == EXISTING


def test_refuses_corrupt_settings(home, cfg_tmp):
    (home / ".claude" / "settings.json").write_text("{nope")
    rc, _, _ = _run(home, cfg_tmp)
    assert rc == 1
    assert (home / ".claude" / "settings.json").read_text() == "{nope"


BIN2 = "/other/venv/bin/treant-client"


def test_reinstall_with_new_binary_replaces_everything(home, cfg_tmp):
    runner = Calls()
    _run(home, cfg_tmp, runner)
    _run(home, cfg_tmp, runner, binary=BIN2)
    h = settings(home)["hooks"]
    for event in ("SessionStart", "PreToolUse", "SessionEnd", "PreCompact"):
        cmds = [
            x["command"] for g in h[event] for x in g["hooks"] if "treant-client" in x["command"]
        ]
        assert len(cmds) == 1 and cmds[0].startswith(BIN2 + " "), event
    assert h["SessionStart"][0] == EXISTING["hooks"]["SessionStart"][0]
    a = json.loads((home / ".kiro" / "agents" / "me.json").read_text())
    assert [e["command"] for e in a["hooks"]["agentSpawn"]] == [f"{BIN2} hook kiro-start"]
    assert len(a["hooks"]["preToolUse"]) == 2
    assert a["hooks"]["preToolUse"][1]["command"] == f"{BIN2} hook kiro-guard"
    assert a["mcpServers"]["treant"]["command"] == BIN2
    assert runner.registered == BIN2
    p = plistlib.loads((home / "Library/LaunchAgents/org.treant.kiro-sweep.plist").read_bytes())
    assert p["ProgramArguments"][0] == BIN2


def test_write_is_atomic_when_interrupted(home, cfg_tmp, monkeypatch):
    path = home / ".claude" / "settings.json"
    before = path.read_text()

    def boom(fd):
        raise OSError("disk")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        _run(home, cfg_tmp)
    assert path.read_text() == before
    assert [p.name for p in path.parent.iterdir() if ".treant-bak-" not in p.name] == [
        "settings.json"
    ]


def test_symlinked_settings_stays_symlink(home, cfg_tmp, tmp_path):
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    link = home / ".claude" / "settings.json"
    real.write_text(link.read_text())
    link.unlink()
    link.symlink_to(real)
    _run(home, cfg_tmp)
    assert link.is_symlink() and link.resolve() == real.resolve()
    assert "claude-session-start" in real.read_text()


LEGACY = "Library/LaunchAgents/com.example.treant.kiro-sweep.plist"
NEW = "Library/LaunchAgents/org.treant.kiro-sweep.plist"
STAMP = "treant-bak-20261101T120000Z"


def _uninstall(home, cfg, runner=None, **kw):
    runner = runner or Calls(present=True)
    out = []
    rc = installer.uninstall(
        home=home,
        cfg=cfg,
        run=runner,
        out=out.append,
        uid=501,
        now=lambda: datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC),
        **kw,
    )
    return rc, runner, "".join(out)


def _legacy(home):
    path = home / LEGACY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps({"Label": "com.example.treant.kiro-sweep"}))
    return path


class Loaded(Calls):
    """launchctl print succeeds for the given labels."""

    def __init__(self, labels, **kw):
        super().__init__(**kw)
        self.labels = labels

    def __call__(self, cmd, **kw):
        r = super().__call__(cmd, **kw)
        if cmd[:2] == ["launchctl", "print"] and cmd[2].rsplit("/", 1)[1] in self.labels:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return r


def test_plist_label_is_neutral(home, cfg_tmp):
    _run(home, cfg_tmp)
    p = plistlib.loads((home / NEW).read_bytes())
    assert p["Label"] == "org.treant.kiro-sweep"
    assert installer.PLIST_LABEL == "org.treant.kiro-sweep"


def test_install_migrates_the_legacy_job(home, cfg_tmp):
    legacy = _legacy(home)
    before = legacy.read_bytes()
    rc, runner, _ = _run(home, cfg_tmp)
    assert rc == 0
    assert not legacy.exists()
    backups = Path(os.environ["TREANT_STATE_DIR"]) / "backups"
    assert (backups / f"{legacy.name}.{STAMP}").read_bytes() == before
    assert sorted(p.name for p in legacy.parent.iterdir()) == [Path(NEW).name]
    assert ["launchctl", "bootout", "gui/501/com.example.treant.kiro-sweep"] in runner.calls
    assert (home / NEW).exists()
    lc = [c[1:3] for c in runner.calls if c[0] == "launchctl" and c[1] != "print"]
    assert lc == [
        ["bootout", "gui/501/com.example.treant.kiro-sweep"],
        ["bootout", "gui/501"],
        ["bootstrap", "gui/501"],
    ]


def test_install_boots_out_a_loaded_legacy_job_without_plist(home, cfg_tmp):
    rc, runner, _ = _run(home, cfg_tmp, Loaded({"com.example.treant.kiro-sweep"}))
    assert rc == 0
    assert ["launchctl", "bootout", "gui/501/com.example.treant.kiro-sweep"] in runner.calls


def test_install_without_legacy_leaves_launchctl_alone(home, cfg_tmp):
    _, runner, _ = _run(home, cfg_tmp)
    assert not [c for c in runner.calls if c[:2] == ["launchctl", "bootout"] and "example" in c[2]]


def test_dry_run_reports_the_legacy_migration(home, cfg_tmp):
    legacy = _legacy(home)
    calls = []

    def boom(*a, **k):
        calls.append(a)
        raise AssertionError("subprocess")

    out = []
    rc = installer.install(
        dry_run=True,
        binary=BIN,
        home=home,
        cfg=cfg_tmp,
        run=boom,
        out=out.append,
        uid=501,
        which=fake_which,
    )
    assert rc == 0 and not calls and legacy.exists()
    assert "would run: launchctl bootout gui/501/com.example.treant.kiro-sweep" in "".join(out)


def test_uninstall_removes_new_and_legacy_jobs(home, cfg_tmp):
    _run(home, cfg_tmp)
    legacy = _legacy(home)
    rc, runner, _ = _uninstall(home, cfg_tmp)
    assert rc == 0
    assert not (home / NEW).exists() and not legacy.exists()
    backups = Path(os.environ["TREANT_STATE_DIR"]) / "backups"
    assert (backups / f"{legacy.name}.{STAMP}").exists()
    assert (backups / f"{Path(NEW).name}.{STAMP}").exists()
    assert list(legacy.parent.iterdir()) == []  # no backups left for launchd to trip over
    assert ["launchctl", "bootout", "gui/501/com.example.treant.kiro-sweep"] in runner.calls
    assert ["launchctl", "bootout", "gui/501", str(home / NEW)] in runner.calls


AGENTS = ".kiro/agents"


def test_uninstall_without_kiro_agent_cleans_every_agent(home, cfg_tmp):
    from dataclasses import replace

    agents = home / AGENTS
    other = {"name": "other", "hooks": {"stop": [{"command": "y"}]}, "mcpServers": {"x": {}}}
    (agents / "other.json").write_text(json.dumps(other))
    (agents / "broken.json").write_text("{nope")
    (agents / "notes.txt").write_text("not an agent")
    _run(home, cfg_tmp)  # merges into me.json
    two = installer.add_kiro(
        {"name": "two", "hooks": {"preToolUse": [{"command": "z"}]}, "keep": [1, {"a": None}]},
        BIN2,
    )
    (agents / "two.json").write_text(json.dumps(two))
    rc, _, out = _uninstall(home, replace(cfg_tmp, kiro_agent=None))
    assert rc == 0
    assert json.loads((agents / "me.json").read_text()) == {
        "name": "me",
        "tools": ["*"],
        "hooks": {"preToolUse": [{"command": "x"}]},
    }
    assert json.loads((agents / "two.json").read_text()) == {
        "name": "two",
        "hooks": {"preToolUse": [{"command": "z"}]},
        "keep": [1, {"a": None}],
    }
    assert json.loads((agents / "other.json").read_text()) == other
    assert not (agents / f"other.json.{STAMP}").exists()
    assert (agents / f"two.json.{STAMP}").exists()
    assert (agents / "broken.json").read_text() == "{nope"
    assert f"removed Treant hooks from {agents / 'me.json'}" in out
    assert f"removed Treant hooks from {agents / 'two.json'}" in out
    assert "other.json" not in out
    assert f"{agents / 'broken.json'} is not valid JSON" in out


def test_uninstall_with_kiro_agent_touches_only_that_agent(home, cfg_tmp):
    agents = home / AGENTS
    two = installer.add_kiro({"name": "two"}, BIN)
    (agents / "two.json").write_text(json.dumps(two))
    _run(home, cfg_tmp)
    rc, _, _ = _uninstall(home, cfg_tmp)
    assert rc == 0
    assert json.loads((agents / "two.json").read_text()) == two
    assert json.loads((agents / "me.json").read_text()) == {
        "name": "me",
        "tools": ["*"],
        "hooks": {"preToolUse": [{"command": "x"}]},
    }


def test_uninstall_dry_run_without_kiro_agent_changes_nothing(home, cfg_tmp):
    from dataclasses import replace

    _run(home, cfg_tmp)
    _legacy(home)
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    rc, runner, out = _uninstall(home, replace(cfg_tmp, kiro_agent=None), dry_run=True)
    assert rc == 0 and runner.calls == []
    assert {p: p.read_bytes() for p in home.rglob("*") if p.is_file()} == before
    assert f"would remove Treant hooks from {home / AGENTS / 'me.json'}" in out
    assert "would run: launchctl bootout gui/501/com.example.treant.kiro-sweep" in out


def test_reinstall_repoints_an_old_kiro_proxy_entry(home, cfg_tmp):
    # entries written before --client kiro existed are ours and get the flag on reinstall
    agent = home / AGENTS / "me.json"
    agent.write_text(
        json.dumps({"mcpServers": {"treant": {"command": BIN, "args": ["mcp-proxy"]}}})
    )
    rc, _, _ = _run(home, cfg_tmp)
    assert rc == 0
    t = json.loads(agent.read_text())["mcpServers"]["treant"]
    assert t == {"command": BIN, "args": ["mcp-proxy", "--client", "kiro"]}


def test_uninstall_removes_old_and_new_kiro_proxy_entries():
    for args in (["mcp-proxy"], ["mcp-proxy", "--client", "kiro"]):
        agent = {"mcpServers": {"treant": {"command": BIN, "args": args}, "x": {}}}
        assert installer.remove_kiro(agent) == {"mcpServers": {"x": {}}}


def test_install_refuses_a_foreign_kiro_treant_server(home, cfg_tmp, capsys):
    agent = home / AGENTS / "me.json"
    foreign = {"mcpServers": {"treant": {"command": "/usr/bin/other", "args": ["serve"]}}}
    agent.write_text(json.dumps(foreign))
    rc, _, _ = _run(home, cfg_tmp)
    assert rc == 1 and "mcpServers.treant" in capsys.readouterr().err
    assert json.loads(agent.read_text()) == foreign


def test_hook_timeouts_leave_room_beyond_the_budgets():
    # Claude Code treats a hook timeout as a non-blocking error and runs the command: the
    # host timeout must cover interpreter start and imports on top of the in-process budget.
    from treant_client import hooks_claude as hc

    budgets = {
        "claude-session-start": hc.START_BUDGET,
        "claude-pre-tool-use": hc.GUARD_BUDGET,
        "claude-session-end": hc.SAVE_HOOK_BUDGET,
        "claude-pre-compact": hc.SAVE_HOOK_BUDGET,
    }
    for _event, _m, sub, timeout in installer.CLAUDE_EVENTS:
        assert timeout >= budgets[sub] + 1.0, sub


def test_reinstall_updates_an_old_hook_timeout(home, cfg_tmp):
    _run(home, cfg_tmp)
    s = settings(home)
    s["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] = 3
    (home / ".claude" / "settings.json").write_text(json.dumps(s))
    _run(home, cfg_tmp, Calls(present=True))
    assert settings(home)["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] == 5


SPACED = "/home/a b/bin/treant-client"


def test_binary_path_with_spaces_is_quoted_and_found_again(home, cfg_tmp):
    import shlex

    _run(home, cfg_tmp, binary=SPACED)
    h = settings(home)["hooks"]
    cmd = h["PreToolUse"][0]["hooks"][0]["command"]
    assert shlex.split(cmd) == [SPACED, "hook", "claude-pre-tool-use"]
    a = json.loads((home / AGENTS / "me.json").read_text())
    assert shlex.split(a["hooks"]["agentSpawn"][0]["command"]) == [SPACED, "hook", "kiro-start"]
    _run(home, cfg_tmp, Calls(present=True, registered=SPACED), binary=SPACED)
    assert len(settings(home)["hooks"]["PreToolUse"]) == 1  # recognised as ours: no duplicate
    rc, _, _ = _uninstall(home, cfg_tmp)
    assert rc == 0 and settings(home) == EXISTING


@pytest.mark.parametrize(
    "bad",
    [
        {"hooks": []},
        {"hooks": {"PreToolUse": {"matcher": "Bash"}}},
        {"hooks": {"PreToolUse": [{"hooks": "x"}]}},
    ],
)
def test_wrong_shaped_settings_is_an_install_error(home, cfg_tmp, bad, capsys):
    (home / ".claude" / "settings.json").write_text(json.dumps(bad))
    rc, _, _ = _run(home, cfg_tmp)
    assert rc == 1 and "error:" in capsys.readouterr().err
    assert settings(home) == bad


@pytest.mark.parametrize(
    "bad",
    [{"hooks": []}, {"hooks": {"agentSpawn": {}}}, {"mcpServers": []}],
)
def test_wrong_shaped_kiro_agent_is_an_install_error(home, cfg_tmp, bad, capsys):
    agent = home / AGENTS / "me.json"
    agent.write_text(json.dumps(bad))
    rc, _, _ = _run(home, cfg_tmp)
    assert rc == 1 and "error:" in capsys.readouterr().err
    assert json.loads(agent.read_text()) == bad


def test_corrupt_kiro_agent_is_an_install_error(home, cfg_tmp, capsys):
    agent = home / AGENTS / "me.json"
    agent.write_text("{nope")
    rc, _, _ = _run(home, cfg_tmp)
    assert rc == 1 and "not valid JSON" in capsys.readouterr().err
    assert agent.read_text() == "{nope"


def test_missing_kiro_agent_is_skipped(home, cfg_tmp):
    (home / AGENTS / "me.json").unlink()
    rc, _, out = _run(home, cfg_tmp)
    assert rc == 0 and f"skipped Kiro: {home / AGENTS / 'me.json'} does not exist" in out
    assert not (home / AGENTS / "me.json").exists()


def test_partial_install_says_what_changed(home, cfg_tmp, capsys):
    agent = home / AGENTS / "me.json"
    agent.write_text(json.dumps({"mcpServers": {"treant": {"command": "/x", "args": ["y"]}}}))
    rc, _, _ = _run(home, cfg_tmp)
    err = capsys.readouterr().err
    assert rc == 1
    assert "stopped part way" in err and str(home / ".claude" / "settings.json") in err


def test_uninstall_on_a_clean_system_leaves_files_alone(home, cfg_tmp):
    s = home / ".claude" / "settings.json"
    s.write_text('{"theme":"dark"}')  # compact: a reformat would show
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    rc, _, _ = _uninstall(home, cfg_tmp, Calls())
    assert rc == 0
    assert {p: p.read_bytes() for p in home.rglob("*") if p.is_file()} == before


def test_backups_in_the_same_second_do_not_overwrite(home, cfg_tmp):
    _run(home, cfg_tmp)
    first = (home / ".claude" / f"settings.json.{STAMP}").read_text()
    _run(home, cfg_tmp, binary=BIN2)  # same clock second, new content
    baks = sorted((home / ".claude").glob("settings.json.treant-bak-*"))
    assert len(baks) == 2
    assert (home / ".claude" / f"settings.json.{STAMP}").read_text() == first


class FlakyBootstrap(Calls):
    def __init__(self, fails, **kw):
        super().__init__(**kw)
        self.fails = fails

    def __call__(self, cmd, **kw):
        r = super().__call__(cmd, **kw)
        if cmd[:2] == ["launchctl", "bootstrap"] and self.fails:
            self.fails -= 1
            return subprocess.CompletedProcess(cmd, 5, "", "Input/output error")
        return r


def test_bootstrap_is_retried(home, cfg_tmp):
    runner = FlakyBootstrap(2)
    rc, _, _ = _run(home, cfg_tmp, runner, sleep=lambda s: None)
    assert rc == 0
    assert len([c for c in runner.calls if c[:2] == ["launchctl", "bootstrap"]]) == 3


def test_bootstrap_gives_up(home, cfg_tmp, capsys):
    rc, _, _ = _run(home, cfg_tmp, FlakyBootstrap(99), sleep=lambda s: None)
    assert rc == 1 and "launchctl bootstrap failed" in capsys.readouterr().err


def test_install_skips_launchctl_when_plist_same_and_job_loaded(home, cfg_tmp):
    _run(home, cfg_tmp)
    runner = Loaded({"org.treant.kiro-sweep"}, present=True)
    rc, _, _ = _run(home, cfg_tmp, runner)
    assert rc == 0
    assert not [
        c for c in runner.calls if c[:2] in (["launchctl", "bootout"], ["launchctl", "bootstrap"])
    ]


def test_uninstall_boots_out_a_loaded_job_without_plist(home, cfg_tmp):
    rc, runner, _ = _uninstall(home, cfg_tmp, Loaded({"org.treant.kiro-sweep"}, present=True))
    assert rc == 0
    assert ["launchctl", "bootout", "gui/501/org.treant.kiro-sweep"] in runner.calls


def test_uninstall_dry_run_changes_nothing(home, cfg_tmp):
    _run(home, cfg_tmp)
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    rc, runner, out = _uninstall(home, cfg_tmp, dry_run=True)
    assert rc == 0 and runner.calls == []
    assert {p: p.read_bytes() for p in home.rglob("*") if p.is_file()} == before
    assert "claude-pre-tool-use" in out and "would run: launchctl bootout" in out


def test_config_load_error_is_an_install_error(home, monkeypatch, capsys):
    from treant_client import config as cc

    def boom(*a, **k):
        raise ValueError("bad toml")

    monkeypatch.setattr(cc, "load_config", boom)
    rc, _, _ = _run(home, None)
    assert rc == 1 and "could not load" in capsys.readouterr().err
    rc = installer.uninstall(home=home, run=Calls(), out=lambda s: None, uid=501)
    assert rc == 1


def test_mcp_check_is_not_a_substring_match(home, cfg_tmp):
    runner = Calls(registered=BIN + "-old")
    rc, _, _ = _run(home, cfg_tmp, runner)
    assert rc == 0
    assert ["claude", "mcp", "remove", "-s", "user", "treant"] in runner.calls
    assert runner.registered == BIN


class RemoveFails(Calls):
    def __call__(self, cmd, **kw):
        if cmd[:3] == ["claude", "mcp", "remove"]:
            self.calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 1, "", "")
        return super().__call__(cmd, **kw)


def test_failed_mcp_remove_is_an_install_error(home, cfg_tmp, capsys):
    rc, _, _ = _run(home, cfg_tmp, RemoveFails(registered="/old/treant-client"))
    assert rc == 1 and "claude mcp remove" in capsys.readouterr().err


def test_sweep_path_makes_a_relative_claude_folder_absolute(tmp_path, monkeypatch):
    # PATH entries like "bin" or "." make which() return a relative path; launchd runs the job
    # from /, where a relative folder means something else
    monkeypatch.chdir(tmp_path)
    path = installer.sweep_path(lambda n: "bin/claude")
    first = path.split(":")[0]
    assert first == str(tmp_path / "bin") and os.path.isabs(first)
