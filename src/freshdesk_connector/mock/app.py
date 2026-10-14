"""A local, offline stand-in for the Freshdesk v2 API.

Why build a mock instead of hitting a real trial account?

* Tests must be deterministic and must not consume a merchant's shared rate-limit
  budget, nor leave artefacts in someone's helpdesk.
* The interesting behaviour is the *failure* behaviour - 429s with ``Retry-After``,
  per-endpoint sub-limits, validation errors on unknown filter fields, the 30-day
  default window, the 300-ticket search ceiling. A mock reproduces all of that on
  demand through configuration rather than by luck.

Everything it returns is generated from fictional fixtures, and it is deliberately
faithful to the real API's shapes and quirks:

* ``X-RateLimit-Total/Remaining/Used-CurrentRequest`` on *every* response,
* ``Retry-After`` on 429,
* ``{"description": "Validation failed", "errors": [...]}`` on a bad filter field,
* ``agent_id`` as the filter name for the field the object calls ``responder_id``,
* ``?include=`` costing 3 units of budget instead of 1.

Run it standalone::

    uvicorn freshdesk_connector.mock.app:app --port 8099
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import fixtures
from .query import CONTACT_FILTER_FIELDS, QueryError, matches, parse

WINDOW_SECONDS = 60.0
MAX_PER_PAGE = 100
SEARCH_PER_PAGE = 30
SEARCH_MAX_PAGE = 10
LIST_MAX_PAGE = 300

#: Reported in X-RateLimit-Total when rate limiting is switched off, so clients
#: still exercise their header-parsing path. Matches the Enterprise ceiling.
REPORTED_TOTAL_WHEN_DISABLED = 700


@dataclass
class MockSettings:
    """Knobs that make failure modes reproducible in tests and demos."""

    api_key: str = "mock-api-key"
    domain: str = "acme"
    overall_per_minute: int = 0  # 0 disables rate limiting (headers still sent)
    bucket_caps: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0
    chaos_500_rate: float = 0.0
    chaos_429_rate: float = 0.0
    default_window_days: int = 30
    seed: int = 7
    #: Seconds per rate-limit window. Real Freshdesk uses 60; tests shrink it so
    #: backoff behaviour can be exercised without real-time waits.
    window_seconds: float = 60.0
    #: Deterministic failure injection: bucket -> 1-based call ordinals that must
    #: return 429. Preferred over probabilistic chaos because it is reproducible.
    fail_on_call: dict[str, list[int]] = field(default_factory=dict)
    retry_after_seconds: int = 2

    @classmethod
    def from_env(cls) -> "MockSettings":
        def _f(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, default))
            except ValueError:
                return default

        def _i(name: str, default: int) -> int:
            try:
                return int(os.environ.get(name, default))
            except ValueError:
                return default

        return cls(
            api_key=os.environ.get("MOCK_API_KEY", "mock-api-key"),
            domain=os.environ.get("MOCK_DOMAIN", "acme"),
            overall_per_minute=_i("MOCK_RATE_LIMIT", 0),
            bucket_caps={
                "tickets_list": _i("MOCK_TICKET_LIST_LIMIT", 0),
                "tickets_search": _i("MOCK_TICKET_SEARCH_LIMIT", 0),
                "contacts_list": _i("MOCK_CONTACTS_LIST_LIMIT", 0),
            },
            latency_ms=_i("MOCK_LATENCY_MS", 0),
            chaos_500_rate=_f("MOCK_CHAOS_500", 0.0),
            chaos_429_rate=_f("MOCK_CHAOS_429", 0.0),
            window_seconds=_f("MOCK_WINDOW_SECONDS", 60.0),
            retry_after_seconds=_i("MOCK_RETRY_AFTER", 2),
        )


def bucket_for(path: str) -> str:
    """Mirror Freshdesk's per-endpoint accounting buckets."""
    if "search/tickets" in path:
        return "tickets_search"
    if path.endswith("/tickets"):
        return "tickets_list"
    if path.endswith("/contacts"):
        return "contacts_list"
    return "default"


