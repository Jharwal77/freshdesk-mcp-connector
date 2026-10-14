"""Label translation, projections, and the constrained search-query builder."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from freshdesk_connector.errors import ValidationError
from freshdesk_connector.models import (
    SEARCH_QUERY_MAX_CHARS,
    build_ticket_query,
    compact_conversation,
    compact_ticket,
    priority_label,
    resolve_priority,
    resolve_status,
    status_label,
    ticket_field_summary,
)
from freshdesk_connector.mock.query import parse


# --------------------------------------------------------------------- enums
def test_status_and_priority_labels_are_symmetric():
    assert resolve_status("open") == 2
    assert resolve_status("Waiting on Customer") == 6
    assert status_label(2) == "open"
    assert resolve_priority("urgent") == 4
    assert resolve_priority(3) == 3
    assert priority_label(4) == "urgent"


def test_unknown_labels_fail_loudly_with_the_allowed_values():
    with pytest.raises(ValidationError) as excinfo:
        resolve_status("escalated")
    assert "allowed" in excinfo.value.details
    assert "open" in excinfo.value.details["allowed"]


# ---------------------------------------------------------------- projections
def test_compact_ticket_is_token_lean_and_translated():
    raw = {
        "id": 1,
        "subject": "x" * 400,
        "status": 2,
        "priority": 4,
        "source": 1,
        "requester_id": 5,
        "responder_id": None,
        "tags": ["a"],
        "description": "<div>huge html body</div>" * 50,
        "description_text": "plain body " * 200,
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-10-02T00:00:00Z",
        "due_by": "2026-10-03T00:00:00Z",
        "fr_escalated": True,
        "cf_order_id": "order_1",
        "cf_empty": None,
        "stats": {"resolved_at": "2026-10-04T00:00:00Z"},
    }
    projected = compact_ticket(raw, portal_url="https://acme.freshdesk.com", include_description=False, now=datetime(2026, 10, 2, tzinfo=timezone.utc))
    assert projected["status"] == "open" and projected["status_id"] == 2
    assert projected["priority"] == "urgent"
    assert len(projected["subject"]) <= 200
    assert "description_excerpt" not in projected
    assert projected["custom_fields"] == {"order_id": "order_1"}  # empty custom fields dropped
    assert projected["sla_breached"] is True  # resolved after the due date
    assert projected["url"].endswith("/a/tickets/1")


def test_compact_ticket_never_leaks_credential_keys():
    projected = compact_ticket(
        {"id": 1, "subject": "s", "api_key": "secret", "password": "hunter2"},
        portal_url="https://acme.freshdesk.com",
    )
    assert "api_key" not in projected and "password" not in projected


def test_compact_conversation_labels_private_notes():
    public = compact_conversation({"id": 1, "private": False, "body_text": "hello", "incoming": True})
    internal = compact_conversation({"id": 2, "private": True, "body_text": "internal", "incoming": False})
    assert public["visibility"] == "public" and public["customer_visible"] is True
    assert public["direction"] == "inbound"
    assert internal["visibility"] == "internal" and internal["customer_visible"] is False
    assert internal["direction"] == "outbound"


def test_ticket_field_summary_does_not_double_the_cf_prefix():
    standard = ticket_field_summary({"name": "status", "label": "Status", "type": "x", "default": True})
    custom = ticket_field_summary({"name": "cf_order_id", "label": "Order", "type": "x", "default": False})
    unprefixed = ticket_field_summary({"name": "sector", "label": "Sector", "type": "x", "default": False})
    assert standard["searchable_expression"] == "status"
    assert custom["searchable_expression"] == "cf_order_id"
    assert unprefixed["searchable_expression"] == "cf_sector"


# ---------------------------------------------------------------- query builder
def test_builder_translates_labels_and_groups_or_conditions():
    query = build_ticket_query(status=["open", "pending"], priority="urgent")
    assert query == "(status:2 OR status:3) AND priority:4"


def test_priority_at_least_is_an_explicit_or_list_not_a_comparison():
    """`priority:>N` is ambiguous in Freshworks' docs; an OR list cannot be."""
    query = build_ticket_query(priority_at_least="high")
    assert query == "(priority:3 OR priority:4)"
    assert "priority:>" not in query


def test_builder_handles_dates_nulls_and_custom_fields():
    query = build_ticket_query(
        created_after="2026-09-01",
        due_before="2026-10-15T09:00:00Z",
        include_unassigned_agent=True,
        custom_fields={"cf_order_id": "order_123", "cf_retry_count": 3},
    )
    assert "created_at:>'2026-09-01'" in query
    assert "due_by:<'2026-10-15T09:00:00Z'" in query
    assert "agent_id:null" in query
    assert "cf_order_id:'order_123'" in query
    assert "cf_retry_count:3" in query
    # And the mock's Freshdesk-compatible parser must accept what we generate.
    parse(query, allowed_custom_fields={"cf_order_id", "cf_retry_count"})


def test_an_empty_filter_set_is_refused_rather_than_scanning_everything():
    with pytest.raises(ValidationError) as excinfo:
        build_ticket_query()
    assert "unbounded" in excinfo.value.message.lower()
    assert "freshdesk_list_tickets" in excinfo.value.details["hint"]


def test_the_builder_signature_is_itself_the_field_whitelist():
    """There is no way to smuggle an arbitrary field name in as a structured filter.

    Unknown fields therefore cannot be *guessed* by the model: the only places a
    raw field name can appear are `custom_fields` (validated by the account's real
    field list at query time) and `extra_expression` (an explicit escape hatch).
    """
    import inspect

    params = set(inspect.signature(build_ticket_query).parameters)
    assert {"status", "priority", "group_id", "custom_fields"} <= params
    assert not {"arbitrary", "field", "where"} & params

    with pytest.raises(ValidationError):
        build_ticket_query(created_after="last tuesday")


def test_custom_field_names_are_prefixed_exactly_once():
    query = build_ticket_query(custom_fields={"order_id": "order_9", "cf_plan": "annual"})
    assert "cf_order_id:'order_9'" in query
    assert "cf_plan:'annual'" in query
    assert "cf_cf_plan" not in query


def test_oversized_queries_are_refused_before_spending_a_call():
    with pytest.raises(ValidationError) as excinfo:
        build_ticket_query(tag=[f"tag-{i}" for i in range(60)])
    assert str(SEARCH_QUERY_MAX_CHARS) in excinfo.value.message


def test_values_containing_quotes_are_rejected_not_silently_mangled():
    """Freshdesk's mini-language has no escape sequence, so refuse rather than
    emit a query that silently matches the wrong thing."""
    with pytest.raises(ValidationError) as excinfo:
        build_ticket_query(tag="o'brien")
    assert "quote" in excinfo.value.message.lower()
    with pytest.raises(ValidationError):
        build_ticket_query(custom_fields={"cf_note": 'say "hi"'})
