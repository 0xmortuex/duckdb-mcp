"""MCP stdio smoke test: launches the real installed server as a subprocess, lists its
tools and calls them over the protocol, and checks that instructive errors reach the agent
(MCPServer hides any exception that is not a ToolError)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

EXPECTED_TOOLS = {
    "list_files",
    "describe_file",
    "profile_file",
    "query",
    "sample_file",
    "explain_query",
}


def params(world: Path, *extra: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "duckdb_mcp", "--dir", str(world / "data"), "--max-rows", "25", *extra],
        cwd=str(world / "outside"),
    )


@pytest.mark.anyio
async def test_tools_and_a_session_over_stdio(world: Path) -> None:
    async with stdio_client(params(world)) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        assert init.server_info.name == "duckdb"
        tools = (await session.list_tools()).tools
        assert {t.name for t in tools} == EXPECTED_TOOLS
        assert all(t.description and len(t.description) > 80 for t in tools)

        res = await session.call_tool("list_files", {})
        assert not res.is_error and "people.csv" in res.content[0].text  # type: ignore[union-attr]

        res = await session.call_tool("describe_file", {"path": "people.csv"})
        text = res.content[0].text  # type: ignore[union-attr]
        assert "Rows: 4" in text and "| name | VARCHAR |" in text

        res = await session.call_tool(
            "query", {"sql": "SELECT city FROM 'people.csv' WHERE age > 40"}
        )
        assert "New York" in res.content[0].text  # type: ignore[union-attr]

        res = await session.call_tool("query", {"sql": "SELECT * FROM range(1000)", "format": "json"})
        data = json.loads(res.content[0].text)  # type: ignore[union-attr]
        assert data["truncated"] and data["row_count"] == 25

        res = await session.call_tool("profile_file", {"path": "people.csv"})
        assert "nulls" in res.content[0].text  # type: ignore[union-attr]
        res = await session.call_tool("sample_file", {"path": "people.parquet", "rows": 2})
        assert "2 rows" in res.content[0].text  # type: ignore[union-attr]
        res = await session.call_tool("explain_query", {"sql": "SELECT * FROM 'people.csv'"})
        assert "CSV" in res.content[0].text.upper()  # type: ignore[union-attr]


@pytest.mark.anyio
async def test_instructive_errors_reach_the_agent(world: Path) -> None:
    secret = (world / "outside" / "secret.csv").as_posix()
    async with stdio_client(params(world)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        res = await session.call_tool("query", {"sql": "DROP TABLE people"})
        assert res.is_error and "read-only" in res.content[0].text  # type: ignore[union-attr]

        res = await session.call_tool("query", {"sql": f"SELECT * FROM read_csv('{secret}')"})
        assert res.is_error and "allowed folders" in res.content[0].text  # type: ignore[union-attr]
        assert "top" not in res.content[0].text  # type: ignore[union-attr]

        res = await session.call_tool("query", {"sql": "SELECT * FROM '../outside/secret.csv'"})
        assert res.is_error

        res = await session.call_tool("describe_file", {"path": "missing.csv"})
        assert res.is_error and "list_files" in res.content[0].text  # type: ignore[union-attr]

        res = await session.call_tool("query", {"sql": "SELEC 1"})
        assert res.is_error and "syntax" in res.content[0].text  # type: ignore[union-attr]

        res = await session.call_tool("sample_file", {"path": "people.csv", "rows": 0})
        assert res.is_error and "between 1 and 100" in res.content[0].text  # type: ignore[union-attr]


@pytest.mark.anyio
async def test_default_dir_is_the_working_directory(world: Path) -> None:
    p = StdioServerParameters(command=sys.executable, args=["-m", "duckdb_mcp"], cwd=str(world / "outside"))
    async with stdio_client(p) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        res = await session.call_tool("list_files", {})
        text = res.content[0].text  # type: ignore[union-attr]
        assert "secret.csv" in text and "people.csv" not in text
