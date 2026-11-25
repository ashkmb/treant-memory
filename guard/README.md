# treant-guard

Standalone, stdlib-only client that checks risky shell commands against rules loaded from a
shipped pack and the user's config directory. It makes no network calls and has no runtime
dependencies. See ../docs/architecture.md for how it fits into Treant.

## Config

`~/.config/treant/guard.toml`, plus `~/.config/treant/guard.d/*.toml` (read in sorted order).
Later files win for the same folder; target lists are merged.

```toml
[guard]
mode = "protect"                 # the default; "allowlist", "production" and "off" below

[guard.modes]                    # optional per-workspace mode
startup = "production"

[workspaces]                     # folder → workspace; longest match wins
"~/Documents/Projects/Acme" = "acme"
"~/Documents/Projects/Personal" = "personal"

[protect."*"]                    # production: always asks, in every workspace
kube_context = ["production"]
aws_profile = ["default", "acme-prod"]

[allow.acme]                     # the workspace's own targets
kube_context = ["ops", "pre-prod"]
aws_profile = ["acme-ops"]
git_remote_host = ["git.acme.example"]
terraform_dir = ["~/Documents/Projects/Acme"]

[deny.acme]                      # never runs from this workspace; deny wins over allow
kube_context = ["prod"]

[deny."*"]                       # denied in every workspace
aws_profile = ["root"]

[about]                          # shown to the session at start (max 2000 characters each)
acme = "Employer work. Prod is read-only."
```

Folder keys may start with `~` or `~/` (your home), never `~user`; `"*"` is not a workspace
name (it means every workspace). Folders outside the map are not checked. Across files, allow, deny and protect lists are unioned; `[protect]` and `[deny]`
take a workspace or `"*"` (every workspace), `[allow]` only a workspace. `[guard]` belongs in
`guard.toml` only: in a `guard.d` file it makes that file invalid, so no drop-in can change
the mode.

For a command a rule matches in a mapped folder, each target it acts on is looked up in order:

| Target is … | `protect` mode (default) | `production` mode | `allowlist` mode |
|---|---|---|---|
| denied (`[deny.<ws>]` or `[deny."*"]`) | refused | refused | refused |
| protected (`[protect.<ws>]` or `[protect."*"]`) | asks | asks | asks |
| in the workspace's own `[allow.<ws>]` | runs | runs | runs |
| in another workspace's `[allow]` only | asks ("belongs to …") | runs | asks |
| listed nowhere | runs | runs | asks |
| can't tell (a computed value, the rules below) | runs | runs | asks |
| the line does not lex | runs | runs | asks |

`[guard.modes]` sets the mode of one workspace (`"*"` is not allowed there; `mode` covers every
workspace). Folders of a workspace named there are mapped in `guard.toml` only, so no
drop-in can change a folder's mode. `production` is protect mode without the "belongs to …" asks: use it for a workspace
where only production targets need a second look. Below, "protect mode" covers it too.
`off` turns the guard off for that workspace: every command runs, deny and protect lists
included. The workspace still scopes Treant memory; only the guard steps aside.

Refused means Claude Code `deny`, Kiro blocked every time with no retry. An invalid config, an
internal error or the hook running out of time asks in both modes. In protect mode a protected
target also asks when the workspace allows it, and a word given to a tool switch
(`kubectx prod`, `kubectl config use-context prod`, see below) is checked like a target, so
switching to a production context asks even before anything runs against it.

What protect mode trusts: a target the guard can't tell runs. `kubectl --context "$C"`, a
`[[switch]]` with no readable argument (`kubectx -`, a fuzzy picker), or anything else this
README says "can't tell" for runs without a prompt, even if it ends up on production. The
same holds for deny: a denied target is refused when the guard can read it, and runs when it
can't (`bash -c 'kubectl --context prod …'` with `prod` denied); deny is not a hard stop in
protect mode. List production in `[protect]`, keep `--context`/`--profile` on commands that
matter, or use `mode = "allowlist"` where that is not enough. Below, where a can't-tell case
"asks", that is allowlist mode; protect mode runs it.

Commands:

    treant-guard init                                   # write a commented guard.toml once
    treant-guard check-config                           # "ok: …" plus warnings, or "invalid: <file>: <hint>" (exit 1)
    treant-guard check --cwd DIR -- kubectl get po      # print allow / ask / deny + reason; exit 0 / 1 / 2
    ssh <host> "sudo treant target list <ws>" | treant-guard import <ws>

