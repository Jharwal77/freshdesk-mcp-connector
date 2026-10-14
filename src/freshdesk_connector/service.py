"""Business operations exposed to the agent.

The MCP layer (``server.py``) is a thin wrapper over this module; everything
merchant- or agent-specific lives here so it can be unit-tested without a
transport, and so a future non-MCP client (REST, gRPC, a queue worker) can reuse
the same primitives.

Every public method returns a JSON-serialisable envelope:

.. code-block:: python

    {"ok": true, "data": {...}, "pagination": {...}, "warnings": [...], "meta": {...}}

Errors return ``{"ok": false, "error": {...actionable...}}`` instead of raising, so
the agent always gets a structured result it can reason about.
"""

from __future__ import annotations

import functools
import hashlib
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .client import FreshdeskClient, Page
from .config import Config
from .errors import (
    FreshdeskError,
    NotFoundError,
    ValidationError,
    WriteDisabled,
)
from .models import (
    MAX_PER_PAGE,
    build_ticket_query,
    compact_agent,
    compact_company,
    compact_contact,
    compact_conversation,
    compact_group,
    compact_ticket,
    normalize_records,
    ticket_field_summary,
)

#: Upper bound on records any single tool call may return. Chosen to keep the
#: agent's context window healthy: 100 compact tickets is roughly 30-50 KB.
MAX_RECORDS_PER_CALL = 100
DEFAULT_RECORDS = 25

#: Cap on the honest bounded text scan.
MAX_SCAN_PAGES = 10


