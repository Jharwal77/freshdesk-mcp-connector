"""The MCP surface: tool registration, annotations, dispatch and error shapes.

These run the server object in-process (no subprocess), which keeps the suite fast
while still exercising the real ``MCPServer`` dispatch path.
``test_mcp_protocol.py`` covers the same surface over a real stdio connection.
"""

from __future__ import annotations

import json

import httpx
import pytest

from freshdesk_connector.mock.app import MockSettings, build_app
from freshdesk_connector.server import ConnectorRuntime, create_server
from tests.conftest import make_config

READ_TOOLS = {
    "freshdesk_connector_health",
    "freshdesk_rate_limit_status",
    "freshdesk_list_tickets",
    "freshdesk_get_ticket",
    "freshdesk_search_tickets",
    "freshdesk_search_tickets_by_text",
    "freshdesk_list_ticket_conversations",
    "freshdesk_get_contact",
    "freshdesk_search_contacts",
    "freshdesk_get_company",
    "freshdesk_list_agents",
    "freshdesk_list_groups",
    "freshdesk_get_ticket_fields",
}


@pytest.fixture
def runtime(config) -> ConnectorRuntime:
    app = build_app(MockSettings(api_key=config.api_key))
    return ConnectorRuntime(config, transport=httpx.ASGITransport(app=app))


@pytest.fixture
def mcp(runtime):
    return create_server(runtime.config, runtime=runtime)


def _payload(result) -> dict:
    """MCP tool results arrive as content blocks; parse the first text block."""
    return json.loads(result.content[0].text)


async def test_read_only_tool_set_is_registered_by_default(mcp):
    tools = await mcp.list_tools()
    assert {t.name for t in tools} == READ_TOOLS


async def test_write_tool_appears_only_when_enabled():
    writable = make_config(enable_writes=True)
    app = build_app(MockSettings(api_key=writable.api_key))
    server = create_server(writable, runtime=ConnectorRuntime(writable, transport=httpx.ASGITransport(app=app)))
    names = {t.name for t in await server.list_tools()}
    assert names == READ_TOOLS | {"freshdesk_add_private_note"}


async def test_every_tool_advertises_annotations_and_a_real_description(mcp):
    for tool in await mcp.list_tools():
        assert tool.description and len(tool.description) > 60, tool.name
        assert tool.annotations is not None, tool.name
        assert tool.annotations.read_only_hint is (tool.name != "freshdesk_add_private_note")
        assert tool.annotations.open_world_hint is True


async def test_the_write_tool_is_declared_additive_and_idempotent():
    writable = make_config(enable_writes=True)
    server = create_server(writable, runtime=ConnectorRuntime(writable))
    tool = next(t for t in await server.list_tools() if t.name == "freshdesk_add_private_note")
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.idempotent_hint is True


async def test_input_schemas_are_generated_from_type_hints(mcp):
    tool = next(t for t in await mcp.list_tools() if t.name == "freshdesk_search_tickets")
    schema = tool.input_schema
    assert schema["type"] == "object"
    assert "status" in schema["properties"]
    assert schema["properties"]["limit"]["type"] == "integer"
    assert schema.get("required") in (None, []), "all filters are optional; the builder enforces at least one"


async def test_server_instructions_warn_about_the_three_silent_traps(mcp):
    """The instructions are the only place an agent learns these before it acts."""
    import re

    text = re.sub(r"\s+", " ", (mcp.instructions or "").replace("**", ""))
    assert "30 days" in text, "the default listing window must be disclosed"
    assert "no full-text search" in text, "the agent must not believe search is exhaustive"
    assert "per merchant account" in text, "the shared rate budget must be explained"
    assert "visibility=internal" in text, "the private-note guardrail must be stated"


async def test_tool_call_returns_a_structured_envelope(mcp):
    result = await mcp.call_tool("freshdesk_search_tickets", {"status": "open", "limit": 3})
    payload = _payload(result)
    assert payload["ok"] is True
    assert len(payload["data"]) <= 3
    assert payload["meta"]["generated_freshdesk_query"]


async def test_labels_accepted_over_the_wire_are_translated(mcp):
    result = await mcp.call_tool("freshdesk_search_tickets", {"status": ["open", "pending"], "limit": 2})
    payload = _payload(result)
    assert payload["ok"] and "(status:2 OR status:3)" in payload["meta"]["generated_freshdesk_query"]


async def test_failures_are_returned_as_data_not_raised(mcp):
    result = await mcp.call_tool("freshdesk_get_ticket", {"ticket_id": 999999})
    assert result.is_error is False, "connector errors must not surface as transport errors"
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["retryable"] is False
    assert payload["error"]["agent_hint"]


async def test_bad_arguments_produce_a_correctable_error(mcp):
    result = await mcp.call_tool("freshdesk_list_tickets", {"limit": 100000})
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_request"
    assert "cursor" in payload["error"]["agent_hint"]


async def test_unknown_tool_names_do_not_crash_the_server(mcp):
    with pytest.raises(Exception):
        await mcp.call_tool("freshdesk_delete_everything", {})


async def test_the_write_tool_does_not_exist_when_writes_are_disabled(mcp):
    """Fail closed by not registering it at all - an agent cannot call what is absent."""
    names = {t.name for t in await mcp.list_tools()}
    assert "freshdesk_add_private_note" not in names
    with pytest.raises(Exception, match="Unknown tool"):
        await mcp.call_tool("freshdesk_add_private_note", {"ticket_id": 10001, "body": "hi"})


async def test_the_write_tool_works_over_the_wire_when_explicitly_enabled():
    writable = make_config(enable_writes=True)
    app = build_app(MockSettings(api_key=writable.api_key))
    server = create_server(writable, runtime=ConnectorRuntime(writable, transport=httpx.ASGITransport(app=app)))

    first = _payload(
        await server.call_tool(
            "freshdesk_add_private_note",
            {"ticket_id": 10001, "body": "Triage: escalated.", "idempotency_key": "mcp-1"},
        )
    )
    assert first["ok"] is True
    assert first["data"]["private"] is True
    assert any("internal" in w.lower() for w in first["warnings"])

    repeat = _payload(
        await server.call_tool(
            "freshdesk_add_private_note",
            {"ticket_id": 10001, "body": "Triage: escalated.", "idempotency_key": "mcp-1"},
        )
    )
    assert repeat["data"]["deduplicated"] is True


async def test_conversations_expose_visibility_for_guardrails(mcp):
    result = await mcp.call_tool("freshdesk_list_ticket_conversations", {"ticket_id": 10001})
    payload = _payload(result)
    assert {c["visibility"] for c in payload["data"]} == {"internal", "public"}
    assert payload["warnings"]


async def test_every_tool_result_is_json_text(mcp):
    """Whatever the tool returns must be parseable JSON for any MCP client."""
    calls = {
        "freshdesk_connector_health": {},
        "freshdesk_rate_limit_status": {},
        "freshdesk_list_tickets": {"limit": 2},
        "freshdesk_get_ticket": {"ticket_id": 10001},
        "freshdesk_list_groups": {},
        "freshdesk_list_agents": {},
        "freshdesk_get_ticket_fields": {},
        "freshdesk_get_contact": {"contact_id": 3001},
        "freshdesk_get_company": {"company_id": 5001},
    }
    for name, args in calls.items():
        payload = _payload(await mcp.call_tool(name, args))
        assert "ok" in payload, name