`check-config` warns about a kind no rule uses, a table naming a workspace not in
`[workspaces]`, and an allow entry that never takes effect: also denied in its workspace,
inside a folder denied there, denied everywhere, or a host written with capitals.

`import` reads `kind<TAB>value` lines (UTF-8; old kind names are translated) and writes `guard.d/<ws>.toml`: `[allow.<ws>]` from stdin plus the file's existing `[deny.<ws>]` and `[protect.<ws>]`. It refuses a file holding anything else, and leaves the file as is when stdin holds no target but the file has an allow list (a failed `ssh` must not wipe it); edit the file to empty a list.

## Rules

The shipped pack (`src/treant_guard/rules/default.toml`) covers `kubectl`, `helm`, `aws`,
`terraform`, `terragrunt`, `git push`, `ansible-playbook` and `wrangler`. Add `[[rule]]`
tables to the user config to add tools; a rule with the same `command` and `kind` as a
shipped one replaces it, and `disabled = true` drops it.

A rule says which command it matches and where the target value comes from. The value is
taken from `flags`, then `env`, then `config`, then `default_from`; a matched rule that finds
no value reports its kind as undeterminable, which asks. `check-config` prints `: <hint>` after
`invalid: <file>` only when there is a hint.

| Field | Meaning |
|---|---|
| `command` | Alternative program basenames, e.g. `["kubectl"]`; the rule matches when the command's basename is any of them. Not a word sequence (required). |
| `kind` | Name of the target kind, used as the key in `[allow.<ws>]` / `[deny.<ws>]` (required). |
| `subcommands` / `skip_subcommands` | Only these subcommands, or all but these (mutually exclusive). |
| `global_flags_with_value` | Options before the subcommand that take a value, so it can be found. |
| `flags`, `short_flag` | Options carrying the value (`--context ops`, `--context=ops`). `short_flag` (a dash and one letter) adds only the attached form (`-iHOST`); the separate form (`-i HOST`) must be listed in `flags`. A blank value (`--context ""`, `--profile=`) is no value: the tool falls back to its default, so `env`, `config` and `default_from` are read as well and the kind is also "can't tell". |
| `env` | Environment variables carrying the value. |
| `config` | Project file lookup: `files`, `format` (`auto`, `ini`, `toml`, `json`, `jsonc`), `key` (dotted), `first_found_only`, `search` (`cwd` or `upward`). Parsed as data, never executed. |
| `default_from` | Command whose output is the value when nothing else is set. It runs in the command's folder with the command's environment (the hook's, plus exports, unsets and the prefix earlier on the line); when that environment holds a variable the guard can't read, the kind can't tell. |
| `unknown_if` | `-option` or `$VAR` that makes the value undeterminable (asks). Used only when no flag value was found; a `$VAR` entry counts only when the variable is assigned on the command line or earlier on the same line, not when it is merely in the hook's environment (use `env` for that). |
| `all_values` | Check every value found, not just the last or first. |
| `split` | Separator for several values in one string, e.g. `","`; applies to values from `env` and `config`. |
| `match` | `exact` (default) or `path_prefix`: the value is a folder, listed entries cover their subfolders. |
| `resolver` | Built-in resolver (`terraform_dir`, `git_push_host`); not combinable with `flags`, `env`, `config`, `default_from` or `unknown_if`. |
| `disabled` | Drop the shipped rule with this `command` and `kind`. |

No list may hold an empty string, `subcommands` and `default_from` must not be empty lists and
`split` must not be empty: such a rule would check nothing, so it makes the config invalid.
When one file holds two rules with the same `command` and `kind`, or two files do, the last
one read wins.

```toml
[[rule]]
command = ["kubectl"]
kind = "kube_context"
flags = ["--context"]
unknown_if = ["--kubeconfig", "$KUBECONFIG"]
default_from = ["kubectl", "config", "current-context"]
```

Values of `path_prefix` kinds (today `terraform_dir`) must be absolute paths (`~` is
expanded); a relative one makes the config invalid, and `check-config` names every relative
value of that kind in the file. Deny and protect matching also resolve symlinks, so a link into
a denied folder is denied; an allowed folder must hold the target both as written and as a
real path, so a link inside it that points elsewhere is judged by where it leads. A relative
value that a user `path_prefix` rule reads (`mytool --dir prod`) is also taken from the
command's folder: deny, protect and other workspaces' lists see that folder, while the relative
value itself is never allowed (allowlist mode asks).

Hosts (`git_remote_host`) are read in lower case, and deny, protect and other workspaces'
entries are compared in lower case too; write allow entries in lower case (`check-config` warns
about one with capitals, which never matches).

## Same-line state

A command line is read segment by segment, so `cd prod && terraform apply` is checked against
`prod`. Where the guard can read the new state it uses it; where it cannot, the kinds that
depend on it ask ("can't tell"). A value given by the command's own flag (`--context ops`) still
counts unless the whole line is uncertain.

| Earlier on the line | Effect |
|---|---|
| Followed exactly: `cd`/`pushd DIR`, `export A=b`, `unset A`, `declare -x A=b`, `set -a`, `env -i cmd`, and a lone `A=b` for a variable that is already exported (in the hook environment or exported earlier on the line) or after `set -a` | later segments see the new folder or variable. Exact only through plain `;`, newline and `&&` chains. A lone `A=b` for any other variable changes nothing a tool sees. `set` options that change neither exports nor how words are read are followed as no-ops: `pipefail`, `errexit`, `nounset`, `xtrace`, `verbose`, `noclobber`, `errtrace`, `functrace`, `notify`, `monitor`, `ignoreeof`, `emacs`, `vi`, `history`, `hashall` and `-e -u -x -v -C -E -b -m -h`. After any other `-o` name (zsh spellings of `allexport`, `physical`, `noexec`, ...), or a `set` that changes allexport next to a word only one shell accepts (`set -o PIPE_FAIL -a`), a lone `A=b` is "can't tell". `cd` alone and `cd ~/…` after `HOME` was set go to that `HOME`. A `cd` through a folder that does not exist (`cd dev/x/..` without `dev/x`) fails in both shells: "can't tell". |
| Makes later reads "can't tell": any state change that is not in a plain chain (after `\|\|`, `\|`, `&`, inside `( … )`, `{ … }` or a compound command, or in a longer `&&` list once it ends), a value or target containing `$` or a backtick, `cd -`, `popd`, zsh `chdir`, `cd -PL` (bash and zsh disagree), a possible autocd, `set` or `declare` forms it cannot read, a loop with a change in it, a loop variable (`for A in …`, `select A`, zsh `foreach A (…)`), a name set by `printf -v A`, `read A`, `getopts … A` or `mapfile`/`readarray A`, `HOME` when it is unknown or unset (for `cd` and `~`), and after any change of `HOME` every kind but `terraform_dir` (files under `~` may be other ones), a prefix assignment on `export`/`declare`/`typeset`/`readonly`/`local`/`unset`/`set` (bash keeps `A=x export B`'s `A`) | the changed folder, environment or variable is unknown for the rest of the line: a rule that needs it asks, a flag value still counts |
| Makes the whole line "can't tell": `source`, `.`, `eval`, `alias`, `trap`, `setopt`/`unsetopt`/`emulate`/`shopt`, a function definition, `case`, a comment (`#` at the start of a word outside quotes; inside a quoted word or a quoted here-document body it is text (`<<<` is a here-string, and `<<` inside `$(( … ))` a shift unless that region holds a quote, backslash or newline: neither starts a here-document); but the raw text counts when the line holds any `<<` (a shift too), `$'`, a backslash or a `$( … )` in double quotes), a backtick, `$'…'`, a computed command name (`$K`, `${K}`, `{kubectl,}`, a glob), brace expansion in a matched command, zsh `[[ … ]] cmd`, a `{` word after the command (zsh `if { x } { cmd }`, `while x { cmd }`) | every matched rule asks, flags included; a command the guard cannot name counts as matching every rule |

Both bash and zsh forms are covered, since the hook cannot choose the shell.

How a single command is read:

- **Folders.** `cd -P DIR`, `terraform -chdir=DIR` and `git -C DIR` go where the kernel goes:
  `..` after a symlink is the parent of the link's target. A plain `cd` is logical, as in bash
  and zsh. `terragrunt` acts on `--working-dir` / `--terragrunt-working-dir`, else
  `TG_WORKING_DIR` / `TERRAGRUNT_WORKING_DIR`, else the command's folder; a value it can't read
  can't tell.
- **Wrappers.** The program behind `env`, `sudo`, `doas`, `command`, `exec`, `time`, `nohup`,
  `nice`, `timeout`/`gtimeout`, `stdbuf`, `caffeinate`, `watch`, `coproc`, `setsid`, `arch`,
  `ionice`, `chrt`, `taskset`, `cpulimit`, `xargs` and zsh `noglob`,
  `nocorrect`, `-` is the one checked (their options and values are skipped). zsh `=kubectl` is
  `kubectl`. Leading redirects (`>/dev/null cmd`, `2>&1 cmd`) are skipped, and `>|` is a
  redirect, not a pipe; a redirect among a wrapper's options, before an option's value or
  before its positional (`timeout >/dev/null 5 cmd`, `nice -n >/dev/null 5 cmd`) is skipped
  too. `xargs` (with its options) is a wrapper as well, but stdin may append any word, a flag
  among them: every rule its program matches asks, flags included, and any `[[switch]]` of
  that program counts. Under `xargs`, GNU `parallel` and every `find`
  `-exec`/`-execdir`/`-ok`/`-okdir` action, the command words are unwrapped like any command
  (`xargs nice …`, `-exec env {} …`); every kind asks when nothing is left (stdin names the
  program), when any command word holds a placeholder (`{}`, `{1}`, the xargs `-I`/`-J`/`-i`
  string) unless the program, behind the listed wrappers, never runs its arguments (`echo`,
  `printf`, `cat`, `cp`, `mv`, `rm`, `ls`, `stat`, `file`, `wc`, `head`, `tail`, `grep`,
  `egrep`, `fgrep`, `basename`, `dirname`, `touch`, `mkdir`, `rmdir`, `chmod`, `chown`, `ln`,
  `gzip`, `gunzip`, `du`, `readlink`, `realpath`; a guarded program is not one of them, so
  `find . -exec kubectl apply -f {} \;` asks; the exemption needs every placeholder after
  the program word, since BSD `xargs -J` fills in several words and one in a wrapper's option
  can name the program, and no `parallel` `{=…=}`), when the program is a shell or holds a `$`,
  when a `find` action or `parallel` word names a guarded program,
  when `parallel` has no command of its own (stdin or the `:::` values are the commands), or
  for a long option xargs or `parallel` does not spell out exactly (`--max-a 1`, `--repl=%`),
  and when `parallel` sets a replace string of its own (`-I`, `-U`, `--…replace`). xargs `-i`
  counts inside a cluster of short options (`-ri`). `chrt` takes a priority only when the
  next word is digits (`chrt -o cmd`). Like every
  check, these run only when the line itself names a guarded program (the prefilter):
  `echo kubectl delete ns x | xargs nice` is checked, a command list fed from a file
  (`xargs < cmds.txt`, `parallel < cmds.txt`) is not. `env -i` and `exec -c` pass no hook environment on. `env -S`,
  `--split-string` and BSD `env -P`: what runs can't be read, so every kind asks. `env -C DIR`
  and `sudo -D DIR`/`-R`: that command's folder and environment are "can't tell".
- **Shells.** `bash`/`sh`/`zsh`/`dash`/`ksh`/`mksh`/`yash`/`csh`/`tcsh … -c STRING` (or
  `--command`/`--init-command`) where the string names a guarded program or holds a `$`, or a
  shell reading commands from stdin (`… | bash`, `bash --rcfile F`, `bash -s`): every kind
  asks. `fish`, `nu`, `pwsh` and `xonsh` ask when any word names a guarded program or holds a
  `$`, when they read stdin, and for `pwsh -EncodedCommand`. The string itself is not parsed.
  The same goes for `eval`, `trap`, `alias`, zsh `emulate … -c` and a `mapfile -C` callback:
  text that names a guarded program or holds a `$` makes every kind ask.
- **Substitutions the lexer does not split.** A backtick body, or a `$( … )` inside double
  quotes, is parsed on its own: if it runs a command a rule or switch would match (or one the
  guard can't name), `eval`s such text or may hold a comment, every kind asks. So does a `$( … )` whose end
  bash 3.2 and zsh may read differently: a here-document inside it with an unbalanced `(` or
  `)` in its body (bash 3.2 ends the substitution there and runs the rest), one without its
  end line, or a `case`. `git commit -m "$(cat <<'EOF' … EOF)"` and
  `git log "$(git merge-base HEAD main)"..HEAD` stay readable. An unquoted `$( … )` is read as
  part of the line.
- **git.** `git push` can't tell its host when `GIT_DIR`, `GIT_WORK_TREE`, `GIT_CONFIG`,
  `GIT_CONFIG_GLOBAL`, `GIT_CONFIG_SYSTEM`, `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_*`/`_VALUE_*` or
  `GIT_CONFIG_PARAMETERS` is set (hook environment, earlier on the line or as a prefix), with
  `--git-dir`, `--work-tree`, `--config-env`, a `-c` for `remote.*`, `url.*`, `branch.*` or
  `include*`, or `--repo`. If your shell exports one of these all the time, every push asks.

### Tool switches

Some commands change a tool's state on disk, so a later command reads something the guard
cannot see. A `[[switch]]` table lists them:

```toml
[[switch]]
command = ["kubectl"]                 # program basenames
args = ["config", "use-context"]      # optional: leading words that must follow, in order
kinds = ["kube_context"]              # kinds that become "can't tell"
```

After a matching command, every later segment's `kinds` are "can't tell", whatever the
separator (`|`, `&`, `||`, inside `( … )`, after a subshell closes): the change is on disk.
A flag on the later command (`kubectl --context ops get ns`) still counts. `args` must be the
leading non-option words after the program: options are skipped, and an option may take the
next word as its value (`git -C dir remote add`, `kubectl -n x config use prod`), so the guard
reads every doubtful split as a match. A computed word (`$X`) in a leading position matches
any one word. The same words further on (`git commit -m "remote add"`) are no switch.

The other literal words of a switch (each also split at `=`, so `kubectx new=prod` gives `new`
and `prod`, and `--ctx=prod` gives `prod`) are checked against the lists of its kinds: a
denied word refuses and a protected one asks, in both modes, and in protect mode a word only
another workspace allows asks too; an unlisted word is no finding. For a git host kind a word
that is a remote URL is also checked by its host (`git remote add up git@git.prod.example:r`
counts as `git.prod.example`). Values of options a rule says take a value are skipped; the
word after any other option is kept, which can only add a prompt. Words holding `$` or a
backtick, and words appended by `xargs`, are not read. A switch on its own (`kubectx prod`)
is checked even when no rule's program is on the line.

The shipped pack has `kubectx`, `kubectl config` with `use-context`, `use`, `set-context`,
`set`, `rename-context` and `unset`, `aws configure`, `git remote` with `add`, `set-url`,
`rename`, `remove` and `rm`, and `git config` (reading forms such as
`git config --get remote.origin.url` count too, so a later `git push` asks). Your `[[switch]]`
entries are added to these (they cannot override or disable them). `command` is a non-empty
list of strings, `args` a list of strings and `kinds` a non-empty list, none of them holding an
empty string; a kind that no rule
uses makes `check-config` print `warning: [[switch]] kind <k> is used by no rule`.

### Known gaps

The guard reads only the line it is given, to catch an agent's mistakes, not an adversary.
It does not see:

- aliases or functions from your rc files (an alias such as `gp` for `git push` is invisible;
  add a user rule for the alias name), nor what a script, a Makefile or a task runner runs;
- programs started by another program it does not know: `ssh`, `watchexec`, a task runner
  (`xargs`, `find … -exec`/`-execdir`/`-ok` and `parallel` are read, see above);
- a here-document body with an unbalanced quote (`cat <<EOF` then a lone `"`): the lexer reads
  the quote as opening and the lines after it as text;
- a change to what a name means rather than which one is used: `AWS_CONFIG_FILE`,
  `AWS_SHARED_CREDENTIALS_FILE`, a `KUBECONFIG` whose context of the same name points
  elsewhere (`--context` still counts), `~/.ssh/config` host aliases for a git remote;
- `sudo cmd` runs with root's environment and home; the guard reads the hook's;
- a line that never names a guarded program (`$K delete ns x` with `K` set in your shell, or
  commands fed to `xargs`/`parallel`/a shell from a file or another process): the cheap
  prefilter lets it through before any parsing;
- deliberate obfuscation.

## Trust

Rules come only from the shipped pack and the user config directory, never from the command's
folder or a project. Every config file and its directory must be owned by you and not
group- or world-writable (symlinks are followed). A file that fails this, does not parse or
holds an invalid rule makes the whole config invalid, and so does a `guard.d` that is not a
folder (a dangling link, a file): then every command the shipped pack
would check asks, in any folder, with `guard config invalid: <file>`. Project files such as
`ansible.cfg` or `wrangler.toml` are only parsed for one key, never executed.
