# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub's security advisories: on this
repository's Security tab, choose "Report a vulnerability". Do not open a public issue for
them.

Include what you found, how to reproduce it, and which version or commit you tested. You will
get an answer as soon as possible; this is a small project, so please allow a few days.

## Scope

Of most interest:

- one workspace reading or changing another workspace's memory (tokens, database roles, search,
  brief, the graph walk, the gate, the dashboard);
- secrets or private data surviving the scrubbers or reaching `general`;
- the dashboard's login, re-authentication, CSRF or session handling;
- the guard allowing a command it should ask about or deny, where the README does not list the
  case as a known gap.

## Supported versions

Only the latest commit on the default branch is supported while the project is at 0.x.
