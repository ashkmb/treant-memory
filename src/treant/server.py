"""ASGI app: the MCP streamable-HTTP endpoint behind the bearer middleware, plus /healthz."""

import asyncio
from collections.abc import Callable
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp

from treant import auth
from treant.db import Database
from treant.embed import Embedder
from treant.tokens import TokenContext
from treant.tools import Tools

INSTRUCTIONS = (
    "Treant is long-term memory scoped to the workspace of the folder you are working in, plus "
    "shared general know-how. Call search before the first action on a named system (repo, "
    "cluster, service, ticket) in a session, before proposing a design or a fix, after an error "
    "before retrying, and whenever the user refers to past work. When the user asks you to save, "
    "call save_session with typed sections. Never include secrets, tokens or credentials in "
    "anything you save."
)

SAVE_DESCRIPTION = (
    "Save this session's knowledge to memory. `sections` is an object whose keys are:\n"
    "- journal: what was done and the outcome, a few lines\n"
    "- facts: stable truths learned (where config lives, versions, how things connect)\n"
    "- decisions: what was decided, why, and what was rejected\n"
    "- lessons: surprises and root causes\n"
    "- dead_ends: approaches that failed and why\n"
    "- procedures: steps or commands that worked (no command output)\n"
    "- changes: repos, branches, commits, PRs, tickets, what was applied (pointers only, no diffs)\n"
    "- open_threads: unfinished work, next steps, blockers\n"
    "- preferences: corrections on how the owner wants the AI to work\n"
    "- warnings: dangerous things to check first\n"
    "- client_memory: memory files the client wrote or changed this session\n"
    "Unknown keys are kept inside the journal. Saving the same client_session_id again adds a "
    "revision. Never include secrets. Set unsure=true if you are not sure the summary is free of "
    "secrets; the owner reviews it first."
)


def _token_of(ctx: Context) -> TokenContext | None:
    """The verified token for this very request, from the scope the middleware set.

    MCP handlers may run in a task whose context was copied before this request arrived (the
    session's first request), so the ambient contextvar is never used here: a request without
    the scope entry gets ``None`` (forbidden).
    """
    request = getattr(ctx.request_context, "request", None)
    scope = getattr(request, "scope", None) or {}
    return (scope.get("state") or {}).get(auth.SCOPE_KEY)


async def _call(ctx: Context, fn: Callable[..., dict], *args: Any) -> dict:
    token = _token_of(ctx)
    if token is None:
        return {"error": "forbidden"}

    def run() -> dict:
        reset = auth.current_token.set(token)
        try:
            return fn(*args)
        finally:
            auth.current_token.reset(reset)

    return await asyncio.to_thread(run)


def build_mcp(tools: Tools) -> MCPServer:
    server = MCPServer("treant", instructions=INSTRUCTIONS)

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    @server.tool(description=SAVE_DESCRIPTION)
    async def save_session(
        client_session_id: str, sections: dict[str, Any], ctx: Context, unsure: bool = False
    ) -> dict[str, Any]:
        return await _call(ctx, tools.save_session, client_session_id, sections, unsure)

    @server.tool()
    async def search(query: str, ctx: Context, limit: int = 8) -> dict[str, Any]:
        """Search memory (this workspace plus general know-how) by meaning and keywords."""
        return await _call(ctx, tools.search, query, limit)

    @server.tool()
    async def brief(
        ctx: Context, repo: str | None = None, repo_only: bool = False
    ) -> dict[str, Any]:
        """Short orientation for this workspace: warnings, open threads, recent decisions.
        With repo_only, only what memory holds about the named repo."""
        return await _call(ctx, tools.brief, repo, repo_only)

    @server.tool()
    async def guard_check(
        command: str,
        cwd: str,
        ctx: Context,
        kube_context: str | None = None,
        aws_profile: str | None = None,
        git_remotes: dict[str, str] | None = None,
        ansible_inventory: str | None = None,
    ) -> dict[str, Any]:
        """Deprecated: the client decides locally; use warnings.

        Check a risky shell command's targets against this workspace's listed targets.

        ``ansible_inventory`` is the inventory ``ansible-playbook`` uses without ``-i``
        (comma-separated, as in ``ANSIBLE_INVENTORY``). ``unresolved`` lists target kinds
        that could not be determined (``command`` when the line did not parse); an empty
        ``unlisted`` then does not mean all clear.
        """
        return await _call(
            ctx,
            tools.guard_check,
            command,
            cwd,
            kube_context,
            aws_profile,
            git_remotes,
            ansible_inventory,
        )

    @server.tool()
    async def warnings(names: list[str], ctx: Context) -> dict[str, Any]:
        """Current warning facts on workspace nodes named in `names` (lower-cased words)."""
        return await _call(ctx, tools.warnings, names)

    @server.tool()
    async def update_fact(
        fact_id: str, ctx: Context, text: str | None = None, status: str | None = None
    ) -> dict[str, Any]:
        """Correct a workspace fact's text, or set its status to current or closed."""
        return await _call(ctx, tools.update_fact, fact_id, text, status)

    @server.tool()
    async def retract(fact_id: str, ctx: Context) -> dict[str, Any]:
        """Retract a workspace fact that is wrong."""
        return await _call(ctx, tools.retract, fact_id)

    @server.tool()
    async def report_mistake(text: str, ctx: Context) -> dict[str, Any]:
        """Report that memory misled you (max 2000 characters); the owner reviews these."""
        return await _call(ctx, tools.report_mistake, text)

    return server


def build_app(db: Database, embedder: Embedder, allowed_hosts: tuple[str, ...] = ()) -> ASGIApp:
    server = build_mcp(Tools(db, embedder))
    # With allowed hosts set, DNS-rebinding protection checks Host (exact match) and, when
    # present, Origin; requests without an Origin (CLI MCP clients) pass. Empty = off, and the
    # bearer token is the only gate.
    if allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(allowed_hosts),
            allowed_origins=[f"https://{h}" for h in allowed_hosts],
        )
    else:
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = server.streamable_http_app(transport_security=security)
    return auth.BearerMiddleware(app, db)
