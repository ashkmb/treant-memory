"""Register (and remove) the hooks, MCP entries and launchd job. Idempotent, backs up first."""

from __future__ import annotations

import contextlib
import difflib
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from treant_client import config as _config
from treant_client.kiro import SINCE_FILE, ensure_since, read_since
from treant_client.summarize import default_state_dir

PLIST_LABEL = "org.treant.kiro-sweep"
# The label before 2026-11-02: install and uninstall boot it out and remove its plist.
LEGACY_LABEL = "com.example.treant.kiro-sweep"
CLAUDE_EVENTS = (
    # (event, matcher, hook subcommand, timeout seconds). Each timeout is the hook's in-process
    # budget plus room for interpreter start: Claude Code runs the tool call when a hook times out.
    ("SessionStart", None, "claude-session-start", 7),
    ("PreToolUse", "Bash", "claude-pre-tool-use", 5),
    ("SessionEnd", None, "claude-session-end", 2),
    ("PreCompact", None, "claude-pre-compact", 2),
)


class InstallError(Exception):
    pass


@dataclass(frozen=True)
class Paths:
    settings: Path
    kiro_agent: Path | None
    plist: Path
    legacy_plist: Path
    kiro_agents: Path
    state_dir: Path
    log_dir: Path

    @property
    def backup_dir(self) -> Path:
        """Plist backups: never in LaunchAgents, where launchd reads every file."""
        return self.state_dir / "backups"

    @classmethod
    def for_home(cls, home: Path, kiro_agent: str | None) -> Paths:
        return cls(
            settings=home / ".claude" / "settings.json",
            kiro_agent=home / ".kiro" / "agents" / f"{kiro_agent}.json" if kiro_agent else None,
            plist=home / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist",
            legacy_plist=home / "Library" / "LaunchAgents" / f"{LEGACY_LABEL}.plist",
            kiro_agents=home / ".kiro" / "agents",
            state_dir=default_state_dir()
            if "TREANT_STATE_DIR" in os.environ
            else home / ".local" / "state" / "treant",
            log_dir=Path(os.environ["TREANT_CLIENT_LOG_DIR"])
            if os.environ.get("TREANT_CLIENT_LOG_DIR")
            else home / "Library" / "Logs" / "treant",
        )


def find_binary(which: Callable[[str], str | None] = shutil.which) -> str:
    found = which("treant-client")
    if not found:
        raise InstallError("treant-client not found on PATH; run `uv tool install ./client` first")
    return str(Path(found).resolve())


LAUNCHD_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


def sweep_path(which: Callable[[str], str | None] = shutil.which) -> str:
    """PATH for the launchd sweep: claude's folder (as found now) before launchd's default.
    The folder is not resolved, so a Homebrew upgrade (new Cellar path) keeps working."""
    found = which("claude")
    if not found:
        raise InstallError(
            "claude not found on PATH; the Kiro sweep summarizes with `claude -p`. "
            "Install Claude Code first"
        )
    folder = os.path.abspath(os.path.dirname(found))  # absolute, but symlinks unresolved
    return ":".join(dict.fromkeys((folder, *LAUNCHD_PATH)))


def _hook_command(binary: str, sub: str) -> str:
    """The shell command line for a hook; the binary is quoted (a path may hold spaces)."""
    return f"{shlex.quote(binary)} hook {sub}"


def _is_ours(command: object, sub: str) -> bool:
    if not isinstance(command, str):
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    return len(parts) == 3 and parts[1:] == ["hook", sub] and Path(parts[0]).name == "treant-client"


def _shape(ok: bool, what: str) -> None:
    if not ok:
        raise InstallError(f"{what} has an unexpected shape; not touching it")


def _load_json(path: Path) -> dict:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(text)
    except ValueError as e:
        raise InstallError(f"{path} is not valid JSON; not touching it") from e
    if not isinstance(data, dict):
        raise InstallError(f"{path} is not a JSON object; not touching it")
    return data


