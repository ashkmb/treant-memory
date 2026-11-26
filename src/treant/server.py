"""ASGI app: the MCP streamable-HTTP endpoint behind the bearer middleware, plus /healthz."""

import asyncio
import importlib.metadata
from collections.abc import Callable
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
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


def _version() -> str:
    try:
        return importlib.metadata.version("treant")
    except importlib.metadata.PackageNotFoundError:
        return ""


def _hints(
    *, read_only: bool, destructive: bool = False, idempotent: bool = False
) -> ToolAnnotations:
    # Every tool works on this server's own memory only: no tool reaches the outside world.
    return ToolAnnotations(
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=False,
    )


SEARCH_DESCRIPTION = (
    "Search long-term memory by meaning and keywords: this workspace's facts, decisions, lessons, "
    "procedures and warnings, plus shared general know-how. Use it before the first action on a "
    "named repo, service, cluster or ticket, before proposing a design or a fix, after an error "
    "and before retrying, and whenever the user refers to past work. Read-only. Returns "
    "`mode` (hybrid, or keyword when the embedder is down) and `hits`, each with namespace, "
    "kind, type, text, node and score; facts reached through the graph from a direct hit are "
    "included."
)
BRIEF_DESCRIPTION = (
    "Short orientation to read at the start of a session: current warnings, open threads and "
    "recent decisions for this workspace, plus what memory holds about `repo` when given. "
    "With `repo_only`, only the repo section (no workspace-wide warnings or threads). Read-only; "
    "returns `brief` as plain text of at most 6000 characters. The brief is built from distilled "
    "facts, so it stays nearly empty until the worker has distilled some sessions."
)
WARNINGS_DESCRIPTION = (
    "Current warning facts attached to the named systems, for a check before a risky action "
    "(a deploy, a migration, a command against a cluster). Pass the names of the repos, "
    "services, hosts or clusters involved. Read-only; returns `warnings`, a list of texts, "
    "empty when memory holds none."
)
UPDATE_DESCRIPTION = (
    "Correct a fact in this workspace: replace its text, or set an open thread's status to "
    "closed (done) or back to current. Use it when you learn a fact is outdated or imprecise; "
    "use retract when it is simply wrong. The previous text is kept in the fact's history and "
    "the owner can restore it. Needs the token's update scope. Returns the fact's new `version`, "
    "or `error`: not found, invalid input, forbidden."
)
RETRACT_DESCRIPTION = (
    "Retract a fact in this workspace that is wrong, so search and the brief stop returning it. "
    "Prefer update_fact when the fact only needs correcting. The fact and its history are kept "
    "and the owner can restore it. Needs the token's delete scope. Returns `ok`, or `error`: not "
    "found, forbidden."
)
MISTAKE_DESCRIPTION = (
    "Report that memory misled you: a fact, warning or brief line that turned out wrong or "
    "harmful in this session. Describe what memory said and what was actually true. The report "
    "goes to the owner's review page and changes no fact by itself. Returns `ok`, or `error`."
)
GUARD_DESCRIPTION = (
    "Deprecated: the client decides locally now; use warnings. Checks a risky shell command's "
    "targets (kube context, AWS profile, git remotes, Terraform root, Ansible inventory) against "
    "this workspace's listed targets. Read-only. `unresolved` lists target kinds that could not "
    "be determined (`command` when the line did not parse); an empty `unlisted` then does not "
    "mean all clear."
)
REPO_FACTS_DESCRIPTION = (
    "Current facts of this workspace about the named repo and the parts of it (nodes linked to "
    "it by part_of), for checking them against the repo's code. Matches the repo's name or an "
    "alias, case-insensitively. Read-only. Returns `facts`, each with id, type, text and node, "
    "at most 200 and newest first, and `truncated` when more were left out."
)
FLAG_STALE_DESCRIPTION = (
    "Flag a current fact of this workspace whose code tokens (paths, names) are missing from the "
    "repo at a commit, for the owner to review. `missing` lists {token, commit, subject}: each "
    "token must appear verbatim in the fact text; commit and subject name the commit that "
    "removed it. Never changes the fact. Needs the token's update scope. Returns `flagged` "
    "with `flag_id`, or `flagged` false with `reason` (exists, kept), or `error`: not found, "
    "invalid input, forbidden."
)


