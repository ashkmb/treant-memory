"""Tool definitions as clients and directories see them, and the database-free listing mode."""

import os
import sys

import pytest
from mcp import Client, StdioServerParameters

from treant.server import build_listing_mcp

TOOLS = {
    # name: (read_only, destructive)
    "save_session": (False, False),
    "search": (True, False),
    "brief": (True, False),
    "warnings": (True, False),
    "update_fact": (False, True),
    "retract": (False, True),
    "report_mistake": (False, False),
    "guard_check": (True, False),
    "repo_facts": (True, False),
    "flag_stale": (False, False),
}

MINIMAL_ARGS = {
    "save_session": {"client_session_id": "s1", "sections": {"journal": "x"}},
    "search": {"query": "x"},
    "brief": {},
    "warnings": {"names": ["x"]},
    "update_fact": {"fact_id": "00000000-0000-0000-0000-000000000000", "text": "x"},
    "retract": {"fact_id": "00000000-0000-0000-0000-000000000000"},
    "report_mistake": {"text": "x"},
    "guard_check": {"command": "ls", "cwd": "/tmp"},
    "repo_facts": {"repo": "demo"},
    "flag_stale": {
        "fact_id": "00000000-0000-0000-0000-000000000000",
        "repo": "demo",
        "commit": "abc1234",
        "missing": [{"token": "old_name.py"}],
    },
}


async def _tools(server):
    async with Client(server) as c:
        return {t.name: t for t in (await c.list_tools()).tools}


@pytest.mark.anyio
async def test_every_tool_is_documented_and_annotated():
    tools = await _tools(build_listing_mcp())
    assert set(tools) == set(TOOLS)
    for name, (read_only, destructive) in TOOLS.items():
        t = tools[name]
        assert t.title, name
        assert len(t.description or "") >= 120, name
        a = t.annotations
        assert a is not None, name
        assert a.read_only_hint is read_only, name
        assert a.destructive_hint is destructive, name
        assert a.open_world_hint is False, name
        for prop, schema in t.input_schema.get("properties", {}).items():
            assert schema.get("description"), f"{name}.{prop} has no description"


@pytest.mark.anyio
async def test_server_reports_name_and_version():
    async with Client(build_listing_mcp()) as c:
        info = c.server_info
        assert (info.name, info.title) == ("treant", "Treant")
        assert info.version


@pytest.mark.anyio
async def test_guard_check_is_marked_deprecated():
    tools = await _tools(build_listing_mcp())
    assert tools["guard_check"].description.startswith("Deprecated")


@pytest.mark.anyio
async def test_listing_mode_refuses_every_call():
    async with Client(build_listing_mcp()) as c:
        for name, args in MINIMAL_ARGS.items():
            res = await c.call_tool(name, args)
            assert res.structured_content == {"error": "forbidden"}, name


@pytest.mark.anyio
async def test_serve_list_tools_runs_over_stdio_without_any_settings():
    env = {k: v for k, v in os.environ.items() if not k.startswith("TREANT_")}
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-c",
            "import sys; from treant.cli import main; sys.exit(main())",
            "serve",
            "--list-tools",
        ],
        env=env,
    )
    async with Client(params) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert names == set(TOOLS)
        res = await c.call_tool("search", {"query": "x"})
        assert res.structured_content == {"error": "forbidden"}
