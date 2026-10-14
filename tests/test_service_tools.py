"""Service-level behaviour: the guarantees an agent actually depends on."""

from __future__ import annotations

import httpx
import pytest

from freshdesk_connector.client import FreshdeskClient
from freshdesk_connector.mock.app import MockSettings, build_app
from freshdesk_connector.service import MAX_RECORDS_PER_CALL, FreshdeskService
from tests.conftest import make_config


# ------------------------------------------------------------------- lookups
async def test_get_ticket_includes_timeline_and_flags_internal_notes(service):
    result = await service.get_ticket(10001, include_conversations=True)
    assert result["ok"]
    ticket = result["data"]["ticket"]
    assert ticket["id"] == 10001
    assert ticket["url"].endswith("/a/tickets/10001")
    assert "timeline" in ticket
    conversations = result["data"]["conversations"]
    assert {c["visibility"] for c in conversations} == {"internal", "public"}
    assert any("internal notes" in w for w in result["warnings"])


async def test_list_conversations_can_exclude_private_notes(service):
    result = await service.list_ticket_conversations(10001, include_private=False)
    assert result["ok"]
    assert all(c["customer_visible"] for c in result["data"])
    assert "internal" not in result.get("warnings", [""])[0].lower()


async def test_get_contact_by_email_and_by_phone(service):
    by_email = await service.get_contact(email="vikram.rao@example.com")
    assert by_email["ok"] and by_email["data"]["name"] == "Vikram Rao"
    assert by_email["meta"]["matched_by"] == "email"

    by_phone = await service.get_contact(phone="+91 90000 77777")
    assert by_phone["ok"] and by_phone["data"]["name"] == "Divya Menon"


async def test_get_contact_missing_email_returns_a_recovery_hint(service):
    result = await service.get_contact(email="nobody@example.com")
    assert result["ok"] is False
    assert result["error"]["code"] == "not_found"
    assert "requester" in result["error"]["agent_hint"]


async def test_get_contact_requires_exactly_one_identifier(service):
    result = await service.get_contact()
    assert result["ok"] is False and result["error"]["code"] == "invalid_request"


async def test_company_view_can_include_contacts(service):
    result = await service.get_company(5001, include_contacts=True)
    assert result["ok"]
    assert result["data"]["company"]["name"] == "Vayu Wellness Pvt Ltd"
    assert result["data"]["contacts"]


async def test_unfiltered_contact_search_is_refused(service):
    result = await service.search_contacts()
    assert result["ok"] is False
    assert "at least one" in result["error"]["message"]


# ----------------------------------------------------------------- discovery
async def test_ticket_fields_expose_account_specific_names(service):
    result = await service.get_ticket_fields()
    names = result["data"]["filter_field_names"]
    assert "status" in names and "cf_autopay_status" in names
    assert all(not n.startswith("cf_cf_") for n in names)


async def test_groups_and_agents_are_human_readable(service):
    groups = await service.list_groups()
    agents = await service.list_agents()
    assert {g["name"] for g in groups["data"]} >= {"Payments Support", "Escalations"}
    assert any(a["name"] == "Priya Nair" for a in agents["data"])


# -------------------------------------------------------------------- search
async def test_search_returns_the_generated_query_for_auditability(service):
    result = await service.search_tickets(status="open", group_id=9001, limit=5)
    assert result["ok"]
    assert result["meta"]["generated_freshdesk_query"] == "status:2 AND group_id:9001"
    assert result["meta"]["endpoint"] == "/api/v2/search/tickets"


async def test_search_warns_only_when_the_300_ceiling_actually_bites(service):
    small = await service.search_tickets(status="open", limit=5)
    assert not any("300" in w for w in small.get("warnings", []))


async def test_search_by_custom_field(service):
    result = await service.search_tickets(custom_fields={"cf_autopay_status": "failed"}, limit=10)
    assert result["ok"] and result["data"]
    assert all(t["custom_fields"].get("autopay_status") == "failed" for t in result["data"])


async def test_unknown_custom_field_is_rejected_by_freshdesk_with_details(service):
    result = await service.search_tickets(custom_fields={"cf_not_in_this_account": "x"}, limit=1)
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_request"
    assert "ticket_fields" in result["error"]["agent_hint"]


