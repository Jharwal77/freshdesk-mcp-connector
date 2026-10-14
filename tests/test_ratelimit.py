"""Rate-limit governor: pacing, caps, learning and reporting.

The governor is clock- and sleeper-injectable, so these tests run instantly and
assert on exact waits instead of wall-clock timing.
"""

from __future__ import annotations

import pytest

from freshdesk_connector.errors import LocalBudgetExceeded
from freshdesk_connector.ratelimit import (
    BUCKET_CONTACTS_LIST,
    BUCKET_DEFAULT,
    BUCKET_TICKETS_LIST,
    BUCKET_TICKETS_SEARCH,
    PLAN_PROFILES,
    RateLimitGovernor,
    RateLimitHeaders,
    classify_bucket,
)


def test_bucket_classification_matches_freshdesk_endpoints():
    assert classify_bucket("GET", "/tickets") == BUCKET_TICKETS_LIST
    assert classify_bucket("GET", "/search/tickets") == BUCKET_TICKETS_SEARCH
    assert classify_bucket("GET", "/contacts") == BUCKET_CONTACTS_LIST
    assert classify_bucket("GET", "/tickets/1") == BUCKET_DEFAULT
    assert classify_bucket("POST", "/tickets/1/notes") == BUCKET_DEFAULT


async def test_calls_within_the_budget_do_not_sleep(governor, frozen_clock):
    for _ in range(5):
        await governor.acquire(BUCKET_DEFAULT)
    assert frozen_clock.slept == []


async def test_the_account_budget_is_enforced_across_buckets(governor, frozen_clock):
    """The ceiling is account-wide: mixing endpoints must not bypass it."""
    cap = governor.status()["effective_account_cap_per_minute"]
    for _ in range(cap):
        await governor.acquire(BUCKET_DEFAULT)
    assert frozen_clock.slept == [], "the account budget alone should not throttle us yet"
    await governor.acquire(BUCKET_CONTACTS_LIST)
    assert frozen_clock.slept == [pytest.approx(60.0)]


async def test_per_endpoint_sub_limit_is_tighter_than_the_account_limit(governor, frozen_clock):
    """Growth allows 100 calls/min overall but only 40 ticket-list calls."""
    status = governor.status()
    assert status["buckets"][BUCKET_TICKETS_LIST]["effective_cap_per_minute"] < status[
        "effective_account_cap_per_minute"
    ]


async def test_a_bucket_429_halves_that_buckets_cap(governor):
    before = governor._bucket_cap(BUCKET_TICKETS_LIST)
    governor.note_429(BUCKET_TICKETS_LIST, retry_after=0)
    after = governor._bucket_cap(BUCKET_TICKETS_LIST)
    assert after < before
    assert governor.learned_bucket_caps[BUCKET_TICKETS_LIST] == after


async def test_successes_gradually_restore_a_penalised_bucket(governor):
    governor.note_429(BUCKET_TICKETS_LIST, retry_after=0)
    penalised = governor._bucket_cap(BUCKET_TICKETS_LIST)
    for _ in range(6):
        governor.observe(BUCKET_TICKETS_LIST, RateLimitHeaders(total=100, remaining=90), 200)
    assert governor._bucket_cap(BUCKET_TICKETS_LIST) > penalised


async def test_429_blocks_globally_as_well_as_per_endpoint(governor, frozen_clock):
    """Ignoring Retry-After can get an API key or IP blocked, so back off widely."""
    governor.note_429(BUCKET_TICKETS_LIST, retry_after=30)
    await governor.acquire(BUCKET_CONTACTS_LIST)
    assert frozen_clock.total_slept >= 30


async def test_the_retry_after_header_is_honoured_exactly(governor, frozen_clock):
    wait = governor.note_429(BUCKET_TICKETS_LIST, retry_after=17)
    assert wait == pytest.approx(17.0)  # jitter is injected as 0.0 in tests


async def test_missing_retry_after_falls_back_to_a_default_wait(governor):
    assert governor.note_429(BUCKET_TICKETS_LIST, retry_after=None) == pytest.approx(20.0)


async def test_a_wait_longer_than_the_deadline_raises_instead_of_blocking(governor, frozen_clock):
    governor.note_429(BUCKET_TICKETS_LIST, retry_after=120)
    with pytest.raises(LocalBudgetExceeded) as excinfo:
        await governor.acquire(BUCKET_TICKETS_LIST, deadline=frozen_clock() + 5)
    assert excinfo.value.details["required_wait_seconds"] >= 100
    assert excinfo.value.retryable is True


async def test_low_remaining_headers_spread_out_further_calls(governor, frozen_clock):
    governor.observe(BUCKET_DEFAULT, RateLimitHeaders(total=100, remaining=1), 200)
    await governor.acquire(BUCKET_DEFAULT)
    await governor.acquire(BUCKET_DEFAULT)
    assert frozen_clock.total_slept > 0, "a nearly exhausted budget should slow the connector down"


async def test_tiny_budgets_stay_usable():
    """A 1/min budget must not be throttled to zero by an over-large reserve."""
    governor = RateLimitGovernor("trial")
    assert governor._overall_cap() >= 1


async def test_auto_mode_trusts_the_accounts_reported_ceiling():
    governor = RateLimitGovernor("auto")
    assert governor.status()["plan_profile"] == "growth"  # conservative start
    governor.observe(BUCKET_DEFAULT, RateLimitHeaders(total=700, remaining=690), 200)
    status = governor.status()
    assert status["observed_account_limit_per_minute"] == 700
    assert status["effective_account_cap_per_minute"] > PLAN_PROFILES["growth"].overall_per_minute * 0.5


async def test_an_explicit_plan_never_exceeds_the_reported_ceiling():
    governor = RateLimitGovernor("enterprise")
    governor.observe(BUCKET_DEFAULT, RateLimitHeaders(total=100, remaining=99), 200)
    assert governor._overall_cap() <= 100


async def test_status_is_serialisable_and_explains_shared_budgets(governor):
    import json

    governor.note_429(BUCKET_TICKETS_LIST, retry_after=5)
    payload = json.loads(json.dumps(governor.status()))
    assert payload["recent_events"][0]["event"] == "rate_limited"
    assert "shared" in payload["sharing_note"].lower()
    assert "per account" in payload["sharing_note"].lower()
    assert set(payload["buckets"]) == {
        BUCKET_TICKETS_LIST,
        BUCKET_TICKETS_SEARCH,
        BUCKET_CONTACTS_LIST,
        BUCKET_DEFAULT,
    }