def _dump_json(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""


# ---- pure edits -------------------------------------------------------------------------


def add_claude_hooks(settings: dict, binary: str) -> dict:
    hooks = settings.setdefault("hooks", {})
    _shape(isinstance(hooks, dict), "settings.json hooks")
    for event, matcher, sub, timeout in CLAUDE_EVENTS:
        groups = hooks.setdefault(event, [])
        _shape(isinstance(groups, list), f"settings.json hooks.{event}")
        command = _hook_command(binary, sub)
        found = False
        for g in groups:
            if not isinstance(g, dict):
                continue
            inner = g.get("hooks", [])
            _shape(isinstance(inner, list), f"settings.json hooks.{event}[].hooks")
            for h in inner:
                if isinstance(h, dict) and _is_ours(h.get("command"), sub):
                    h["command"] = command  # repoint a stale binary path
                    h["timeout"] = timeout
                    found = True
        if found:
            continue
        group: dict = {}
        if matcher:
            group["matcher"] = matcher
        group["hooks"] = [{"type": "command", "command": command, "timeout": timeout}]
        groups.append(group)
    return settings


def remove_claude_hooks(settings: dict) -> dict:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    for event, _m, sub, _t in CLAUDE_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept = []
        for g in groups:
            if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                inner = [
                    h
                    for h in g["hooks"]
                    if not (isinstance(h, dict) and _is_ours(h.get("command"), sub))
                ]
                if len(inner) != len(g["hooks"]):
                    if not inner:
                        continue
                    g = {**g, "hooks": inner}
            kept.append(g)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        del settings["hooks"]
    return settings


def _kiro_entries(binary: str) -> dict:
    return {
        "agentSpawn": {"command": _hook_command(binary, "kiro-start")},
        "preToolUse": {"matcher": "execute_bash", "command": _hook_command(binary, "kiro-guard")},
    }


# Kiro never sets CLAUDE_PROJECT_DIR; the flag makes its proxy ignore an inherited one.
KIRO_PROXY_ARGS = ["mcp-proxy", "--client", "kiro"]


def _is_our_server(entry: object) -> bool:
    """A Treant proxy entry, with or without the --client flag (older installs had none)."""
    if not isinstance(entry, dict):
        return False
    args = entry.get("args")
    return isinstance(args, list) and args[:1] == ["mcp-proxy"]


def add_kiro(agent: dict, binary: str) -> dict:
    hooks = agent.setdefault("hooks", {})
    _shape(isinstance(hooks, dict), "Kiro agent hooks")
    servers = agent.setdefault("mcpServers", {})
    _shape(isinstance(servers, dict), "Kiro agent mcpServers")
    for key, entry in _kiro_entries(binary).items():
        lst = hooks.setdefault(key, [])
        _shape(isinstance(lst, list), f"Kiro agent hooks.{key}")
        sub = entry["command"].rsplit(" ", 1)[1]
        ours = [e for e in lst if isinstance(e, dict) and _is_ours(e.get("command"), sub)]
        if ours:
            for e in ours:
                e["command"] = entry["command"]
        else:
            lst.append(entry)
    if "treant" in servers and not _is_our_server(servers["treant"]):
        raise InstallError("Kiro agent has another mcpServers.treant entry; not touching it")
    servers["treant"] = {"command": binary, "args": list(KIRO_PROXY_ARGS)}
    return agent


def remove_kiro(agent: dict) -> dict:
    hooks = agent.get("hooks")
    if isinstance(hooks, dict):
        for key, sub in (("agentSpawn", "kiro-start"), ("preToolUse", "kiro-guard")):
            lst = hooks.get(key)
            if isinstance(lst, list):
                kept = [
                    e for e in lst if not (isinstance(e, dict) and _is_ours(e.get("command"), sub))
                ]
                if kept:
                    hooks[key] = kept
                else:
                    del hooks[key]
        if not hooks:
            del agent["hooks"]
    servers = agent.get("mcpServers")
    if isinstance(servers, dict):
        if _is_our_server(servers.get("treant")):
            del servers["treant"]
        if not servers:
            del agent["mcpServers"]
    return agent


def plist_text(binary: str, path: str) -> str:
    return plistlib.dumps(
        {
            "Label": PLIST_LABEL,
            "ProgramArguments": [binary, "kiro-sweep"],
            "EnvironmentVariables": {"PATH": path},
            "StartInterval": 600,
            "RunAtLoad": False,
            "StandardOutPath": "/dev/null",
            "StandardErrorPath": "/dev/null",
        }
    ).decode()


# ---- applying ---------------------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            shutil.copymode(path, tmp)
        except FileNotFoundError:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class _Ctx:
    def __init__(self, dry_run: bool, out, now: Callable[[], datetime]):
        self.dry_run = dry_run
        self.out = out
        self.stamp = now().strftime("%Y%m%dT%H%M%SZ")
        self.changed: list[Path] = []

    def _backup(self, path: Path, backup_dir: Path | None) -> None:
        folder = backup_dir or path.parent
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        name = f"{path.name}.treant-bak-{self.stamp}"
        bak, n = folder / name, 1
        while bak.exists():  # a second run within the same second keeps the first backup
            bak, n = folder / f"{name}-{n}", n + 1
        shutil.copy2(path, bak)

    def write(self, path: Path, new: str | None, backup_dir: Path | None = None) -> bool:
        """Write ``new`` (None = delete) with a backup (next to the file, or in ``backup_dir``);
        True if anything changed."""
        old = _read_text(path)
        target = "" if new is None else new
        if old == target and (new is not None or not path.exists()):
            return False
        diff = difflib.unified_diff(
            old.splitlines(keepends=True),
            target.splitlines(keepends=True),
            f"a/{path}",
            f"b/{path}",
        )
        self.out("".join(diff))
        if self.dry_run:
            return True
        path = path.resolve()  # a symlinked (dotfiles-managed) file keeps its link
        if path.exists():
            self._backup(path, backup_dir)
        if new is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_write(path, new)
        self.changed.append(path)
        return True

    def edit_json(self, path: Path, edit: Callable[[dict], dict]) -> bool:
        """Apply ``edit`` to the JSON object in ``path``; the file is rewritten only when the
        object changed, so a file without Treant entries keeps its formatting."""
        data = _load_json(path)
        new = edit(json.loads(json.dumps(data)))
        if new == data and path.exists():
            return False
        return self.write(path, _dump_json(new))


def _mkdir_private(path: Path, ctx: _Ctx) -> None:
    if ctx.dry_run:
        ctx.out(f"would ensure {path} is mode 0700\n")
        return
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _lctl(run, *args: str) -> subprocess.CompletedProcess:
    return run(["launchctl", *args], capture_output=True, text=True, timeout=30)


def _remove_legacy_job(paths: Paths, ctx: _Ctx, run, uid: int) -> None:
    """Boot out the job under its old label (if its plist exists or it is loaded) and remove
    the plist, with a backup."""
    target = f"gui/{uid}/{LEGACY_LABEL}"
    if ctx.dry_run:
        if paths.legacy_plist.exists():
            ctx.out(f"would run: launchctl bootout {target}\n")
    elif paths.legacy_plist.exists() or _lctl(run, "print", target).returncode == 0:
        _lctl(run, "bootout", target)
    if paths.legacy_plist.exists():
        ctx.write(paths.legacy_plist, None, paths.backup_dir)


def _clean_kiro_agents(paths: Paths, ctx: _Ctx) -> None:
    """Remove Treant's hooks and MCP entry from every Kiro agent that holds them; other files
    and entries are left as they are."""
    for path in sorted(paths.kiro_agents.glob("*.json")):
        try:
            agent = _load_json(path)
        except InstallError as e:
            ctx.out(f"warning: {e}\n")
            continue
        cleaned = remove_kiro(json.loads(json.dumps(agent)))
        if cleaned != agent and ctx.write(path, _dump_json(cleaned)):
            ctx.out(f"{'would remove' if ctx.dry_run else 'removed'} Treant hooks from {path}\n")


BOOTSTRAP_TRIES = 3


def _load_cfg(cfg):
    if cfg is not None:
        return cfg
    try:
        return _config.load_config()
    except Exception as e:  # a bad config.toml or guard config
        raise InstallError(f"could not load the Treant config: {type(e).__name__}") from e


def _stopped(ctx: _Ctx | None) -> None:
    if ctx is not None and ctx.changed and not ctx.dry_run:
        files = "\n".join(f"  {p}" for p in ctx.changed)
        print(
            "install stopped part way; already changed (each has a .treant-bak-* backup):\n"
            + files
            + "\nfix the error and run install again",
            file=sys.stderr,
        )


def install(
    *,
    dry_run: bool = False,
    binary: str | None = None,
    home: Path | None = None,
    cfg=None,
    run=subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    out: Callable[[str], None] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    uid: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    out = out or (lambda s: sys.stdout.write(s))
    ctx: _Ctx | None = None
    try:
        binary = binary or find_binary(which)
        path = sweep_path(which)
        cfg = _load_cfg(cfg)
        paths = Paths.for_home(home or Path.home(), cfg.kiro_agent)
        ctx = _Ctx(dry_run, out, now)
        uid = os.getuid() if uid is None else uid

        _mkdir_private(paths.state_dir, ctx)
        _mkdir_private(paths.log_dir, ctx)

        # Kiro saving starts now: the sweep never sends conversations from before install.
        if read_since(paths.state_dir) is None:
            if dry_run:
                out(f"would write {paths.state_dir / SINCE_FILE} (Kiro saving starts now)\n")
            else:
                ensure_since(paths.state_dir, int(now().timestamp() * 1000))

        settings = _load_json(paths.settings)
        ctx.write(paths.settings, _dump_json(add_claude_hooks(settings, binary)))

        _install_claude_mcp(binary, ctx, run)

        if paths.kiro_agent is None:
            out("skipped Kiro: set kiro_agent in config.toml\n")
        elif paths.kiro_agent.exists():
            agent = _load_json(paths.kiro_agent)
            ctx.write(paths.kiro_agent, _dump_json(add_kiro(agent, binary)))
        else:
            out(f"skipped Kiro: {paths.kiro_agent} does not exist\n")

        _remove_legacy_job(paths, ctx, run, uid)
        changed = ctx.write(paths.plist, plist_text(binary, path), paths.backup_dir)
        if dry_run:
            out(f"would run: launchctl bootout/bootstrap gui/{uid} {paths.plist}\n")
        elif changed or _lctl(run, "print", f"gui/{uid}/{PLIST_LABEL}").returncode != 0:
            _lctl(run, "bootout", f"gui/{uid}", str(paths.plist))
            # bootout finishes asynchronously; an immediate bootstrap can fail (error 5)
            for attempt in range(BOOTSTRAP_TRIES):
                if _lctl(run, "bootstrap", f"gui/{uid}", str(paths.plist)).returncode == 0:
                    break
                if attempt + 1 < BOOTSTRAP_TRIES:
                    sleep(1.0)
            else:
                raise InstallError("launchctl bootstrap failed")
    except InstallError as e:
        print(f"error: {e}", file=sys.stderr)
        _stopped(ctx)
        return 1
    return 0


def _registered_command(text: str) -> str | None:
    """The ``Command:`` value from ``claude mcp get`` output, if there is one."""
    for line in text.splitlines():
        key, sep, value = line.strip().partition(":")
        if sep and key == "Command":
            return value.strip()
    return None


def _install_claude_mcp(binary: str, ctx: _Ctx, run) -> None:
    spec = json.dumps({"type": "stdio", "command": binary, "args": ["mcp-proxy"]})
    cmd = ["claude", "mcp", "add-json", "-s", "user", "treant", spec]
    if ctx.dry_run:
        ctx.out(
            "would run (unless `claude mcp get treant` already shows this binary): "
            + " ".join(cmd)
            + "\n"
        )
        return
    kw = {"capture_output": True, "text": True, "timeout": 30}
    try:
        got = run(["claude", "mcp", "get", "treant"], **kw)
        if got.returncode == 0:
            if _registered_command(got.stdout or "") == binary:
                return
            # a stale path (or output we cannot read): replace the entry
            if run(["claude", "mcp", "remove", "-s", "user", "treant"], **kw).returncode != 0:
                raise InstallError("`claude mcp remove` failed")
        if run(cmd, **kw).returncode != 0:
            raise InstallError("`claude mcp add-json` failed")
    except (OSError, subprocess.SubprocessError) as e:
        raise InstallError(f"could not run claude: {type(e).__name__}") from e


def uninstall(
    *,
    dry_run: bool = False,
    home: Path | None = None,
    cfg=None,
    run=subprocess.run,
    out: Callable[[str], None] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    uid: int | None = None,
) -> int:
    out = out or (lambda s: sys.stdout.write(s))
    try:
        cfg = _load_cfg(cfg)
        paths = Paths.for_home(home or Path.home(), cfg.kiro_agent)
        ctx = _Ctx(dry_run, out, now)
        uid = os.getuid() if uid is None else uid

        if paths.settings.exists():
            ctx.edit_json(paths.settings, remove_claude_hooks)
        if paths.kiro_agent is not None and paths.kiro_agent.exists():
            ctx.edit_json(paths.kiro_agent, remove_kiro)
        elif paths.kiro_agent is None:
            _clean_kiro_agents(paths, ctx)

        target = f"gui/{uid}/{PLIST_LABEL}"
        if dry_run:
            out("would run: claude mcp remove -s user treant (if present)\n")
            out(f"would run: launchctl bootout gui/{uid} {paths.plist}\n")
        else:
            try:
                if (
                    run(
                        ["claude", "mcp", "get", "treant"],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    ).returncode
                    == 0
                ):
                    run(
                        ["claude", "mcp", "remove", "-s", "user", "treant"],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
            except (OSError, subprocess.SubprocessError):
                out("warning: could not run claude; remove the treant MCP entry by hand\n")
            if paths.plist.exists():
                _lctl(run, "bootout", f"gui/{uid}", str(paths.plist))
            elif _lctl(run, "print", target).returncode == 0:
                _lctl(run, "bootout", target)  # loaded, but its plist is already gone
        if paths.plist.exists():
            ctx.write(paths.plist, None, paths.backup_dir)
        _remove_legacy_job(paths, ctx, run, uid)
    except InstallError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0
