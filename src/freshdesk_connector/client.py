"""HTTP transport for the Freshdesk v2 API.

Responsibilities kept here (and nowhere else):

* **Authentication** - Freshdesk uses HTTP Basic with the API key as the username
  and the literal string ``X`` as the password. The key never leaves this module.
* **Retry policy** - 429 honours ``Retry-After``; 5xx and transport errors use
  exponential backoff with full jitter; 4xx never retries (a retry costs budget
  and cannot succeed).
* **Deadlines** - every call is bounded. When the budget runs out we return
  *partial* results with an explicit warning instead of hanging an agent turn.
* **Pagination** - opaque, resumable, tamper-evident cursors; enforces
  Freshdesk's page ceilings (list: 300 pages; search: 10 pages of 30).
* **Audit** - optional JSONL trail of call metadata for debugging and for
  proving what an agent did. No bodies, no secrets.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

import httpx

from .config import Config
from .errors import (
    FreshdeskError,
    LocalBudgetExceeded,
    NetworkError,
    RateLimited,
    ValidationError,
    from_status,
)
from .ratelimit import (
    RateLimitGovernor,
    RateLimitHeaders,
    classify_bucket,
)
from .models import (
    LIST_MAX_PAGE,
    MAX_PER_PAGE,
    SEARCH_MAX_PAGE,
    SEARCH_PER_PAGE,
)

log = logging.getLogger("freshdesk_connector.client")

API_PREFIX = "/api/v2"
CURSOR_VERSION = 1


def _extract(payload: Any, mode: str) -> tuple[list[dict[str, Any]], int | None]:
    """Normalise list vs search envelopes into (records, total)."""
    if isinstance(payload, Mapping):
        records = payload.get("results")
        total = payload.get("total")
        if isinstance(records, list):
            return [r for r in records if isinstance(r, dict)], total if isinstance(total, int) else None
        return ([payload] if payload else []), None
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)], None
    return [], None


@dataclass
class Page:
    """One page of results plus everything an agent needs to continue safely."""

    items: list[dict[str, Any]] = field(default_factory=list)
    total: int | None = None
    next_cursor: str | None = None
    pages_fetched: int = 0
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.items)


def encode_cursor(*, path: str, params: Mapping[str, Any], offset: int, mode: str) -> str:
    """Opaque cursor. The signature binds a cursor to the query that produced it."""
    payload = {
        "v": CURSOR_VERSION,
        "m": mode,
        "off": offset,
        "sig": _signature(path, params),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, *, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except Exception as exc:  # noqa: BLE001 - any malformed cursor is a client bug
        raise ValidationError(
            "The supplied cursor is not a valid pagination cursor for this connector.",
            agent_hint="Restart the listing without a cursor.",
        ) from exc
    if payload.get("v") != CURSOR_VERSION:
        raise ValidationError("Cursor version mismatch; restart the listing without a cursor.")
    if payload.get("sig") != _signature(path, params):
        raise ValidationError(
            "This cursor was minted for a different query. Cursors are bound to the filters that "
            "produced them so a resumed page can never silently mix two result sets.",
            agent_hint="Re-issue the request with the original filters, or start a fresh listing.",
        )
    return payload


def _signature(path: str, params: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        {"path": path, "params": {k: str(v) for k, v in sorted(params.items()) if v is not None}},
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha1(canonical.encode()).hexdigest()[:16]


class FreshdeskClient:
    """Async Freshdesk API client with connector-grade guardrails."""

    def __init__(
        self,
        config: Config,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        governor: RateLimitGovernor | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config = config
        self.governor = governor or RateLimitGovernor(config.plan, clock=clock, sleeper=sleeper)
        self._clock = clock
        self._sleep = sleeper
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._owns_client = True
        self.stats: dict[str, int] = {"calls": 0, "retries": 0, "errors": 0, "throttled": 0}

    # ------------------------------------------------------------------ lifecycle
    async def __aenter__(self) -> "FreshdeskClient":
        self._client = httpx.AsyncClient(
            base_url=self.config.base_url,
            timeout=httpx.Timeout(self.config.timeout),
            transport=self._transport,
            headers={
                "Accept": "application/json",
                "User-Agent": "freshdesk-mcp-connector/0.1 (+agent-studio)",
            },
            auth=httpx.BasicAuth(self.config.api_key, "X"),
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ utilities
    def deadline(self, seconds: float | None = None) -> float:
        return self._clock() + (seconds if seconds is not None else self.config.request_deadline)

    def _audit(self, record: dict[str, Any]) -> None:
        path = self.config.audit_log
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            record["ts"] = datetime.now(timezone.utc).isoformat()
            record["domain"] = self.config.domain
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        except OSError as exc:  # pragma: no cover - audit must never break a call
            log.warning("could not write audit log: %s", exc)

    # ------------------------------------------------------------------- request
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        deadline: float | None = None,
    ) -> Any:
        """Perform one API call with rate-limiting, retries and error mapping."""
        if self._client is None:
            raise RuntimeError("FreshdeskClient must be used as an async context manager.")
        deadline = deadline if deadline is not None else self.deadline()
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        bucket = classify_bucket(method, path)
        attempts = max(1, self.config.max_retries)
        last_error: FreshdeskError | None = None

        for attempt in range(1, attempts + 1):
            await self.governor.acquire(bucket, deadline=deadline)
            started = self._clock()
            try:
                response = await self._client.request(
                    method, f"{API_PREFIX}{path}", params=clean_params, json=json_body
                )
            except httpx.TimeoutException as exc:
                last_error = NetworkError(f"Timed out after {self.config.timeout}s calling {path}: {exc}")
                self._audit({"event": "timeout", "method": method, "path": path, "attempt": attempt})
                if attempt == attempts or self._clock() + 1.0 > deadline:
                    raise last_error from exc
                await self._backoff(attempt, deadline, last_error)
                continue
            except httpx.HTTPError as exc:
                last_error = NetworkError(f"Could not reach Freshdesk: {exc}")
                self._audit({"event": "transport_error", "method": method, "path": path, "attempt": attempt})
                if attempt == attempts or self._clock() + 1.0 > deadline:
                    raise last_error from exc
                await self._backoff(attempt, deadline, last_error)
                continue

            latency_ms = int((self._clock() - started) * 1000)
            headers = RateLimitHeaders.from_headers(response.headers)
            self.governor.observe(bucket, headers, response.status_code)
            self.stats["calls"] += 1
            self._audit(
                {
                    "event": "api_call",
                    "method": method,
                    "path": path,
                    "param_keys": sorted(clean_params),
                    "status": response.status_code,
                    "latency_ms": latency_ms,
                    "bucket": bucket,
                    "attempt": attempt,
                    "rate_limit_remaining": headers.remaining,
                    "rate_limit_total": headers.total,
                }
            )

            if 200 <= response.status_code < 300:
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    return response.json()
                except ValueError as exc:
                    raise FreshdeskError(
                        "Freshdesk returned a non-JSON body for a successful response.",
                        details={"path": path, "status": response.status_code},
                    ) from exc

            body: Any = None
            try:
                body = response.json()
            except ValueError:
                body = response.text[:300]

            error = from_status(response.status_code, body, retry_after=headers.retry_after)
            last_error = error
            self.stats["errors"] += 1

            if isinstance(error, RateLimited):
                self.stats["throttled"] += 1
                wait = self.governor.note_429(bucket, headers.retry_after)
                if attempt < attempts and self._clock() + wait <= deadline:
                    await self._sleep(wait)
                    self.stats["retries"] += 1
                    continue
                raise error

            if error.retryable and attempt < attempts:
                await self._backoff(attempt, deadline, error)
                self.stats["retries"] += 1
                continue
            raise error

        raise last_error or FreshdeskError("Request failed for an unknown reason.")

    async def _backoff(self, attempt: int, deadline: float, error: FreshdeskError) -> None:
        delay = min(8.0, 0.5 * (2 ** (attempt - 1))) + random.uniform(0, 0.4)
        if self._clock() + delay > deadline:
            raise error
        await self._sleep(delay)

    # ------------------------------------------------------------------ paginate
    async def paginate(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        mode: str = "list",
        max_pages: int = 1,
        limit: int | None = None,
        cursor: str | None = None,
        per_page: int | None = None,
        deadline: float | None = None,
    ) -> Page:
        """Walk Freshdesk pagination without blowing a page ceiling or the deadline.

        ``mode`` selects endpoint behaviour:

        * ``list``   - ``/tickets``, ``/contacts``: ``per_page`` up to 100, ``page`` up to 300.
        * ``search`` - ``/search/tickets``: fixed 30 per page, ``page`` must be <= 10 and
          ``per_page`` is rejected outright, so at most 300 records are reachable.

        Cursors are **byte offsets into the result set**, not page numbers, so a
        resumed page can never skip or repeat a record even when the page size
        differs between calls. They are also signature-bound to the query that
        produced them.
        """
        params = dict(params or {})
        page_cap = SEARCH_MAX_PAGE if mode == "search" else LIST_MAX_PAGE
        if mode == "search":
            page_size = SEARCH_PER_PAGE
        else:
            # Match the page size to what the caller asked for. Freshdesk only pages
            # by whole records, so this keeps every cursor page-aligned and avoids
            # re-fetching a page just to skip past records we already returned.
            requested = per_page or (limit if limit else MAX_PER_PAGE)
            page_size = max(1, min(int(requested), MAX_PER_PAGE))
        ceiling = page_cap * page_size

        offset = 0
        if cursor:
            payload = decode_cursor(cursor, path=path, params=params)
            if payload.get("m") != mode:
                raise ValidationError("Cursor was issued for a different listing mode.")
            offset = max(0, int(payload.get("off", 0)))

        warnings: list[str] = []
        if offset >= ceiling:
            warnings.append(
                f"This endpoint cannot expose more than {ceiling} records in one result set "
                f"(page {page_cap} of {page_size}). Narrow the filters to see further."
            )
            return Page(items=[], total=None, next_cursor=None, pages_fetched=0, truncated=True, warnings=warnings)

        page = offset // page_size + 1
        skip = offset % page_size
        target = ceiling if limit is None else min(ceiling, offset + limit)

        collected: list[dict[str, Any]] = []
        total: int | None = None
        pages_fetched = 0
        truncated = False
        exhausted = False
        deadline = deadline if deadline is not None else self.deadline()

        while pages_fetched < max_pages:
            if page > page_cap:
                truncated = True
                warnings.append(
                    f"Reached Freshdesk's hard pagination ceiling of {page_cap} pages "
                    f"({ceiling} records) for this endpoint. Narrow the filters "
                    "(for example, split the date range) to see older records."
                )
                break
            try:
                payload = await self.request(
                    "GET",
                    path,
                    params={**params, "page": page, **({} if mode == "search" else {"per_page": page_size})},
                    deadline=deadline,
                )
            except FreshdeskError as exc:
                # Degrade rather than fail once we already hold real data: an agent
                # that gets 30 tickets plus an honest warning beats one that gets a
                # stack trace. The first page still propagates, because there the
                # caller genuinely has nothing.
                if pages_fetched == 0:
                    raise
                if isinstance(exc, LocalBudgetExceeded):
                    self.governor.events.append({"event": "budget_exceeded", "path": path})
                    reason = f"rate-limit budget ({exc.message})"
                else:
                    reason = f"{exc.code}: {exc.message}"
                warnings.append(
                    f"Stopped after {pages_fetched} page(s) because of {reason}. "
                    "Partial results are returned and more records may exist."
                )
                truncated = True
                break

            batch, page_total = _extract(payload, mode)
            raw_len = len(batch)
            if page_total is not None:
                total = page_total
            if skip:
                batch = batch[skip:]
                skip = 0

            collected.extend(batch)
            pages_fetched += 1
            page += 1

            if raw_len < page_size:
                exhausted = True
                break
            if len(collected) >= target - offset:
                break
            if self._clock() > deadline - 0.05:
                truncated = True
                warnings.append(
                    "Stopped early because the per-call time budget was exhausted; more records "
                    "may exist. Pass the returned cursor to continue."
                )
                break

        if limit is not None and len(collected) > limit:
            collected = collected[:limit]

        next_offset = offset + len(collected)
        next_cursor = None
        if not exhausted and next_offset < ceiling and pages_fetched:
            next_cursor = encode_cursor(
                path=path,
                params={k: v for k, v in params.items() if v is not None},
                offset=next_offset,
                mode=mode,
            )
        elif not exhausted and next_offset >= ceiling:
            truncated = True
            warnings.append(
                f"Reached this endpoint's {ceiling}-record reach; older records need narrower filters."
            )

        return Page(
            items=collected,
            total=total,
            next_cursor=next_cursor,
            pages_fetched=pages_fetched,
            truncated=truncated,
            warnings=warnings,
        )

    # --------------------------------------------------------------- conveniences
    async def get(
        self, path: str, *, params: Mapping[str, Any] | None = None, deadline: float | None = None
    ) -> Any:
        return await self.request("GET", path, params=params, deadline=deadline)

    async def post(
        self,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        deadline: float | None = None,
    ) -> Any:
        return await self.request("POST", path, json_body=json_body, deadline=deadline)

    def rate_limit_status(self) -> dict[str, Any]:
        return {**self.governor.status(), "client_stats": dict(self.stats)}
