"""The seven MCP tools as plain sync methods over the verified token's workspace.

Every method runs through ``_run``: action check, then the DB work, with every failure mapped
to ``{"error": "<category>"}``. Nothing from an exception message ever reaches the caller or
the log.
"""

import logging
import uuid
from collections.abc import Callable
from typing import Any

from treant import auth, brief, graph, guard, jobs, owner, search, sessions, settings
from treant.db import Database
from treant.embed import Embedder
from treant.errors import InvalidInput, NotFound
from treant.scrub import scrub

log = logging.getLogger(__name__)

MAX_MISTAKE = owner.MAX_MISTAKE
MAX_FACT_TEXT = owner.MAX_FACT_TEXT
_SETTABLE_STATUS = {"current", "closed"}
# A token may edit current facts and close or reopen open threads. Undoing a retraction or a
# supersession is an owner action (graph.update_fact stays general for that).
_FINAL_STATUS = {"retracted", "superseded"}


def _fact_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise NotFound("fact not found") from None


class Tools:
    def __init__(self, db: Database, embedder: Embedder) -> None:
        self.db = db
        self.embedder = embedder

    def _run(self, action: str, fn: Callable[[Any], dict]) -> dict:
        try:
            ctx = auth.current()
            if action not in ctx.actions:
                return {"error": "forbidden"}
            return fn(ctx)
        except NotFound:
            return {"error": "not found"}
        except InvalidInput:
            return {"error": "invalid input"}
        except Exception as e:  # noqa: BLE001 - category only, never str(e)
            log.error("tool failed category=%s", type(e).__name__)
            return {"error": "internal error"}

    def save_session(
        self, client_session_id: str, sections: dict[str, Any], unsure: bool = False
    ) -> dict:
        def go(ctx):
            res = sessions.save_session(self.db, ctx, client_session_id, sections, unsure)
            out: dict[str, Any] = {"session_id": res.session_id, "revision": res.revision}
            if res.quarantined:
                out["quarantined"] = True
            return out

        return self._run("create", go)

    def search(self, query: str, limit: int = 8) -> dict:
        def go(ctx):
            res = search.search(self.db, self.embedder, ctx, query, limit)
            return {"mode": res.mode, "hits": res.hits}

        return self._run("read", go)

    def brief(self, repo: str | None = None, repo_only: bool = False) -> dict:
        def go(ctx):
            if not isinstance(repo_only, bool):
                raise InvalidInput("repo_only")
            out: dict = {"brief": brief.brief(self.db, ctx, repo, repo_only)}
            if repo_only:  # lets a caller tell this from a server that ignored the flag
                out["repo_only"] = True
            return out

        return self._run("read", go)

    def guard_check(
        self,
        command: str,
        cwd: str,
        kube_context: str | None = None,
        aws_profile: str | None = None,
        git_remotes: dict[str, str] | None = None,
        ansible_inventory: str | None = None,
    ) -> dict:
        def go(ctx):
            env = guard.GuardEnv(
                cwd, kube_context, aws_profile, dict(git_remotes or {}), ansible_inventory
            )
            return guard.guard_check(self.db, ctx, command, env)

        return self._run("read", go)

    def warnings(self, names: list[str]) -> dict:
        return self._run("read", lambda ctx: {"warnings": guard.warnings_for(self.db, ctx, names)})

    def update_fact(self, fact_id: str, text: str | None = None, status: str | None = None) -> dict:
        def go(ctx):
            if status is not None and status not in _SETTABLE_STATUS:
                raise InvalidInput("status")
            if text is not None and (
                not isinstance(text, str) or not text.strip() or len(text) > MAX_FACT_TEXT
            ):
                raise InvalidInput("text")
            new_text = None
            if text is not None:  # fact text from a token passes the server scrubber too
                cleaned = scrub(text, settings.load(self.db, ctx.workspace).terms)
                if not cleaned.confident:
                    raise InvalidInput("text")
                new_text = cleaned.text
            fid = _fact_uuid(fact_id)
            new_status = status
            with self.db.ws_tx(ctx.workspace) as conn:
                current, old_text, old_version = graph.fact_state(conn, ctx.workspace, fid)
                if current in _FINAL_STATUS:
                    raise InvalidInput("fact is final")
                if new_status == current:  # already there
                    new_status = None
                elif new_status == "current" and current != "closed":
                    raise InvalidInput("only a closed thread can be reopened")
                if new_text == old_text:
                    new_text = None
                if new_text is None and new_status is None:  # nothing changes: no new version
                    return {"version": old_version}
                version = graph.update_fact(conn, ctx.workspace, fid, new_text, new_status)
                if new_text is not None:
                    # update_fact dropped the stale vector; the worker re-embeds pending facts.
                    jobs.enqueue(
                        conn,
                        ctx.workspace,
                        "embed",
                        f"fact:{fid}:{version}",
                        {"revision_id": None},
                    )
            return {"version": version}

        return self._run("update", go)

    def retract(self, fact_id: str) -> dict:
        def go(ctx):
            fid = _fact_uuid(fact_id)
            with self.db.ws_tx(ctx.workspace) as conn:
                graph.retract(conn, ctx.workspace, "fact", fid)
            return {"ok": True}

        return self._run("delete", go)

    def report_mistake(self, text: str) -> dict:
        def go(ctx):
            owner.check_mistake_text(text)
            with self.db.ws_tx(ctx.workspace) as conn:
                # No RETURNING: the workspace role may insert but not select here.
                conn.execute(
                    "INSERT INTO control.mistakes (workspace, text) VALUES (%s, %s)",
                    (ctx.workspace, text),
                )
            return {"ok": True}

        return self._run("create", go)
