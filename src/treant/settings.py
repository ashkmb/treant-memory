"""Per-workspace settings stored in ``control.workspaces.settings`` (jsonb, default ``{}``).

Layout: ``{"backend","alias","judge_alias","publish","rules":{"domains","ticket_prefixes",
"terms"}}``. Parsing is strict: an unknown key or a wrong type is ``InvalidInput``. Messages
name a key, never a value.
"""

from dataclasses import dataclass
from typing import Any

from treant.db import Database
from treant.errors import InvalidInput

BACKENDS = ("litellm", "none")
PUBLISH_MODES = ("auto", "review")
_TOP = {"backend", "alias", "judge_alias", "publish", "rules"}
_RULES = ("domains", "ticket_prefixes", "terms")
MAX_RULE_ENTRIES = 200
MIN_TERM_LEN = 3


@dataclass(frozen=True)
class WorkspaceSettings:
    backend: str = "litellm"
    alias: str = "default"
    judge_alias: str = "default"
    publish: str = "auto"
    domains: tuple[str, ...] = ()
    ticket_prefixes: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()


def _string(raw: dict, key: str, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or not value:
        raise InvalidInput(f"setting {key} must be a non-empty string")
    return value


def _strings(raw: dict, key: str) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise InvalidInput(f"setting rules.{key} must be a list of strings")
    stripped = tuple(v.strip() for v in value)
    if not all(stripped):
        raise InvalidInput(f"setting rules.{key} must not contain empty entries")
    if len(stripped) > MAX_RULE_ENTRIES:
        raise InvalidInput(f"setting rules.{key} has too many entries")
    if key == "terms" and any(len(v) < MIN_TERM_LEN for v in stripped):
        raise InvalidInput("setting rules.terms entries must be at least 3 characters")
    return stripped


def parse(raw: dict) -> WorkspaceSettings:
    if not isinstance(raw, dict):
        raise InvalidInput("settings must be an object")
    unknown = set(raw) - _TOP
    if unknown:
        raise InvalidInput("unknown setting key")
    rules = raw.get("rules", {})
    if not isinstance(rules, dict):
        raise InvalidInput("setting rules must be an object")
    if set(rules) - set(_RULES):
        raise InvalidInput("unknown rules key")
    backend = _string(raw, "backend", "litellm")
    if backend.startswith("delegate:"):
        raise InvalidInput("not available yet")
    if backend not in BACKENDS:
        raise InvalidInput("setting backend must be litellm or none")
    publish = _string(raw, "publish", "auto")
    if publish not in PUBLISH_MODES:
        raise InvalidInput("setting publish must be auto or review")
    return WorkspaceSettings(
        backend=backend,
        alias=_string(raw, "alias", "default"),
        judge_alias=_string(raw, "judge_alias", "default"),
        publish=publish,
        domains=_strings(rules, "domains"),
        ticket_prefixes=_strings(rules, "ticket_prefixes"),
        terms=_strings(rules, "terms"),
    )


def dump(s: WorkspaceSettings) -> dict[str, Any]:
    return {
        "backend": s.backend,
        "alias": s.alias,
        "judge_alias": s.judge_alias,
        "publish": s.publish,
        "rules": {
            "domains": list(s.domains),
            "ticket_prefixes": list(s.ticket_prefixes),
            "terms": list(s.terms),
        },
    }


def load(db: Database, ws: str) -> WorkspaceSettings:
    with db.control_tx() as conn:
        row = conn.execute(
            "SELECT settings FROM control.workspaces WHERE name = %s", (ws,)
        ).fetchone()
    if row is None:
        raise InvalidInput("workspace not found")
    return parse(row[0] or {})


def load_all(db: Database) -> dict[str, WorkspaceSettings]:
    """Settings of every workspace, active and archived.

    Fail-closed on purpose: any row that does not parse raises, because the gate's deny list
    must never run with a workspace's rules missing.
    """
    with db.control_tx() as conn:
        rows = conn.execute("SELECT name, settings FROM control.workspaces").fetchall()
    return {name: parse(raw or {}) for name, raw in rows}
