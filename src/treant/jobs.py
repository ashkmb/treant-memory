"""Durable job queue in ``control.jobs``.

Workspace roles may only INSERT (under an RLS policy) and cannot read the table, so
``enqueue`` never uses RETURNING. Workers claim, complete and fail through the control role.
"""

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from treant.db import Database

_MAX_BACKOFF_S = 6 * 3600
_BASE_BACKOFF_S = 30


@dataclass(frozen=True)
class Job:
    id: int
    workspace: str
    kind: str
    key: str
    payload: dict[str, Any]
    attempts: int
    lease: str


def enqueue(
    conn: psycopg.Connection,
    workspace: str,
    kind: str,
    key: str,
    payload: dict[str, Any],
    run_after_s: int = 0,
) -> None:
    """Insert a queued job on the caller's connection; a duplicate (workspace, kind, key) is a no-op."""
    conn.execute(
        "INSERT INTO control.jobs (workspace, kind, key, payload, run_after) "
        "VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s)) "
        "ON CONFLICT DO NOTHING",
        (workspace, kind, key, Jsonb(payload), run_after_s),
    )


def claim(db: Database, kinds: set[str], lease_s: int = 300) -> Job | None:
    """Lease one ready job. A lease that expired makes a running job claimable again."""
    with db.control_tx() as conn:
        row = conn.execute(
            "UPDATE control.jobs SET status = 'running', lease_id = gen_random_uuid(), "
            "lease_until = now() + make_interval(secs => %(lease)s) "
            "WHERE id = ("
            "  SELECT id FROM control.jobs WHERE kind = ANY(%(kinds)s) AND ("
            "    (status = 'queued' AND run_after <= now()) "
            "    OR (status = 'running' AND lease_until < now())) "
            "  ORDER BY run_after, id LIMIT 1 FOR UPDATE SKIP LOCKED) "
            "RETURNING id, workspace, kind, key, payload, attempts, lease_id::text",
            {"lease": lease_s, "kinds": sorted(kinds)},
        ).fetchone()
    return Job(*row) if row else None


FINAL_STATUSES = ("done", "skipped")


def complete(db: Database, job: Job, status: str = "done") -> bool:
    """Mark a running job ``done`` (or ``skipped``: it had nothing to do); False if the lease
    was lost or the job is no longer running."""
    if status not in FINAL_STATUSES:
        raise ValueError("status must be done or skipped")
    with db.control_tx() as conn:
        cur = conn.execute(
            "UPDATE control.jobs SET status = %s, lease_until = NULL, error_category = NULL "
            "WHERE id = %s AND lease_id = %s AND status = 'running'",
            (status, job.id, job.lease),
        )
    return cur.rowcount == 1


def fail(db: Database, job: Job, category: str, max_attempts: int = 8) -> bool:
    """Record a failed attempt: back off exponentially, or dead-letter at max_attempts."""
    with db.control_tx() as conn:
        cur = conn.execute(
            "UPDATE control.jobs SET attempts = attempts + 1, error_category = %(cat)s, "
            "lease_until = NULL, "
            "status = CASE WHEN attempts + 1 >= %(max)s THEN 'dead' ELSE 'queued' END, "
            "run_after = now() + make_interval(secs => "
            "  least(power(2, attempts + 1) * %(base)s, %(cap)s)) "
            "WHERE id = %(id)s AND lease_id = %(lease)s AND status = 'running'",
            {
                "cat": category,
                "max": max_attempts,
                "base": _BASE_BACKOFF_S,
                "cap": _MAX_BACKOFF_S,
                "id": job.id,
                "lease": job.lease,
            },
        )
    return cur.rowcount == 1