class MockState:
    """In-memory dataset + rate-limit accounting for the mock."""

    def __init__(self, settings: MockSettings) -> None:
        self.settings = settings
        self.rng = random.Random(settings.seed)
        self.tickets = fixtures.build_tickets()
        self.conversations = fixtures.build_conversations()
        self.contacts = fixtures.build_contacts()
        self.agents = fixtures.build_agents()
        self.groups = fixtures.build_groups()
        self.companies = fixtures.build_companies()
        self.ticket_fields = fixtures.build_ticket_fields()
        self.custom_field_names = {f["name"] for f in fixtures.CUSTOM_TICKET_FIELDS}
        self.calls: deque[tuple[float, int]] = deque()
        self.bucket_calls: dict[str, deque[tuple[float, int]]] = defaultdict(deque)
        self.total_calls = 0
        self.note_counter = 0
        self.forced_429s = 0
        self.bucket_call_counts: dict[str, int] = defaultdict(int)

    # ---------------------------------------------------------- rate limiting
    @property
    def window(self) -> float:
        return self.settings.window_seconds

    def _prune(self, now: float) -> None:
        while self.calls and now - self.calls[0][0] >= self.window:
            self.calls.popleft()
        for window in self.bucket_calls.values():
            while window and now - window[0][0] >= self.window:
                window.popleft()

    def account_call(self, bucket: str, cost: int) -> tuple[bool, dict[str, str]]:
        """Charge one call. Returns (allowed, headers) - headers on every response."""
        self._prune(time.monotonic())
        now = time.monotonic()
        limits_on = self.settings.overall_per_minute > 0
        total = self.settings.overall_per_minute if limits_on else REPORTED_TOTAL_WHEN_DISABLED
        bucket_cap = (
            self.settings.bucket_caps.get(bucket) or self.settings.overall_per_minute
            if limits_on
            else REPORTED_TOTAL_WHEN_DISABLED
        )

        used = sum(c for _, c in self.calls)
        bucket_used = sum(c for _, c in self.bucket_calls.get(bucket, ()))

        headers: dict[str, str] = {}
        if limits_on:
            headers = {
                "X-RateLimit-Total": str(total),
                "X-RateLimit-Remaining": str(max(0, total - used - cost)),
                "X-RateLimit-Used-CurrentRequest": str(cost),
            }

        over = limits_on and (used + cost > total or bucket_used + cost > bucket_cap)
        if over:
            oldest = self.calls[0][0] if self.calls else now
            headers["Retry-After"] = str(max(1, int(self.window - (now - oldest)) + 1))
            return False, headers

        # Deterministic failure injection: 429 the Nth call to a bucket, so
        # retry/backoff/degradation paths are reproducible in tests.
        self.bucket_call_counts[bucket] += 1
        if self.bucket_call_counts[bucket] in self.settings.fail_on_call.get(bucket, []):
            self.forced_429s += 1
            self.calls.append((now, cost))
            self.bucket_calls[bucket].append((now, cost))
            headers["Retry-After"] = str(self.settings.retry_after_seconds)
            headers["X-RateLimit-Remaining"] = "0"
            return False, headers

        self.calls.append((now, cost))
        self.bucket_calls[bucket].append((now, cost))
        self.total_calls += 1
        return True, headers

    def force_429(self, bucket: str) -> bool:
        """Apply configured chaos: pretend this call hit the ceiling."""
        if self.settings.chaos_429_rate and self.rng.random() < self.settings.chaos_429_rate:
            self.forced_429s += 1
            self.bucket_calls[bucket].append((time.monotonic(), 1))
            return True
        return False

    def window_snapshot(self) -> dict[str, Any]:
        self._prune(time.monotonic())
        return {
            "calls_this_window": sum(c for _, c in self.calls),
            "buckets_this_window": {k: sum(c for _, c in v) for k, v in self.bucket_calls.items() if v},
            "forced_429s": self.forced_429s,
            "bucket_call_counts": dict(self.bucket_call_counts),
            "total_calls_all_time": self.total_calls,
        }