def ok(
    data: Any,
    *,
    pagination: Mapping[str, Any] | None = None,
    warnings: Sequence[str] | None = None,
    next_actions: Sequence[str] | None = None,
    **meta: Any,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {"ok": True, "data": data}
    if pagination is not None:
        envelope["pagination"] = dict(pagination)
    if warnings:
        envelope["warnings"] = list(warnings)
    if next_actions:
        envelope["suggested_next_actions"] = list(next_actions)
    if meta:
        envelope["meta"] = {k: v for k, v in meta.items() if v is not None}
    return envelope


def fail(error: FreshdeskError) -> dict[str, Any]:
    return error.to_dict()


def envelope(fn: Callable[..., Awaitable[dict[str, Any]]]) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Turn every failure into structured data at the service boundary.

    Callers of this class are agents and demos, and neither handles exceptions
    well. A raised error becomes a traceback in a log nobody reads; an
    ``{"ok": false, "error": {...}}`` envelope becomes a decision the agent can
    act on. Unexpected (non-connector) exceptions still propagate, so genuine bugs
    stay visible instead of being silently swallowed.
    """

    @functools.wraps(fn)
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return await fn(self, *args, **kwargs)
        except FreshdeskError as exc:
            return fail(exc)

    return wrapper


def _pagination(page: Page, *, requested: int | None = None) -> dict[str, Any]:
    return {
        "returned": page.count,
        "total_matching": page.total,
        "pages_fetched": page.pages_fetched,
        "truncated": page.truncated,
        "next_cursor": page.next_cursor,
        "requested_limit": requested,
    }


class FreshdeskService:
    """Read-first operations over Freshdesk tickets, contacts and conversations."""

    def __init__(self, client: FreshdeskClient, config: Config) -> None:
        self.client = client
        self.config = config
        self._idempotency: dict[str, tuple[float, dict[str, Any]]] = {}
        self._started = time.monotonic()

    # ------------------------------------------------------------------ tickets
    @envelope
    async def list_tickets(
        self,
        *,
        predefined_filter: str | None = None,
        requester_id: int | None = None,
        company_id: int | None = None,
        updated_since: str | None = None,
        order_by: str = "updated_at",
        order_type: str = "desc",
        include_description: bool = False,
        limit: int = DEFAULT_RECORDS,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Browse tickets chronologically.

        This is the *container* listing: every non-deleted, non-spam ticket in the
        window, ordered, paginated. It defaults to the last 30 days because that is
        Freshdesk's own default and silently becomes a lie otherwise - the window
        is echoed back in ``meta`` so the agent can state its coverage.
        """
        limit = _clamp(limit, 1, MAX_RECORDS_PER_CALL, "limit")

        params: dict[str, Any] = {"order_by": order_by, "order_type": order_type}
        if predefined_filter:
            allowed = {
                "new_and_my_open",
                "watching",
                "spam",
                "deleted",
                "all_tickets",
            }
            if predefined_filter not in allowed:
                raise ValidationError(
                    f"Unknown predefined filter {predefined_filter!r}.",
                    details={"allowed": sorted(allowed)},
                )
            params["filter"] = predefined_filter
        if requester_id:
            params["requester_id"] = int(requester_id)
        if company_id:
            params["company_id"] = int(company_id)
        if include_description:
            params["include"] = "description"
        window = updated_since
        if window:
            params["updated_since"] = window

        page = await self.client.paginate(
            "/tickets", params=params, mode="list", limit=limit, cursor=cursor
        )
        tickets = [
            compact_ticket(
                t, portal_url=self.config.portal_url, include_description=include_description
            )
            for t in page.items
        ]
        meta = {
            "coverage_window": (
                f"updated_since={window}" if window else "Freshdesk default: tickets created in the last 30 days"
            ),
            "coverage_caveat": (
                None
                if window
                else "Freshdesk returns only tickets created in the past 30 days by default and "
                "silently omits older ones. Pass updated_since to widen it (max 300 pages / 30,000 tickets)."
            ),
            "order": f"{order_by} {order_type}",
        }
        return ok(tickets, pagination=_pagination(page, requested=limit), warnings=page.warnings, **meta)

    @envelope
    async def get_ticket(
        self,
        ticket_id: int,
        *,
        include_conversations: bool = False,
        include_requester: bool = True,
        include_company: bool = False,
        conversation_limit: int = 10,
    ) -> dict[str, Any]:
        includes: list[str] = ["stats"]
        if include_requester or include_company:
            includes.append("requester")
        if include_company:
            includes.append("company")
        raw = await self.client.get(f"/tickets/{int(ticket_id)}", params={"include": ",".join(includes)})
        if not isinstance(raw, Mapping):
            raise FreshdeskError(f"Unexpected payload for ticket {ticket_id}.")

        ticket = compact_ticket(
            raw, portal_url=self.config.portal_url, include_description=True, description_limit=1200
        )
        data: dict[str, Any] = {"ticket": ticket}
        warnings: list[str] = []

        if include_conversations:
            conv = await self.list_ticket_conversations(
                int(ticket_id), limit=conversation_limit, include_private=True
            )
            if conv.get("ok"):
                data["conversations"] = conv["data"]
                internal = sum(1 for c in conv["data"] if c.get("visibility") == "internal")
                if internal:
                    warnings.append(
                        f"{internal} of {len(conv['data'])} entries are internal notes. Never quote "
                        "internal notes to a customer."
                    )
            else:
                warnings.append("Ticket loaded, but conversations could not be fetched (see error).")
                data["conversations_error"] = conv["error"]
        return ok(data, warnings=warnings)

    @envelope
    async def search_tickets(
        self,
        *,
        status: str | int | Sequence[str | int] | None = None,
        priority: str | int | Sequence[str | int] | None = None,
        priority_at_least: str | int | None = None,
        source: str | int | Sequence[str | int] | None = None,
        type: str | None = None,
        agent_id: int | str | None = None,
        responder_id: int | str | None = None,
        group_id: int | str | None = None,
        requester_id: int | str | None = None,
        company_id: int | str | None = None,
        tag: str | Sequence[str] | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        updated_after: str | None = None,
        updated_before: str | None = None,
        due_before: str | None = None,
        custom_fields: Mapping[str, Any] | None = None,
        include_unassigned_agent: bool = False,
        limit: int = DEFAULT_RECORDS,
        max_pages: int = 1,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Structured filter search over tickets.

        Freshdesk's ``/search/tickets`` filters on *structured fields only* - there
        is no full-text search. It also returns a fixed 30 records per page and
        refuses to go past page 10, so any single structured query can see at most
        300 tickets. Both facts are surfaced in ``meta`` and ``warnings`` because an
        agent that believes it has searched "everything" will confidently give a
        wrong answer.
        """
        query = build_ticket_query(
            status=status,
            priority=priority,
            priority_at_least=priority_at_least,
            source=source,
            type=type,
            agent_id=agent_id,
            responder_id=responder_id,
            group_id=group_id,
            requester_id=requester_id,
            company_id=company_id,
            tag=tag,
            created_after=created_after,
            created_before=created_before,
            updated_after=updated_after,
            updated_before=updated_before,
            due_before=due_before,
            custom_fields=custom_fields,
            include_unassigned_agent=include_unassigned_agent,
        )
        limit = _clamp(limit, 1, 300, "limit")
        max_pages = _clamp(max_pages, 1, 10, "max_pages")
        page = await self.client.paginate(
            "/search/tickets",
            params={"query": query},
            mode="search",
            limit=limit,
            max_pages=max_pages,
            cursor=cursor,
        )
        tickets = [compact_ticket(t, portal_url=self.config.portal_url) for t in page.items]
        warnings = list(page.warnings)
        # Only escalate the caveat when it actually bites: a constant wall of
        # warnings trains an agent to ignore them.
        if (page.total or 0) > 300:
            warnings.append(
                f"{page.total} tickets match this filter, but the search endpoint can only reach 300 "
                "(30/page x 10 pages). Split by date range (created_after/created_before) to cover them all."
            )
        elif page.count >= 300:
            warnings.append(
                "Result set hit the search endpoint's 300-ticket ceiling; more may match this filter."
            )
        return ok(
            tickets,
            pagination=_pagination(page, requested=limit),
            warnings=warnings,
            generated_freshdesk_query=query,
            endpoint="/api/v2/search/tickets",
            search_limits="structured-field filters only; no full-text search; <=300 tickets per query",
        )

    @envelope
    async def search_tickets_by_text(
        self,
        text: str,
        *,
        status: str | int | Sequence[str | int] | None = None,
        priority: str | int | None = None,
        updated_since: str | None = None,
        include_descriptions: bool = False,
        limit: int = 20,
        max_pages: int = 3,
    ) -> dict[str, Any]:
        """Best-effort keyword search, implemented as a *bounded* client-side scan.

        Freshdesk has no full-text search, so this walks tickets a page at a time and
        matches case-insensitively in the subject (and optionally the description).
        It is deliberately explicit about coverage: the envelope reports exactly how
        many tickets were examined and whether the scan was complete. Treat the
        result as "matches found", not "all matches that exist".
        """
        needle = (text or "").strip().lower()
        if len(needle) < 3:
            raise ValidationError("Provide at least 3 characters to search for.")
        limit = _clamp(limit, 1, MAX_RECORDS_PER_CALL, "limit")
        max_pages = _clamp(max_pages, 1, MAX_SCAN_PAGES, "max_pages")

        # Narrow the candidate set with structured filters first - the cheaper the
        # scan, the fewer rate-limit units it costs.
        params: dict[str, Any] = {"order_by": "updated_at", "order_type": "desc"}
        if include_descriptions:
            params["include"] = "description"
        if updated_since:
            params["updated_since"] = updated_since

        matches: list[dict[str, Any]] = []
        scanned = 0
        pages = 0
        complete = False
        page_no = 1
        while pages < max_pages and len(matches) < limit:
            payload = await self.client.get(
                "/tickets",
                params={**params, "page": page_no, "per_page": MAX_PER_PAGE},
            )
            batch = normalize_records(payload)
            if not batch:
                complete = True
                break
            pages += 1
            for t in batch:
                scanned += 1
                haystacks = [str(t.get("subject") or "")]
                if include_descriptions:
                    haystacks.append(str(t.get("description_text") or t.get("description") or ""))
                if any(needle in h.lower() for h in haystacks):
                    matches.append(compact_ticket(t, portal_url=self.config.portal_url))
                    if len(matches) >= limit:
                        break
            if len(batch) < MAX_PER_PAGE:
                complete = True
                break
            page_no += 1

        warnings = [
            "Freshdesk exposes no full-text search. This result comes from a bounded scan of the "
            "most recently updated tickets, not from an index of the whole helpdesk.",
        ]
        if not complete:
            warnings.append(
                f"Scan incomplete: examined {scanned} tickets across {pages} page(s). Older or "
                "differently-filtered tickets were not checked. Narrow with updated_since/status, or "
                "raise max_pages (each page costs an API call from the account-wide budget)."
            )
        return ok(
            matches,
            pagination={
                "returned": len(matches),
                "pages_scanned": pages,
                "tickets_examined": scanned,
                "scan_complete": complete,
                "requested_limit": limit,
            },
            warnings=warnings,
            scan_window=params.get("updated_since") or "Freshdesk default window (last 30 days created)",
            searched_description=include_descriptions,
        )

    @envelope
    async def list_ticket_conversations(
        self, ticket_id: int, *, limit: int = 20, include_private: bool = True
    ) -> dict[str, Any]:
        limit = _clamp(limit, 1, MAX_RECORDS_PER_CALL, "limit")
        payload = await self.client.get(f"/tickets/{int(ticket_id)}/conversations")
        convs = [compact_conversation(c) for c in normalize_records(payload)]
        convs.sort(key=lambda c: str(c.get("created_at") or ""))
        if not include_private:
            convs = [c for c in convs if c.get("customer_visible")]
        warns: list[str] = []
        internal = sum(1 for c in convs if c.get("visibility") == "internal")
        if internal:
            warns.append(
                f"{internal} entr{'y' if internal == 1 else 'ies'} marked visibility=internal are private "
                "agent notes and were never shown to the customer."
            )
        return ok(convs[:limit], pagination={"returned": min(len(convs), limit), "total_matching": len(convs)}, warnings=warns)

    # ----------------------------------------------------------------- contacts
    @envelope
    async def get_contact(
        self,
        *,
        contact_id: int | None = None,
        email: str | None = None,
        phone: str | None = None,
    ) -> dict[str, Any]:
        """Look up a customer by id, email or phone.

        Exactly one identifier is required. Email is tried server-side; phone falls
        back to a contacts search, because Freshdesk does not index phone numbers on
        the list endpoint.
        """
        if contact_id is not None:
            raw = await self.client.get(f"/contacts/{int(contact_id)}")
            if not isinstance(raw, Mapping):
                raise FreshdeskError("Unexpected contact payload.")
            return ok(compact_contact(raw), matched_by="id")

        if email:
            payload = await self.client.get("/contacts", params={"email": email.strip()})
            found = normalize_records(payload)
            if found:
                return ok(compact_contact(found[0]), matched_by="email")
            # Fall back to the contacts search endpoint.
            payload = await self.client.get("/search/contacts", params={"query": f"email:'{email.strip()}'"})
            found = normalize_records(payload.get("results") if isinstance(payload, Mapping) else payload)
            if found:
                return ok(compact_contact(found[0]), matched_by="email_search")
            raise NotFoundError(
                f"No contact found with email {email!r}.",
                agent_hint=(
                    "The customer may exist as a requester without a contact record, or under a different "
                    "address. Search recent tickets by requester instead."
                ),
            )

        if phone:
            for digits in _phone_candidates(phone):
                for field in ("mobile", "phone"):
                    payload = await self.client.get(
                        "/search/contacts", params={"query": f"{field}:'{digits}'"}
                    )
                    found = normalize_records(
                        payload.get("results") if isinstance(payload, Mapping) else payload
                    )
                    if found:
                        return ok(compact_contact(found[0]), matched_by=f"{field}_search")
            raise NotFoundError(
                f"No contact found with phone {phone!r}.",
                agent_hint=(
                    "Phone matching needs the subscriber number; if the customer wrote in by email, "
                    "look them up that way instead."
                ),
            )

        raise ValidationError("Provide exactly one of contact_id, email or phone.")

    @envelope
    async def search_contacts(
        self,
        *,
        email: str | None = None,
        phone: str | None = None,
        name: str | None = None,
        company_id: int | None = None,
        limit: int = DEFAULT_RECORDS,
    ) -> dict[str, Any]:
        limit = _clamp(limit, 1, MAX_RECORDS_PER_CALL, "limit")
        clauses: list[str] = []
        if email:
            clauses.append(f"email:'{email.strip()}'")
        if phone:
            digits = _phone_candidates(phone)[0]
            clauses.append(f"(mobile:'{digits}' OR phone:'{digits}')")
        if company_id:
            clauses.append(f"company_id:{int(company_id)}")
        if name:
            clauses.append(f"name:'{name.strip()}'")
        if not clauses:
            raise ValidationError(
                "Provide at least one of email, phone, name or company_id.",
                agent_hint="Unfiltered contact listing is expensive; use a company_id or email to scope it.",
            )
        query = " AND ".join(clauses)
        payload = await self.client.get("/search/contacts", params={"query": query})
        results = normalize_records(payload.get("results") if isinstance(payload, Mapping) else payload)
        total = payload.get("total") if isinstance(payload, Mapping) else None
        return ok(
            [compact_contact(c) for c in results[:limit]],
            pagination={"returned": min(len(results), limit), "total_matching": total},
            generated_freshdesk_query=query,
        )

    @envelope
    async def get_company(self, company_id: int, *, include_contacts: bool = False) -> dict[str, Any]:
        raw = await self.client.get(f"/companies/{int(company_id)}")
        if not isinstance(raw, Mapping):
            raise FreshdeskError("Unexpected company payload.")
        data: dict[str, Any] = {"company": compact_company(raw)}
        warnings: list[str] = []
        if include_contacts:
            payload = await self.client.get("/contacts", params={"company_id": int(company_id), "per_page": 100})
            contacts = [compact_contact(c) for c in normalize_records(payload)]
            data["contacts"] = contacts[:MAX_RECORDS_PER_CALL]
            warnings.append(
                "Company contact lists can be large; only the first "
                f"{min(len(contacts), MAX_RECORDS_PER_CALL)} contacts are returned."
            )
        return ok(data, warnings=warnings)

    # ------------------------------------------------------------------ lookups
    @envelope
    async def list_agents(self, *, group_id: int | None = None, limit: int = 100) -> dict[str, Any]:
        payload = await self.client.get("/agents", params={"per_page": min(limit, MAX_PER_PAGE)})
        agents = [compact_agent(a) for a in normalize_records(payload)]
        if group_id is not None:
            agents = [a for a in agents if int(group_id) in (a.get("group_ids") or [])]
        return ok(agents, pagination={"returned": len(agents)}, note="Agents are needed to interpret agent_id/responder_id filters and assignments.")

    @envelope
    async def list_groups(self) -> dict[str, Any]:
        payload = await self.client.get("/groups", params={"per_page": 100})
        groups = [compact_group(g) for g in normalize_records(payload)]
        return ok(groups, pagination={"returned": len(groups)}, note="Groups are Freshdesk's routing teams; group_id filters map to these names.")

    @envelope
    async def get_ticket_fields(self) -> dict[str, Any]:
        """Teach the agent the account's real, case-sensitive field names.

        This is the tool that prevents the single most common Freshdesk failure:
        guessing a filter field that does not exist in *this* merchant's account and
        getting an opaque HTTP 400.
        """
        payload = await self.client.get("/ticket_fields")
        fields = normalize_records(payload)
        standard = []
        custom = []
        for f in fields:
            summary = ticket_field_summary(f)
            (custom if str(summary.get("name", "")).startswith("cf_") or f.get("default") is False else standard).append(summary)
        return ok(
            {
                "standard_fields": standard,
                "custom_fields": custom,
                "filter_field_names": [f["searchable_expression"] for f in standard + custom],
            },
            note=(
                "Pass these names (case-sensitive) as `custom_fields` when searching. Standard numeric "
                "fields such as status and priority accept the friendly labels."
            ),
        )

    # --------------------------------------------------------------------- ops
    @envelope
    async def connector_health(self) -> dict[str, Any]:
        """Cheap auth + connectivity probe that also reports the live budget."""
        started = time.monotonic()
        probe_error: dict[str, Any] | None = None
        try:
            await self.client.get("/ticket_fields", params={"per_page": 1})
        except FreshdeskError as exc:
            probe_error = exc.to_dict()["error"]
        latency_ms = int((time.monotonic() - started) * 1000)
        return ok(
            {
                "authenticated": probe_error is None,
                "domain": self.config.domain,
                "latency_ms": latency_ms,
                "writes_enabled": self.config.enable_writes,
                "uptime_seconds": round(time.monotonic() - self._started, 1),
                "probe": "/api/v2/ticket_fields?per_page=1",
            }
            | ({"error": probe_error} if probe_error else {}),
            warnings=[] if probe_error is None else ["The auth probe failed; tools will fail until credentials are fixed."],
        )

    @envelope
    async def rate_limit_status(self) -> dict[str, Any]:
        return ok(
            self.client.rate_limit_status(),
            note=(
                "Freshdesk limits are per account, shared with every other integration. Check this "
                "before a large scan rather than discovering the ceiling mid-task."
            ),
        )

    # -------------------------------------------------------------------- write
    @envelope
    async def add_private_note(
        self, ticket_id: int, body: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Add an internal note to a ticket - the only write this connector offers.

        Guardrails, in order:

        1. Registered only when ``FRESHDESK_ENABLE_WRITES=true``; otherwise the call
           fails closed with an explicit, non-retryable error.
        2. A private note is invisible to the customer, so the blast radius of a
           mistake is an internal comment rather than a customer-visible message.
        3. Notes are deduplicated on ``idempotency_key`` (or a hash of the body) for
           15 minutes, because Freshdesk has no native idempotency support and an
           agent that retries after a timeout would otherwise double-post.
        """
        if not self.config.enable_writes:
            raise WriteDisabled("Write tools are disabled (FRESHDESK_ENABLE_WRITES is not set to true).")
        text = (body or "").strip()
        if not text:
            raise ValidationError("Note body must not be empty.")
        if len(text) > 4000:
            raise ValidationError("Note body is capped at 4000 characters by this connector.")

        key = idempotency_key or hashlib.sha256(f"{ticket_id}:{text}".encode()).hexdigest()[:16]
        now = time.monotonic()
        self._idempotency = {k: v for k, v in self._idempotency.items() if now - v[0] < 900}
        if key in self._idempotency:
            cached = self._idempotency[key][1]
            return ok({**cached, "deduplicated": True}, note="Identical note already posted within 15 minutes; not posted again.")

        payload = await self.client.post(f"/tickets/{int(ticket_id)}/notes", json_body={"body": text, "private": True})
        result = {"ticket_id": int(ticket_id), "note_id": (payload or {}).get("id"), "private": True}
        self._idempotency[key] = (now, result)
        return ok(
            {**result, "deduplicated": False},
            warnings=["This note is internal (private=true). It is not visible to the customer."],
        )

    # ------------------------------------------------------------------ helpers
    async def resolve_group_names(self, tickets: Sequence[Mapping[str, Any]]) -> dict[int, str]:
        """Best-effort group_id -> name map for human-readable summaries."""
        try:
            groups = await self.list_groups()
        except FreshdeskError:
            return {}
        if not groups.get("ok"):
            return {}
        return {int(g["id"]): str(g.get("name")) for g in groups["data"] if g.get("id") is not None}


def _phone_candidates(phone: str) -> list[str]:
    """Digit strings worth trying for a phone number, most specific first.

    Customers and agents write numbers as ``+91 90000 77777``, ``9000077777`` or
    ``+919000077777``; Freshdesk stores whatever was typed at creation time. Trying
    the full digit string and then the trailing 10 digits covers the realistic
    cases without pretending to be a phone-number library.
    """
    digits = "".join(ch for ch in phone if ch.isdigit())
    candidates = [digits]
    if len(digits) > 10:
        candidates.append(digits[-10:])
    # De-duplicate while preserving order.
    seen: list[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.append(c)
    if not seen:
        raise ValidationError("Phone number must contain at least one digit.")
    return seen


def _clamp(value: int, low: int, high: int, name: str) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} must be an integer.") from exc
    if value < low:
        raise ValidationError(f"{name} must be >= {low} (got {value}).")
    if value > high:
        raise ValidationError(
            f"{name} must be <= {high} (got {value}). Larger values are refused to keep the agent's "
            "context window and the merchant's shared rate-limit budget healthy.",
            agent_hint=f"Call again with {name}<={high} and paginate with the returned cursor.",
        )
    return value


def summarise_window(days: int) -> str:
    """ISO timestamp helper used by the demo workflows and docs."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
