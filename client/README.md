# treant-client

Claude Code and Kiro adapters for the Treant memory server. Standard library only, plus
`treant-guard` (`../guard`, also standard library only), which `uv` installs with it.

## Install

    uv tool install ./client          # puts treant-client on PATH (pulls in ./guard)
    treant-client install --dry-run   # review the diffs
    treant-client install

`install` is idempotent and backs each changed file up to `<file>.treant-bak-<UTC timestamp>`
(launchd plists are backed up to `~/.local/state/treant/backups/`, not `LaunchAgents`). If it
stops part way it lists the files it already changed; fix the error and run it again.
It edits `~/.claude/settings.json` (SessionStart, PreToolUse/Bash, SessionEnd, PreCompact hooks),
registers the `treant` MCP proxy with `claude mcp add-json -s user`, merges hooks and the MCP
server into `~/.kiro/agents/<kiro_agent>.json`, installs the launchd job
`~/Library/LaunchAgents/org.treant.kiro-sweep.plist`, and sets the state and log
directories to mode 0700.

## Tokens

One token per workspace, kept only in the macOS Keychain (service `treant`, account = workspace).

    treant-client token set work      # prompts; the token never appears in argv
    treant-client token check         # per workspace: missing, or set + ok / error category

## What runs when

- Session start (Claude Code `SessionStart`, Kiro `agentSpawn`): injects a note
  (`Workspace: <ws> (folder ~/...).`, then the workspace's `[about]` text from the guard config),
  followed by the workspace brief when Treant answers. The note appears even without a token or
  with the server down; an unmapped folder gets nothing.
- Before a shell command (`PreToolUse` Bash, Kiro `preToolUse`): `treant-guard` checks it
  locally against the workspace's targets in `~/.config/treant/guard.toml` (and `guard.d/*.toml`);
  no token or network needed. When a rule matched and the workspace has a token, the server's
  `warnings` lookup may add memory warnings (skipped silently when slow or failing). On a
  finding Claude Code is asked to confirm; Kiro blocks once and allows the identical retry
  for 15 minutes. A target in `[deny]` is refused instead: Claude Code gets `deny` (no
  warnings lookup), Kiro blocks every time with no retry. See `../guard/README.md` for rules,
  targets and `treant-guard check-config`.
- `SessionEnd` / `PreCompact`: a detached worker summarizes, scrubs and saves the session.
- After a save: the stale check runs for the session's git repository when its `HEAD` moved
  since the last check (memo `~/.local/state/treant/stale-heads.json`). It asks the server
  for the repository's facts (at most 200 facts / 30 s per run), looks up the file paths and
  code names each fact mentions with `git ls-tree` / `git grep -q` at `HEAD`, and flags a fact whose
  tokens are gone for the owner to keep or retract. It sends only tokens taken from the fact
  text plus commit ids and subjects, never file contents, and needs a token with `update` to
  flag. Anything uncertain counts as present; a weak token (a bare file name such as
  `README.md`, a CamelCase or dotted name such as `GitHub` or `os.path`) counts as missing only
  when `git log` finds the commit that removed it. Run it by hand with
  `treant-client check-stale [--repo PATH] [--dry-run]` (`--dry-run` prints what would be
  flagged; the by-hand run ignores the memo).
- launchd, every 10 minutes: `kiro-sweep` saves Kiro conversations idle for 30 minutes and
  last updated after install (watermark `~/.local/state/treant/kiro-since`; write `0` to backfill).
  It reads Kiro's old `conversations_v2` table and the v3 engine's `~/.kiro/sessions/` folders.
- `treant-client mcp-proxy`: stdio MCP proxy; picks the token from the session folder
  (Claude Code: `CLAUDE_PROJECT_DIR`, else its folder). Kiro's entry adds `--client kiro`, so
  a `CLAUDE_PROJECT_DIR` inherited from a Claude Code shell that started Kiro is ignored.

Folders outside the configured map get no Treant behavior at all, except that while the
guard config is invalid, commands the shipped rule pack would check ask in every folder.
The folder map is
`[workspaces]` in `~/.config/treant/guard.toml`; while that is missing, empty or invalid,
`[folders]` in `~/.config/treant/config.toml` is used (this fallback stays; `[about]` is
empty then). With neither, the map is empty: no workspace, no note, no memory.
The memory hooks fail open. The guard fails closed: a command a rule would check asks
(Kiro: blocks once) on an invalid guard config (`guard config invalid`), an internal error
(`guard error`) or when the 3 s hook budget runs out (`guard timed out`).

## Configuration

The client ships no defaults that belong to anyone: set what you need in
`~/.config/treant/config.toml`. Every key is optional.

```toml
url = "https://treant.example.com/mcp"   # the Treant server; unset = memory off
kiro_agent = "me"                        # ~/.kiro/agents/me.json; unset = install skips Kiro

[folders]                                # fallback map when guard.toml has no [workspaces]
"~/work" = "work"

[scrub_host_terms]                       # extra hostname terms removed before saving, per workspace
work = ["acme"]
```

| Key | Unset means |
|---|---|
| `url` | Memory is off: no brief, no save, no warnings lookup; `mcp-proxy` answers tool calls with `Treant: no url configured in ~/.config/treant/config.toml`. The guard and the workspace note still work. |
| `[folders]` | Empty map: no note and no memory unless `guard.toml` has `[workspaces]`. |
| `[scrub_host_terms]` | No extra scrub terms. |
| `kiro_agent` | `install` prints `skipped Kiro: set kiro_agent in config.toml` and touches no Kiro file; `uninstall` cleans every agent file (see below). |

## Uninstall

    treant-client uninstall --dry-run
    treant-client uninstall

Removes exactly what `install` added (backups are made first). With `kiro_agent` unset it removes
Treant's hooks and MCP entry from every `~/.kiro/agents/*.json` that holds them, leaves every
other entry as it was, skips a file that is not valid JSON with a warning, and prints each
file it cleaned. Tokens stay in the Keychain; delete with
`security delete-generic-password -s treant -a <workspace>`.

## Logs

`~/Library/Logs/treant/client.log`: event name, workspace, status, duration and error category
only, never commands, transcripts, summaries or tokens.
