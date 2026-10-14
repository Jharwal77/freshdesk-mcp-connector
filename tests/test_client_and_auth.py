"""Authentication, error taxonomy, retries and audit behaviour."""

from __future__ import annotations

import json

import httpx
import pytest

from freshdesk_connector.client import FreshdeskClient
from freshdesk_connector.errors import (
    AuthError,
    NetworkError,
    NotFoundError,
    RateLimited,
    UpstreamError,
    ValidationError,
)
from freshdesk_connector.mock.app import MockSettings, build_app
from tests.conftest import make_config


async def test_successful_get_returns_json(client):
    payload = await client.get("/ticket_fields")
    assert isinstance(payload, list) and payload
    assert client.stats["calls"] == 1


async def test_bad_credentials_raise_auth_error_and_are_not_retried(config):
    app = build_app(MockSettings(api_key="different-key"))
    async with FreshdeskClient(config, transport=httpx.ASGITransport(app=app)) as client:
        with pytest.raises(AuthError) as excinfo:
            await client.get("/tickets")
        assert excinfo.value.http_status == 401
        assert excinfo.value.retryable is False
        # A 401 is never retried: retrying burns budget and cannot succeed.
        assert client.stats["retries"] == 0


async def test_missing_ticket_raises_not_found(client):
    with pytest.raises(NotFoundError) as excinfo:
        await client.get("/tickets/999999")
    assert excinfo.value.http_status == 404
    assert excinfo.value.retryable is False
    # The guidance the agent needs is in the hint, not the raw HTTP message.
    assert "does not exist" in excinfo.value.agent_hint.lower()
    assert "search" in excinfo.value.agent_hint.lower()


async def test_validation_error_carries_freshdesk_field_details(client):
    with pytest.raises(ValidationError) as excinfo:
        await client.get("/search/tickets", params={"query": "nonsense_field:1"})
    assert excinfo.value.http_status == 400
    details = excinfo.value.details
    assert details["freshdesk_errors"][0]["field"] == "nonsense_field"
    assert "ticket_fields" in excinfo.value.agent_hint


async def test_429_is_retried_and_honours_retry_after(config):
    settings = MockSettings(api_key=config.api_key, fail_on_call={"default": [1]}, retry_after_seconds=1)
    app = build_app(settings)
    async with FreshdeskClient(config, transport=httpx.ASGITransport(app=app)) as client:
        payload = await client.get("/ticket_fields")
        assert payload  # recovered
        assert client.stats["throttled"] == 1
        assert client.stats["retries"] == 1
        events = [e for e in client.governor.events if e["event"] == "rate_limited"]
        assert events and events[0]["retry_after_header"] == 1.0


async def test_429_gives_up_when_it_cannot_wait_within_the_deadline(config):
    settings = MockSettings(
        api_key=config.api_key, fail_on_call={"default": [1, 2, 3, 4]}, retry_after_seconds=600
    )
    app = build_app(settings)
    impatient = make_config(request_deadline=5.0)
    async with FreshdeskClient(impatient, transport=httpx.ASGITransport(app=app)) as client:
        with pytest.raises(RateLimited):
            await client.get("/ticket_fields")


async def test_5xx_is_retried_then_succeeds(config):
    """A single injected 500 must not surface to the caller."""
    from unittest.mock import patch

    app = build_app(MockSettings(api_key=config.api_key))
    calls = {"n": 0}
    real_request = httpx.AsyncClient.request

    async def flaky_request(self, *args, **kwargs):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, json={"description": "Internal server error"})
        return await real_request(self, *args, **kwargs)

    with patch.object(httpx.AsyncClient, "request", flaky_request):
        async with FreshdeskClient(config, transport=httpx.ASGITransport(app=app)) as client:
            payload = await client.get("/ticket_fields")
            assert payload
            assert client.stats["retries"] == 1


async def test_transport_failure_maps_to_network_error(config):
    class Boom(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):  # noqa: ANN001
            raise httpx.ConnectError("no route to host")

    async with FreshdeskClient(config, transport=Boom()) as client:
        with pytest.raises(NetworkError):
            await client.get("/tickets")


async def test_error_envelopes_are_json_serialisable():
    """Every error shape must survive json.dumps - agents receive text, not objects."""
    for exc in (
        AuthError("bad key"),
        RateLimited("slow down", retry_after=30),
        ValidationError("bad field", details={"freshdesk_errors": [{"field": "status"}]}),
        UpstreamError("boom", http_status=503),
    ):
        payload = json.loads(json.dumps(exc.to_dict()))
        assert payload["ok"] is False
        assert payload["error"]["code"]
        assert payload["error"]["agent_hint"]
        assert isinstance(payload["error"]["retryable"], bool)


async def test_audit_log_records_calls_without_secrets(tmp_path, config):
    audit = tmp_path / "audit.jsonl"
    audited = make_config(audit_log=audit)
    app = build_app(MockSettings(api_key=config.api_key))
    async with FreshdeskClient(audited, transport=httpx.ASGITransport(app=app)) as client:
        await client.get("/tickets", params={"email": "someone@example.com"})
    lines = [json.loads(line) for line in audit.read_text().splitlines()]
    assert lines and lines[0]["event"] == "api_call"
    record = lines[0]
    assert record["method"] == "GET" and record["status"] == 200
    # Param *keys* are logged for debugging; values (which may be PII) are not.
    assert record["param_keys"] == ["email"]
    assert "someone@example.com" not in audit.read_text()
    assert config.api_key not in audit.read_text()
