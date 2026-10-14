"""End-to-end test over a real MCP stdio connection.

This is the test that answers "does it actually work?":

1. a mock Freshdesk API is served over real TCP by uvicorn,
2. the MCP server is spawned as a **separate process** exactly as an MCP client
   (Claude Desktop, Cursor, Agent Studio) would spawn it,
3. the test acts as a real MCP client over stdin/stdout: initialise, list tools,
   call tools, and assert on what comes back.

Nothing is stubbed, so this also proves the packaging, environment handling and
JSON framing are right - the things that usually break on someone else's machine.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER_ARGV = ["-m", "freshdesk_connector.server"]


@pytest.fixture
def server_env(live_mock_server) -> dict[str, str]:
    """Environment for the spawned MCP server, pointed at the live mock."""
    return {
        "FRESHDESK_DOMAIN": "acme",
        "FRESHDESK_API_KEY": "test-api-key",
        "FRESHDESK_BASE_URL": live_mock_server,
        "FRESHDESK_PLAN": "growth",
        "FRESHDESK_ENABLE_WRITES": "false",
        "PYTHONPATH": os.pathsep.join(filter(None, [os.environ.get("PYTHONPATH", ""), "src"])),
        "PYTHONUNBUFFERED": "1",
    }


async def test_full_stdio_round_trip(server_env):
    params = StdioServerParameters(command=sys.executable, args=SERVER_ARGV, env=server_env)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.server_info.name == "freshdesk-connector"
            assert "30 days" in (init.instructions or "")

            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert "freshdesk_search_tickets" in names
            assert "freshdesk_add_private_note" not in names, "writes must be off by default"

            health = await session.call_tool("freshdesk_connector_health", {})
            payload = json.loads(health.content[0].text)
            assert payload["ok"] is True and payload["data"]["authenticated"] is True

            found = await session.call_tool(
                "freshdesk_search_tickets_by_text", {"text": "autopay", "max_pages": 2}
            )
            scan = json.loads(found.content[0].text)
            assert scan["ok"] is True
            assert scan["pagination"]["tickets_examined"] > 0
            assert any("no full-text search" in w for w in scan["warnings"])

            missing = await session.call_tool("freshdesk_get_ticket", {"ticket_id": 424242})
            error = json.loads(missing.content[0].text)
            assert error["ok"] is False and error["error"]["code"] == "not_found"

            rate = await session.call_tool("freshdesk_rate_limit_status", {})
            budget = json.loads(rate.content[0].text)
            assert budget["ok"] is True
            assert "tickets_list" in budget["data"]["buckets"]


async def test_server_process_stays_healthy_across_many_calls(server_env):
    """A long-lived session must not leak the HTTP client or degrade."""
    params = StdioServerParameters(command=sys.executable, args=SERVER_ARGV, env=server_env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for _ in range(12):
                result = await session.call_tool("freshdesk_list_tickets", {"limit": 3})
                payload = json.loads(result.content[0].text)
                assert payload["ok"] is True
                assert len(payload["data"]) == 3
