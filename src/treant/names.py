"""Workspace, schema and role names.

Every identifier that reaches SQL is derived here from validated input, so a name
that passes these checks is safe to quote with ``psycopg.sql.Identifier``.
"""

import re

_WORKSPACE = re.compile(r"[a-z][a-z0-9]{1,30}")
# Roles are cluster-wide and Postgres silently truncates identifiers past 63 bytes;
# a short prefix keeps "<prefix>_ws_<31 chars>" well inside that.
_PREFIX = re.compile(r"[a-z][a-z0-9_]{0,23}")
RESERVED = frozenset({"general", "control"})
# The kinds of a workspace target (the CLI's ``target add`` choices and the dashboard's form).
TARGET_KINDS = ("kube_context", "aws_profile", "git_host", "tf_root", "ansible_inventory")
ROLE_KINDS = frozenset({"control", "gate", "publisher", "admin", "login"})


def validate_workspace(name: str) -> str:
    if not isinstance(name, str) or not _WORKSPACE.fullmatch(name) or name in RESERVED:
        raise ValueError("invalid workspace name")
    return name


def valid_prefix(prefix: object) -> bool:
    return isinstance(prefix, str) and _PREFIX.fullmatch(prefix) is not None


def _validate_prefix(prefix: str) -> str:
    if not isinstance(prefix, str) or not _PREFIX.fullmatch(prefix):
        raise ValueError("invalid role prefix")
    return prefix


def ws_schema(name: str) -> str:
    return f"ws_{validate_workspace(name)}"


def ws_role(prefix: str, name: str) -> str:
    return f"{_validate_prefix(prefix)}_ws_{validate_workspace(name)}"


def role(prefix: str, kind: str) -> str:
    if kind not in ROLE_KINDS:
        raise ValueError("invalid role kind")
    return f"{_validate_prefix(prefix)}_{kind}"