# ----------------------------------------------------------------- scan honesty
async def test_text_scan_reports_coverage_and_never_claims_completeness(service):
    result = await service.search_tickets_by_text("autopay", max_pages=1)
    assert result["ok"]
    pagination = result["pagination"]
    assert pagination["tickets_examined"] > 0
    assert "scan_complete" in pagination
    assert any("no full-text search" in w for w in result["warnings"])


async def test_text_scan_can_read_descriptions_when_asked(service):
    subjects_only = await service.search_tickets_by_text("reconciliation", max_pages=2)
    with_bodies = await service.search_tickets_by_text("reconciliation", include_descriptions=True, max_pages=2)
    assert len(with_bodies["data"]) >= len(subjects_only["data"])


async def test_text_scan_requires_a_meaningful_needle(service):
    result = await service.search_tickets_by_text("ab")
    assert result["ok"] is False and result["error"]["code"] == "invalid_request"


# ------------------------------------------------------------------- guardrails
async def test_limit_is_clamped_with_an_explanatory_error(service):
    result = await service.list_tickets(limit=MAX_RECORDS_PER_CALL + 1)
    assert result["ok"] is False
    assert str(MAX_RECORDS_PER_CALL) in result["error"]["message"]
    assert "cursor" in result["error"]["agent_hint"]


async def test_writes_are_disabled_by_default(service):
    result = await service.add_private_note(10001, "hello")
    assert result["ok"] is False
    assert result["error"]["code"] == "writes_disabled"
    assert result["error"]["retryable"] is False


async def test_enabled_writes_post_a_private_note_and_deduplicate(config, mock_state):
    writable = make_config(enable_writes=True)
    app = build_app(MockSettings(api_key=config.api_key))
    async with FreshdeskClient(writable, transport=httpx.ASGITransport(app=app)) as client:
        service = FreshdeskService(client, writable)

        first = await service.add_private_note(10001, "Agent Studio triage: escalated to risk.", idempotency_key="k1")
        assert first["ok"] and first["data"]["private"] is True
        assert first["data"]["deduplicated"] is False

        # A retry after a timeout must not double-post.
        second = await service.add_private_note(10001, "Agent Studio triage: escalated to risk.", idempotency_key="k1")
        assert second["ok"] and second["data"]["deduplicated"] is True
        assert second["data"]["note_id"] == first["data"]["note_id"]

        notes = [n for n in app.state.mock.conversations[10001] if n["private"]]
        assert sum(1 for n in notes if "Agent Studio triage" in n["body_text"]) == 1


async def test_write_validation_rejects_empty_notes(config):
    writable = make_config(enable_writes=True)
    app = build_app(MockSettings(api_key=config.api_key))
    async with FreshdeskClient(writable, transport=httpx.ASGITransport(app=app)) as client:
        service = FreshdeskService(client, writable)
        result = await service.add_private_note(10001, "   ")
        assert result["ok"] is False and result["error"]["code"] == "invalid_request"


# ------------------------------------------------------------------------- ops
async def test_health_reports_auth_state_without_leaking_the_key(service, config):
    result = await service.connector_health()
    assert result["ok"] and result["data"]["authenticated"] is True
    assert result["data"]["writes_enabled"] is False
    assert config.api_key not in str(result)


async def test_health_reports_failure_instead_of_raising(config):
    app = build_app(MockSettings(api_key="wrong"))
    async with FreshdeskClient(config, transport=httpx.ASGITransport(app=app)) as client:
        service = FreshdeskService(client, config)
        result = await service.connector_health()
        assert result["ok"] and result["data"]["authenticated"] is False
        assert result["data"]["error"]["code"] == "auth_failed"


async def test_rate_limit_status_costs_no_api_calls(service, mock_state):
    before = mock_state.window_snapshot()["calls_this_window"]
    result = await service.rate_limit_status()
    after = mock_state.window_snapshot()["calls_this_window"]
    assert result["ok"] and before == after


@pytest.mark.parametrize("workflow_arg", ["teams", "queue"])
async def test_every_result_is_json_serialisable(service, workflow_arg):
    """Agents receive text; nothing may return a datetime or a raw object."""
    import json

    for result in (
        await service.list_tickets(limit=3),
        await service.get_ticket(10001),
        await service.search_tickets(status="open", limit=3),
        await service.get_ticket_fields(),
        await service.list_ticket_conversations(10001),
        await service.get_contact(contact_id=3001),
    ):
        json.dumps(result)  # raises if anything is not serialisable