def build_mcp(tools: Tools) -> MCPServer:
    server = MCPServer(
        "treant",
        title="Treant",
        version=_version(),
        website_url="https://github.com/ashkmb/treant-memory",
        instructions=INSTRUCTIONS,
    )

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    @server.tool(
        title="Save session",
        description=SAVE_DESCRIPTION,
        annotations=_hints(read_only=False),
    )
    async def save_session(
        client_session_id: Annotated[
            str,
            Field(
                description="Your own stable id for this session; saving it again adds a revision."
            ),
        ],
        sections: Annotated[
            dict[str, Any],
            Field(
                description="Typed sections: keys from the list above, values a string or a list of strings."
            ),
        ],
        ctx: Context,
        unsure: Annotated[
            bool,
            Field(
                description="True when you are not sure the text is free of secrets; the owner reviews it before anything is searchable."
            ),
        ] = False,
    ) -> dict[str, Any]:
        return await _call(ctx, tools.save_session, client_session_id, sections, unsure)

    @server.tool(
        title="Search memory",
        description=SEARCH_DESCRIPTION,
        annotations=_hints(read_only=True, idempotent=True),
    )
    async def search(
        query: Annotated[str, Field(description="What you want to know, in plain words or names.")],
        ctx: Context,
        limit: Annotated[int, Field(description="Number of direct hits to return.")] = 8,
    ) -> dict[str, Any]:
        return await _call(ctx, tools.search, query, limit)

    @server.tool(
        title="Session brief",
        description=BRIEF_DESCRIPTION,
        annotations=_hints(read_only=True, idempotent=True),
    )
    async def brief(
        ctx: Context,
        repo: Annotated[
            str | None,
            Field(description="Repository name (or owner/name) to add a section about."),
        ] = None,
        repo_only: Annotated[
            bool,
            Field(description="Only the repo section, with the whole length budget; needs repo."),
        ] = False,
    ) -> dict[str, Any]:
        return await _call(ctx, tools.brief, repo, repo_only)

    @server.tool(
        title="Check targets (deprecated)",
        description=GUARD_DESCRIPTION,
        annotations=_hints(read_only=True, idempotent=True),
    )
    async def guard_check(
        command: Annotated[str, Field(description="The full shell command line.")],
        cwd: Annotated[str, Field(description="Working directory the command runs in.")],
        ctx: Context,
        kube_context: Annotated[
            str | None, Field(description="Current kubectl context, if any.")
        ] = None,
        aws_profile: Annotated[
            str | None, Field(description="Current AWS profile, if any.")
        ] = None,
        git_remotes: Annotated[
            dict[str, str] | None, Field(description="Git remotes of cwd, name to URL.")
        ] = None,
        ansible_inventory: Annotated[
            str | None,
            Field(
                description="Inventory ansible-playbook uses without -i (comma-separated, as in ANSIBLE_INVENTORY)."
            ),
        ] = None,
    ) -> dict[str, Any]:
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

    @server.tool(
        title="Warnings for systems",
        description=WARNINGS_DESCRIPTION,
        annotations=_hints(read_only=True, idempotent=True),
    )
    async def warnings(
        names: Annotated[
            list[str],
            Field(
                description="Names of the systems involved (repos, services, hosts, clusters); matched case-insensitively, at most 200."
            ),
        ],
        ctx: Context,
    ) -> dict[str, Any]:
        return await _call(ctx, tools.warnings, names)

    @server.tool(
        title="Correct a fact",
        description=UPDATE_DESCRIPTION,
        annotations=_hints(read_only=False, destructive=True, idempotent=True),
    )
    async def update_fact(
        fact_id: Annotated[str, Field(description="The fact's id, as search returns it.")],
        ctx: Context,
        text: Annotated[
            str | None, Field(description="The corrected text, at most 20000 characters.")
        ] = None,
        status: Annotated[
            str | None,
            Field(description="current or closed; closed applies to open threads only."),
        ] = None,
    ) -> dict[str, Any]:
        return await _call(ctx, tools.update_fact, fact_id, text, status)

    @server.tool(
        title="Retract a fact",
        description=RETRACT_DESCRIPTION,
        annotations=_hints(read_only=False, destructive=True, idempotent=True),
    )
    async def retract(
        fact_id: Annotated[str, Field(description="The fact's id, as search returns it.")],
        ctx: Context,
    ) -> dict[str, Any]:
        return await _call(ctx, tools.retract, fact_id)

    @server.tool(
        title="Report a memory mistake",
        description=MISTAKE_DESCRIPTION,
        annotations=_hints(read_only=False),
    )
    async def report_mistake(
        text: Annotated[
            str,
            Field(
                description="What memory said and what was true instead, at most 2000 characters."
            ),
        ],
        ctx: Context,
    ) -> dict[str, Any]:
        return await _call(ctx, tools.report_mistake, text)

    @server.tool(
        title="Facts about a repo",
        description=REPO_FACTS_DESCRIPTION,
        annotations=_hints(read_only=True, idempotent=True),
    )
    async def repo_facts(
        repo: Annotated[
            str,
            Field(description="The repo's name as memory knows it, at most 200 characters."),
        ],
        ctx: Context,
    ) -> dict[str, Any]:
        return await _call(ctx, tools.repo_facts, repo)

    @server.tool(
        title="Flag a stale fact",
        description=FLAG_STALE_DESCRIPTION,
        annotations=_hints(read_only=False),
    )
    async def flag_stale(
        fact_id: Annotated[str, Field(description="The fact's id, as repo_facts returns it.")],
        repo: Annotated[
            str, Field(description="The repo the fact was checked against, as for repo_facts.")
        ],
        commit: Annotated[
            str, Field(description="The repo's HEAD commit id the check ran at (hex).")
        ],
        missing: Annotated[
            list[dict[str, Any]],
            Field(
                description="Missing tokens, at most 10 kept: {token, commit, subject}; token is 3-120 characters found verbatim in the fact text, commit and subject (one line) name the commit that removed it."
            ),
        ],
        ctx: Context,
    ) -> dict[str, Any]:
        return await _call(ctx, tools.flag_stale, fact_id, repo, commit, missing)

    return server


class _ListingOnly:
    """Stands in for Tools in listing mode. Never reached: without a bearer token on an HTTP
    request, every tool call is answered ``forbidden`` before a tool runs."""

    def __getattr__(self, name: str) -> Callable[..., dict]:
        def refuse(*args: Any) -> dict:
            raise RuntimeError("listing mode has no memory")

        return refuse


def build_listing_mcp() -> MCPServer:
    """The same tool definitions with no database, for directories that list a server's tools
    (``treant serve --list-tools``, stdio). Every tool call returns ``forbidden``."""
    return build_mcp(_ListingOnly())  # type: ignore[arg-type]


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
