"""Error taxonomy for the Freshdesk connector.

Design goal: every failure returned to the agent must be *actionable*. An LLM
that receives ``{"code": "upstream_rate_limited", "retryable": true,
"agent_hint": "..."}`` can decide whether to retry, narrow the query, or stop and
ask a human. An LLM that receives an opaque ``500`` cannot.

Every error therefore carries:

* ``code``        - stable machine-readable identifier (safe to branch on)
* ``message``     - human-readable, ASCII, no secrets
* ``http_status`` - upstream status if there was one, else ``None``
* ``retryable``   - whether a retry could plausibly succeed
* ``agent_hint``  - one sentence telling the agent what to do next
* ``details``     - structured extras (e.g. Freshdesk field-level validation errors)
"""

from __future__ import annotations

from typing import Any


class FreshdeskError(Exception):
    """Base class for every error this connector raises or returns."""

    code: str = "connector_error"
    http_status: int | None = None
    retryable: bool = False
    agent_hint: str = "Report this failure to the user."

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        agent_hint: str | None = None,
        http_status: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if agent_hint is not None:
            self.agent_hint = agent_hint
        if http_status is not None:
            self.http_status = http_status
        if retryable is not None:
            self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        """Shape returned to the MCP client instead of a raw exception."""
        payload: dict[str, Any] = {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "agent_hint": self.agent_hint,
            },
        }
        if self.http_status is not None:
            payload["error"]["http_status"] = self.http_status
        if self.details:
            payload["error"]["details"] = self.details
        return payload

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.code}] {self.message}"


class ConfigError(FreshdeskError):
    """Local misconfiguration: missing/invalid credentials or settings."""

    code = "config_error"
    agent_hint = (
        "This is a human setup problem, not a query problem. Tell the operator to "
        "set FRESHDESK_DOMAIN and FRESHDESK_API_KEY (see .env.example) and restart "
        "the connector. Do not retry."
    )


class AuthError(FreshdeskError):
    """401 from Freshdesk: the API key is wrong, revoked, or lacks permission."""

    code = "auth_failed"
    http_status = 401
    retryable = False
    agent_hint = (
        "Credentials were rejected. Stop calling tools and ask a human to verify or "
        "rotate FRESHDESK_API_KEY. Retrying will not help and still consumes your "
        "account rate-limit budget."
    )


class PermissionDenied(FreshdeskError):
    """403: the key is valid but this agent/role may not read this object."""

    code = "permission_denied"
    http_status = 403
    retryable = False
    agent_hint = (
        "The API key authenticated but is not allowed to read this resource. Report "
        "the specific object you needed to the user; do not retry the same call."
    )


class NotFoundError(FreshdeskError):
    """404: the object does not exist (or was hard-deleted)."""

    code = "not_found"
    http_status = 404
    retryable = False
    agent_hint = (
        "The record does not exist. Re-check the id, or search for the record by "
        "email/subject instead of guessing another id."
    )


class ValidationError(FreshdeskError):
    """400: Freshdesk rejected the request, usually a bad filter field or value."""

    code = "invalid_request"
    http_status = 400
    retryable = False
    agent_hint = (
        "The request was rejected by Freshdesk. Fix the named field and retry once. "
        "For ticket filters, call freshdesk_get_ticket_fields first - searchable "
        "field names are account-specific and case sensitive."
    )


class RateLimited(FreshdeskError):
    """429 from Freshdesk. Distinct from LocalBudgetExceeded on purpose."""

    code = "upstream_rate_limited"
    http_status = 429
    retryable = True

    def __init__(self, message: str, *, retry_after: float | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after
        if retry_after:
            self.agent_hint = (
                f"Freshdesk rate limit hit; the connector already backed off. Wait "
                f"~{int(retry_after)}s, narrow the query (fewer pages, tighter date "
                f"range) and retry once."
            )
        else:
            self.agent_hint = (
                "Freshdesk rate limit hit. Wait, then narrow the query (fewer pages, "
                "tighter date range) and retry once."
            )
        self.details.setdefault("retry_after_seconds", retry_after)


class UpstreamError(FreshdeskError):
    """5xx from Freshdesk: transient on their side."""

    code = "upstream_error"
    retryable = True
    agent_hint = (
        "Freshdesk returned a server error. The connector retried with backoff. If it "
        "still failed, report the outage to the user rather than looping."
    )


class NetworkError(FreshdeskError):
    """Timeout or connection failure before a response was received."""

    code = "network_error"
    retryable = True
    agent_hint = (
        "The connector could not reach Freshdesk. Check connectivity/DNS, then retry "
        "once; if it persists, report it instead of looping."
    )


class LocalBudgetExceeded(FreshdeskError):
    """The connector chose to stop to protect the shared account budget."""

    code = "local_budget_exceeded"
    retryable = True
    agent_hint = (
        "The connector stopped early to protect the account-wide rate-limit budget "
        "that all other Freshdesk integrations share. Partial results are included. "
        "Re-issue the request later, or narrow it further."
    )


class WriteDisabled(FreshdeskError):
    """A write tool was called while the connector is read-only."""

    code = "writes_disabled"
    retryable = False
    agent_hint = (
        "This connector runs read-only. Do not attempt the write again; instead "
        "summarise the intended action and ask the user to perform it or to enable "
        "FRESHDESK_ENABLE_WRITES for a supervised session."
    )


def from_status(status: int, body: Any, *, retry_after: float | None = None) -> FreshdeskError:
    """Map an HTTP status + parsed body to the right error type."""
    detail: str = ""
    extra: dict[str, Any] = {}
    if isinstance(body, dict):
        # Freshdesk error shapes: {"description": "...", "errors":[{...}]}
        # or {"code": "...", "message": "..."}
        detail = str(body.get("description") or body.get("message") or "")
        if isinstance(body.get("errors"), list):
            extra["freshdesk_errors"] = body["errors"]
        if body.get("code") and not extra.get("freshdesk_code"):
            extra["freshdesk_code"] = body["code"]
    elif isinstance(body, str):
        detail = body[:300]

    summary = f"Freshdesk returned HTTP {status}"
    if detail:
        summary = f"{summary}: {detail}"

    if status == 400:
        return ValidationError(summary, details=extra, http_status=400)
    if status == 401:
        return AuthError(summary, details=extra)
    if status == 403:
        return PermissionDenied(summary, details=extra)
    if status == 404:
        return NotFoundError(summary, details=extra)
    if status == 429:
        return RateLimited(summary, retry_after=retry_after, details=extra)
    if status >= 500:
        return UpstreamError(summary, details=extra, http_status=status)
    return FreshdeskError(summary, details=extra, http_status=status)
