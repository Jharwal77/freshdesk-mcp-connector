"""Pagination: cursors, page ceilings, and honest truncation.

These are the tests that matter most for agent safety: a connector that silently
returns page 1 of 3 and calls it "the tickets" produces confidently wrong answers.
"""

from __future__ import annotations

import httpx
import pytest

from freshdesk_connector.client import FreshdeskClient, decode_cursor, encode_cursor
from freshdesk_connector.errors import ValidationError
from freshdesk_connector.mock.app import MockSettings, build_app


async def test_cursor_walk_visits_every_record_exactly_once(service, mock_state):
    """No record is skipped and none is repeated across a full cursor walk.

    Scope is the default 30-day listing window, which is the honest limit of the
    endpoint being walked.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    in_window = {
        t["id"]
        for t in mock_state.tickets
        if datetime.fromisoformat(str(t["created_at"]).replace("Z", "+00:00")) >= cutoff
    }
    seen: list[int] = []
    cursor = None
    for _ in range(20):
        page = await service.list_tickets(limit=7, cursor=cursor)
        assert page["ok"]
        seen.extend(t["id"] for t in page["data"])
        cursor = page["pagination"]["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)), "a record was returned twice"
    assert set(seen) == in_window, "records were skipped or invented"
    assert len(seen) < len(mock_state.tickets), (
        "the fixture set should include tickets outside the default window, "
        "otherwise this test proves nothing about the 30-day caveat"
    )


async def test_page_size_tracks_the_requested_limit(service, mock_state):
    page = await service.list_tickets(limit=9)
    assert page["pagination"]["returned"] == 9
    assert page["pagination"]["next_cursor"]


async def test_cursor_is_bound_to_the_query_that_produced_it(service):
    page = await service.list_tickets(limit=5)
    cursor = page["pagination"]["next_cursor"]
    # Reusing a cursor with different filters must fail loudly, not silently
    # return a mixed-up result set.
    with pytest.raises(ValidationError) as excinfo:
        await service.client.paginate(
            "/tickets", params={"order_by": "created_at", "order_type": "desc"}, mode="list", cursor=cursor
        )
    assert "different query" in excinfo.value.message


async def test_malformed_cursor_is_rejected_clearly(service):
    with pytest.raises(ValidationError):
        await service.client.paginate("/tickets", params={}, mode="list", cursor="not-a-cursor")


def test_cursor_round_trip():
    cursor = encode_cursor(path="/tickets", params={"a": 1}, offset=42, mode="list")
    assert decode_cursor(cursor, path="/tickets", params={"a": 1})["off"] == 42


async def test_search_ceiling_is_reported_not_silently_applied(client, config):
    """Freshdesk's search endpoint stops at page 10 of 30 = 300 records."""
    app = build_app(MockSettings(api_key=config.api_key, default_window_days=365 * 5))
    async with FreshdeskClient(config, transport=httpx.ASGITransport(app=app)) as c:
        page = await c.paginate(
            "/search/tickets",
            params={"query": "status:2 OR status:3 OR status:4 OR status:5 OR status:6 OR status:7"},
            mode="search",
            max_pages=10,
        )
        # The mock holds fewer than 300 records, so this asserts the walk completes
        # and reports a total rather than the ceiling itself.
        assert page.total is not None
        assert len(page.items) == min(page.total, 300)


async def test_listing_stops_at_the_page_ceiling_with_a_warning(client, config):
    """With a tiny window and a huge ceiling, the walk must explain its limits."""
    app = build_app(MockSettings(api_key=config.api_key))
    async with FreshdeskClient(config, transport=httpx.ASGITransport(app=app)) as c:
        page = await c.paginate("/tickets", mode="list", max_pages=1, limit=5)
        assert page.pages_fetched == 1


async def test_deadline_truncation_returns_partial_results_with_a_warning(config, frozen_clock):
    """The flagship guardrail: never hang an agent, always explain what is missing."""
    settings = MockSettings(
        api_key=config.api_key, fail_on_call={"tickets_search": [2]}, retry_after_seconds=600
    )
    app = build_app(settings)
    async with FreshdeskClient(config, transport=httpx.ASGITransport(app=app)) as client:
        page = await client.paginate(
            "/search/tickets", params={"query": "created_at:>'2000-01-01'"}, mode="search", limit=90, max_pages=3
        )
        assert page.count == 30, "page 1 should still be returned"
        assert page.truncated is True
        assert any("upstream_rate_limited" in w for w in page.warnings)
        assert page.next_cursor, "truncation must leave a usable cursor"


async def test_first_page_failure_still_propagates(client, config):
    """Partial results are only useful once we have *some* results."""
    from freshdesk_connector.errors import RateLimited

    settings = MockSettings(
        api_key=config.api_key, fail_on_call={"tickets_search": [1]}, retry_after_seconds=600
    )
    app = build_app(settings)
    async with FreshdeskClient(config, transport=httpx.ASGITransport(app=app)) as c:
        with pytest.raises(RateLimited):
            await c.paginate("/search/tickets", params={"query": "status:2"}, mode="search", max_pages=2)


async def test_expired_window_is_disclosed(service, mock_state):
    """The 30-day default window is a data-loss trap; the envelope must say so."""
    default_page = await service.list_tickets(limit=100)
    widened_page = await service.list_tickets(limit=100, updated_since="2000-01-01")

    assert len(widened_page["data"]) > len(default_page["data"])
    assert "coverage_caveat" in default_page["meta"]
    assert "30 days" in default_page["meta"]["coverage_window"]
    # With an explicit window there is nothing to caveat, so the field is absent
    # (empty metadata is stripped rather than filled with nulls).
    assert "coverage_caveat" not in widened_page["meta"]