def build_app(settings: MockSettings | None = None, state: MockState | None = None) -> FastAPI:
    settings = settings or MockSettings()
    state = state or MockState(settings)
    security = HTTPBasic(auto_error=False)

    app = FastAPI(title="Mock Freshdesk API", version="2.0-mock")
    app.state.mock = state
    app.state.settings = settings

    # ------------------------------------------------------------- projections
    def with_includes(ticket: dict[str, Any], include: str | None) -> dict[str, Any]:
        """Emulate ``?include=`` - embeds related objects on demand (at extra cost)."""
        out = dict(ticket)
        keys = {k.strip() for k in (include or "").split(",") if k.strip()}
        if "requester" in keys:
            for c in state.contacts:
                if c["id"] == ticket.get("requester_id"):
                    out["requester"] = c
                    break
        if "company" in keys:
            for c in state.companies:
                if c["id"] == ticket.get("company_id"):
                    out["company"] = c
                    break
        return out

    # ---------------------------------------------------------------- plumbing
    def authenticate(credentials: HTTPBasicCredentials | None = Depends(security)) -> str:
        if credentials is None or credentials.username != settings.api_key or credentials.password != "X":
            raise HTTPException(
                status_code=401, detail={"code": "invalid_credentials", "message": "Invalid credentials"}
            )
        return credentials.username

    def validation_error(field_name: str, message: str, code: str = "invalid_field") -> HTTPException:
        return HTTPException(
            status_code=400,
            detail={
                "description": "Validation failed",
                "errors": [{"field": field_name, "message": message, "code": code}],
            },
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException):
        payload = exc.detail if isinstance(exc.detail, dict) else {"message": exc.detail}
        return JSONResponse(status_code=exc.status_code, content=payload)

    @app.middleware("http")
    async def rate_limit_and_chaos(request: Request, call_next: Callable) -> Response:
        path = request.url.path
        if not path.startswith("/api/v2"):
            return await call_next(request)

        if settings.latency_ms:
            await asyncio.sleep(settings.latency_ms / 1000.0)

        bucket = bucket_for(path)
        # Freshdesk charges more than one unit when `include=` is used.
        cost = 3 if "include=" in str(request.url).lower() else 1
        allowed, headers = state.account_call(bucket, cost)

        if allowed and state.force_429(bucket):
            allowed = False
            headers = {**headers, "Retry-After": "2"}

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "description": "API rate limit exceeded",
                    "errors": [{"message": "Too many requests", "code": "rate_limit_exceeded"}],
                },
                headers=headers,
            )

        if settings.chaos_500_rate and state.rng.random() < settings.chaos_500_rate:
            return JSONResponse(status_code=500, content={"description": "Internal server error"}, headers=headers)

        response: Response = await call_next(request)
        for key, value in headers.items():
            response.headers[key] = value
        return response

    # ------------------------------------------------------------------ routes
    @app.get("/")
    async def root() -> dict[str, Any]:
        return {
            "service": "mock-freshdesk",
            "note": "Local stand-in for https://<domain>.freshdesk.com/api/v2 - fictional data only.",
            "tickets": len(state.tickets),
            "rate_limit_per_minute": settings.overall_per_minute or "disabled",
        }

    @app.get("/__mock/stats")
    async def stats() -> dict[str, Any]:
        return state.window_snapshot()

    @app.get("/api/v2/ticket_fields")
    async def ticket_fields(_: str = Depends(authenticate)) -> list[dict[str, Any]]:
        return state.ticket_fields

    @app.get("/api/v2/groups")
    async def groups(_: str = Depends(authenticate)) -> list[dict[str, Any]]:
        return state.groups

    @app.get("/api/v2/agents")
    async def agents(_: str = Depends(authenticate)) -> list[dict[str, Any]]:
        return state.agents

    @app.get("/api/v2/companies/{company_id}")
    async def company(company_id: int, _: str = Depends(authenticate)) -> dict[str, Any]:
        for c in state.companies:
            if c["id"] == company_id:
                return c
        raise HTTPException(status_code=404, detail={"description": "Company not found"})

    # ---- contacts
    @app.get("/api/v2/contacts")
    async def list_contacts(
        _: str = Depends(authenticate),
        email: str | None = None,
        company_id: int | None = None,
        per_page: int = Query(30, ge=1, le=MAX_PER_PAGE),
        page: int = Query(1, ge=1, le=LIST_MAX_PAGE),
    ) -> list[dict[str, Any]]:
        items = list(state.contacts)
        if email:
            items = [c for c in items if str(c.get("email", "")).lower() == email.strip().lower()]
        if company_id:
            items = [c for c in items if c.get("company_id") == company_id]
        start, end = (page - 1) * per_page, page * per_page
        return items[start:end]

    @app.get("/api/v2/contacts/{contact_id}")
    async def get_contact(contact_id: int, _: str = Depends(authenticate)) -> dict[str, Any]:
        for c in state.contacts:
            if c["id"] == contact_id:
                return c
        raise HTTPException(status_code=404, detail={"description": "Contact not found"})

    @app.get("/api/v2/search/contacts")
    async def search_contacts(
        _: str = Depends(authenticate),
        query: str = Query(...),
        page: int = Query(1, ge=1, le=SEARCH_MAX_PAGE),
    ) -> dict[str, Any]:
        try:
            node = parse(query, allowed_fields=CONTACT_FILTER_FIELDS)
        except QueryError as exc:
            raise validation_error(exc.field or "query", str(exc), exc.code) from exc
        results = [c for c in state.contacts if matches(node, {**c, "tag": c.get("tags") or []})]
        start = (page - 1) * SEARCH_PER_PAGE
        return {"total": len(results), "results": results[start : start + SEARCH_PER_PAGE]}

    # ---- tickets
    @app.get("/api/v2/tickets")
    async def list_tickets(
        _: str = Depends(authenticate),
        filter: str | None = None,  # noqa: A002 - the real API's parameter name
        requester_id: int | None = None,
        company_id: int | None = None,
        updated_since: str | None = None,
        order_by: str = "updated_at",
        order_type: str = "desc",
        include: str | None = None,
        per_page: int = Query(30, ge=1, le=MAX_PER_PAGE),
        page: int = Query(1, ge=1, le=LIST_MAX_PAGE),
    ) -> list[dict[str, Any]]:
        items = list(state.tickets)

        if filter == "spam":
            items = [t for t in items if t.get("spam")]
        elif filter == "deleted":
            items = [t for t in items if t.get("deleted")]
        elif filter == "watching":
            items = [t for t in items if t["id"] % 3 == 0]
        elif filter == "new_and_my_open":
            items = [t for t in items if t.get("status") in (2, 3)]
        else:
            items = [t for t in items if not t.get("spam") and not t.get("deleted")]

        if updated_since:
            cutoff = _parse_date(updated_since)
            items = [t for t in items if _parse_date(str(t["updated_at"])) >= cutoff]
        elif filter not in {"spam", "deleted"}:
            # Freshdesk silently limits the default listing to tickets *created* in
            # the last 30 days - the most common cause of "the API is missing data".
            cutoff = datetime.now(timezone.utc) - timedelta(days=settings.default_window_days)
            items = [t for t in items if _parse_date(str(t["created_at"])) >= cutoff]

        if requester_id:
            items = [t for t in items if t.get("requester_id") == requester_id]
        if company_id:
            items = [t for t in items if t.get("company_id") == company_id]

        key = order_by if order_by in {"created_at", "updated_at", "status", "priority", "due_by"} else "updated_at"
        reverse = (order_type or "desc").lower() != "asc"
        items.sort(key=lambda t: str(t.get(key) or ""), reverse=reverse)

        start, end = (page - 1) * per_page, page * per_page
        return [with_includes(t, include) for t in items[start:end]]

    @app.get("/api/v2/tickets/{ticket_id}")
    async def get_ticket(
        ticket_id: int, _: str = Depends(authenticate), include: str | None = None
    ) -> dict[str, Any]:
        for t in state.tickets:
            if t["id"] == ticket_id:
                return with_includes(t, include)
        raise HTTPException(status_code=404, detail={"description": "Ticket not found"})

    @app.get("/api/v2/tickets/{ticket_id}/conversations")
    async def get_conversations(ticket_id: int, _: str = Depends(authenticate)) -> list[dict[str, Any]]:
        if not any(t["id"] == ticket_id for t in state.tickets):
            raise HTTPException(status_code=404, detail={"description": "Ticket not found"})
        return state.conversations.get(ticket_id, [])

    @app.post("/api/v2/tickets/{ticket_id}/notes")
    async def add_note(ticket_id: int, request: Request, _: str = Depends(authenticate)) -> dict[str, Any]:
        if not any(t["id"] == ticket_id for t in state.tickets):
            raise HTTPException(status_code=404, detail={"description": "Ticket not found"})
        body = await request.json()
        text = str((body or {}).get("body") or "")
        if not text.strip():
            raise validation_error("body", "Body cannot be blank", "missing_field")
        state.note_counter += 1
        note = {
            "id": 900000 + state.note_counter,
            "ticket_id": ticket_id,
            "user_id": 2001,
            "private": bool(body.get("private", True)),
            "incoming": False,
            "body": f"<div>{text}</div>",
            "body_text": text,
            "attachments": [],
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": 0,
        }
        state.conversations.setdefault(ticket_id, []).append(note)
        return note

    @app.get("/api/v2/search/tickets")
    async def search_tickets(
        _: str = Depends(authenticate),
        query: str = Query(...),
        page: int = Query(1, ge=1),
    ) -> dict[str, Any]:
        if page > SEARCH_MAX_PAGE:
            raise validation_error("page", f"page must not exceed {SEARCH_MAX_PAGE}", "invalid_page")
        try:
            node = parse(query, allowed_custom_fields=state.custom_field_names)
        except QueryError as exc:
            raise validation_error(exc.field or "query", str(exc), exc.code) from exc

        results = [t for t in state.tickets if not t.get("deleted") and matches(node, t)]
        start = (page - 1) * SEARCH_PER_PAGE
        return {"total": len(results), "results": results[start : start + SEARCH_PER_PAGE]}

    return app


def _parse_date(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


#: Module-level app for `uvicorn freshdesk_connector.mock.app:app`.
app = build_app(MockSettings.from_env())


def main() -> None:  # pragma: no cover - manual entry point
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("MOCK_PORT", "8099")))
