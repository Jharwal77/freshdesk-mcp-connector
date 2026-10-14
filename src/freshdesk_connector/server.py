"""MCP server: exposes the connector to an Agent Studio agent.

Tool-design notes
-----------------
* **Read-only by default.** Thirteen read tools are registered unconditionally.
  One write tool (``freshdesk_add_private_note``) is registered only when
  ``FRESHDESK_ENABLE_WRITES=true``. An agent that cannot write cannot leak.
* **Discovery before query.** ``freshdesk_get_ticket_fields`` and
  ``freshdesk_list_groups`` exist because Freshdesk's filter field names are
  account-specific and searches fail opaquely without them.
* **Honest results.** Every tool returns ``warnings`` and a ``pagination`` block
  describing exactly what it did *not* see, because a connector that quietly
  returns a partial answer is worse than one that fails.
* **Errors are data.** Failures return ``{"ok": false, "error": {...}}`` with a
  ``retryable`` flag and an ``agent_hint`` instead of raising, so the agent can
  choose to retry, narrow, or escalate.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Callable, Mapping, Sequence

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .client import FreshdeskClient
from .config import Config
from .errors import FreshdeskError
from .service import FreshdeskService, fail

log = logging.getLogger("freshdesk_connector.server")

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
ADDITIVE_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)

SERVER_INSTRUCTIONS = """
This connector gives you read access to a merchant's Freshdesk helpdesk: tickets,
conversations, contacts, companies, agents and groups. It is read-only unless the
operator explicitly enabled a single guarded write tool.

Work in this order:
1. `freshdesk_connector_health` if you have any doubt the credentials work.
2. `freshdesk_get_ticket_fields` + `freshdesk_list_groups` to learn this account's
   real filter field names and routing teams. Field names are account-specific and
   case-sensitive; guessing one produces an opaque HTTP 400.
3. Then search or list.

Three limits will silently corrupt an answer if you ignore them. Every tool result
states which one applies:
* The ticket listing endpoint returns only tickets *created in the last 30 days*
  unless you pass `updated_since` (hard ceiling: 300 pages / 30,000 tickets).
* The search endpoint filters structured fields only - there is **no full-text
  search** - returns a fixed 30 per page, and refuses to go past page 10, so a
  single query can see at most 300 tickets.
* Freshdesk rate limits are **per merchant account and shared with every other
  integration**. Call `freshdesk_rate_limit_status` before a large scan, and prefer
  narrow filters over wide scans.

Never present a partial or truncated result as complete: check `pagination.truncated`
and `warnings` and say so in your answer. Never quote an entry with
`visibility=internal` to a customer; those are private agent notes.
""".strip()


class ConnectorRuntime:
    """Owns the lazily-created HTTP client so tools share one connection pool."""

    def __init__(self, config: Config, *, transport: Any = None) -> None:
        self.config = config
        self._transport = transport
        self._client: FreshdeskClient | None = None
        self._service: FreshdeskService | None = None
        self._lock = asyncio.Lock()

    async def service(self) -> FreshdeskService:
        async with self._lock:
            if self._client is None:
                self._client = await FreshdeskClient(self.config, transport=self._transport).__aenter__()
                self._service = FreshdeskService(self._client, self.config)
            assert self._service is not None
            return self._service

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            self._service = None


def _guarded(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Convert exceptions into the connector's structured error envelope."""

    @functools.wraps(fn)
    async def wrapper(self: "ConnectorTools", *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return await fn(self, *args, **kwargs)
        except FreshdeskError as exc:
            log.warning("tool %s failed: %s", fn.__name__, exc.code)
            return fail(exc)
        except (ValueError, TypeError) as exc:
            return fail(
                FreshdeskError(
                    f"Invalid arguments: {exc}",
                    details={"tool": fn.__name__},
                    agent_hint="Fix the arguments and retry once; do not repeat the identical call.",
                )
            )
        except Exception as exc:  # noqa: BLE001 - never leak a raw traceback to the model
            log.exception("unexpected failure in %s", fn.__name__)
            return fail(
                FreshdeskError(
                    f"Unexpected connector failure in {fn.__name__}: {type(exc).__name__}",
                    agent_hint=(
                        "This is a connector bug, not a data problem. Report it and include the tool name; "
                        "consider falling back to a narrower query."
                    ),
                )
            )

    return wrapper


class ConnectorTools:
    """Thin, fully-specified wrappers over :class:`FreshdeskService`."""

    def __init__(self, runtime: ConnectorRuntime) -> None:
        self.runtime = runtime

    # --------------------------------------------------------------------- ops
    @_guarded
    async def connector_health(self) -> dict[str, Any]:
        """Check that the connector can authenticate and reach Freshdesk.

        Use when: a previous call failed oddly, or at the start of a session on a
        new account. Returns authentication status, round-trip latency, whether
        writes are enabled, and the domain in use. Costs one API call.
        """
        return await (await self.runtime.service()).connector_health()

    @_guarded
    async def rate_limit_status(self) -> dict[str, Any]:
        """Report this connector's live view of the shared Freshdesk rate budget.

        Use when: about to run a multi-page scan, or after a 429. Returns the
        effective per-minute ceiling, how much of the window is spent, per-endpoint
        sub-limits, and any recent throttling. Costs zero API calls.
        """
        return await (await self.runtime.service()).rate_limit_status()

    # ----------------------------------------------------------------- tickets
    @_guarded
    async def list_tickets(
        self,
        updated_since: str | None = None,
        predefined_filter: str | None = None,
        requester_id: int | None = None,
        company_id: int | None = None,
        order_by: str = "updated_at",
        order_type: str = "desc",
        include_description: bool = False,
        limit: int = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Browse tickets chronologically, newest first by default.

        Use when: you need an ordered sweep of recent tickets, a broad "what came in
        today" view, or when filters are unknown.
        Avoid when: you know what you are looking for - `freshdesk_search_tickets`
        is cheaper and can reach older tickets.

        Caveats returned in the result: without `updated_since` Freshdesk returns
        only tickets *created in the last 30 days*; `include_description` costs 3x
        the rate-limit budget because Freshdesk charges extra for `include=`.

        Args:
            updated_since: ISO date/timestamp. Widen beyond the 30-day default window.
            predefined_filter: one of all_tickets, new_and_my_open, watching, spam, deleted.
            limit: max records to return this call (1-100). Exactly one API call is
                spent per call, so raise `limit` rather than paging for small walks.
            cursor: pass `pagination.next_cursor` from a previous call to continue
                exactly where it stopped.
        """
        return await (await self.runtime.service()).list_tickets(
            updated_since=updated_since,
            predefined_filter=predefined_filter,
            requester_id=requester_id,
            company_id=company_id,
            order_by=order_by,
            order_type=order_type,
            include_description=include_description,
            limit=limit,
            cursor=cursor,
        )

    @_guarded
    async def get_ticket(
        self,
        ticket_id: int,
        include_conversations: bool = True,
        include_company: bool = False,
        conversation_limit: int = 10,
    ) -> dict[str, Any]:
        """Fetch one ticket by id, optionally with its full conversation timeline.

        Use when: you have a ticket id and need the whole picture - description,
        SLA clock, timeline, and the replies and private notes.
        Conversations are labelled `visibility: internal | public`; internal entries
        are private agent notes and must never be quoted to a customer.
        """
        return await (await self.runtime.service()).get_ticket(
            int(ticket_id),
            include_conversations=include_conversations,
            include_company=include_company,
            conversation_limit=conversation_limit,
        )

    @_guarded
    async def search_tickets(
        self,
        status: str | Sequence[str] | None = None,
        priority: str | Sequence[str] | None = None,
        priority_at_least: str | None = None,
        source: str | None = None,
        type: str | None = None,
        agent_id: int | str | None = None,
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
        limit: int = 25,
        max_pages: int = 1,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Search tickets by structured fields. This is the workhorse.

        Use when: "open urgent tickets", "tickets in the Settlements group",
        "everything tagged autopay created this month", "unassigned tickets",
        "tickets for this order id".

        Friendly labels are translated to Freshdesk's numeric enums for you:
        status accepts open / pending / resolved / closed / waiting_on_customer /
        waiting_on_third_party; priority accepts low / medium / high / urgent.
        Pass lists to build an OR (status=["open","pending"]).

        Caveats (also returned in the result): no full-text search - structured
        fields only; 30 records per page; page 10 is the hard ceiling, so at most
        300 tickets are reachable per query. Split wide queries by date range.

        Args:
            priority_at_least: e.g. "high" means high or urgent.
            due_before: ISO date - the standard way to find SLA-risk tickets.
            custom_fields: {"cf_order_id": "order_123"} - call
                `freshdesk_get_ticket_fields` first for this account's field names.
            include_unassigned_agent: only tickets with no agent assigned.
            cursor: continue a previous search; the cursor is bound to the filter set.
        """
        return await (await self.runtime.service()).search_tickets(
            status=status,
            priority=priority,
            priority_at_least=priority_at_least,
            source=source,
            type=type,
            agent_id=agent_id,
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
            limit=limit,
            max_pages=max_pages,
            cursor=cursor,
        )

    @_guarded
    async def search_tickets_by_text(
        self,
        text: str,
        updated_since: str | None = None,
        include_descriptions: bool = False,
        limit: int = 20,
        max_pages: int = 3,
    ) -> dict[str, Any]:
        """Find tickets containing a keyword, using a bounded client-side scan.

        Use when: you only have free text - a customer's name, an order id inside
        the body, a phrase from a complaint - and need candidate tickets.
        Avoid when: structured filters would do; a scan costs one API call per page
        from the shared account budget.

        This exists because Freshdesk has no full-text search. The result always
        states how many tickets were examined and whether the scan was complete. If
        `pagination.scan_complete` is false, matches may exist that were not seen -
        say so rather than answering as if you searched everything.

        Args:
            text: at least 3 characters, matched case-insensitively.
            updated_since: narrow the scan window (strongly recommended).
            include_descriptions: also scan description bodies; costs 3x budget per page.
            max_pages: scan depth ceiling (1-10).
        """
        return await (await self.runtime.service()).search_tickets_by_text(
            text,
            updated_since=updated_since,
            include_descriptions=include_descriptions,
            limit=limit,
            max_pages=max_pages,
        )

    @_guarded
    async def list_ticket_conversations(
        self, ticket_id: int, limit: int = 20, include_private: bool = True
    ) -> dict[str, Any]:
        """List the reply/note timeline of one ticket, oldest first.

        Use when: you need the history - what the agent said, what the customer
        said, and what was noted internally. Entries carry
        `visibility: internal | public`; set `include_private=False` to see only
        what the customer can see, which is the right view for drafting a reply.
        """
        return await (await self.runtime.service()).list_ticket_conversations(
            int(ticket_id), limit=limit, include_private=include_private
        )

    # ---------------------------------------------------------------- contacts
    @_guarded
    async def get_contact(
        self, contact_id: int | None = None, email: str | None = None, phone: str | None = None
    ) -> dict[str, Any]:
        """Look up one customer contact by id, email or phone.

        Use when: you have an identifier from a ticket, an email thread or a caller.
        Provide exactly one. Email is resolved server-side; phone falls back to a
        search because Freshdesk does not index phones on the list endpoint.
        """
        return await (await self.runtime.service()).get_contact(contact_id=contact_id, email=email, phone=phone)

    @_guarded
    async def search_contacts(
        self,
        email: str | None = None,
        phone: str | None = None,
        name: str | None = None,
        company_id: int | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Find contacts by email, phone, name or company.

        Use when: you need the customer behind a ticket, or every contact at an
        account. Requires at least one filter - unfiltered contact listing is
        expensive and is refused. Matching is exact-ish (Freshdesk's contact search
        is not fuzzy), so partial names may return nothing.
        """
        return await (await self.runtime.service()).search_contacts(
            email=email, phone=phone, name=name, company_id=company_id, limit=limit
        )

    @_guarded
    async def get_company(self, company_id: int, include_contacts: bool = False) -> dict[str, Any]:
        """Fetch one company (account) record, optionally with its contacts.

        Use when: you need account tier, health score or industry to judge
        severity, or you are assembling a 360 view of a business customer.
        """
        return await (await self.runtime.service()).get_company(int(company_id), include_contacts=include_contacts)

    # --------------------------------------------------------------- discovery
    @_guarded
    async def list_agents(self, group_id: int | None = None, limit: int = 100) -> dict[str, Any]:
        """List support agents, optionally filtered to one group.

        Use when: you need a human-readable name for an `agent_id` / `responder_id`,
        or you are deciding who owns a ticket. Combine with `freshdesk_list_groups`
        to map routing teams.
        """
        return await (await self.runtime.service()).list_agents(group_id=group_id, limit=limit)

    @_guarded
    async def list_groups(self) -> dict[str, Any]:
        """List routing groups (Payments Support, Settlements, Escalations, ...).

        Use when: before filtering by group_id. Group ids are account-specific, and
        an agent that guesses one will silently return the wrong team's tickets.
        """
        return await (await self.runtime.service()).list_groups()

    @_guarded
    async def get_ticket_fields(self) -> dict[str, Any]:
        """List this account's ticket fields, including custom fields.

        Use when: before any search that touches a custom field, or after an HTTP
        400 from `freshdesk_search_tickets`. Field names are account-specific and
        case-sensitive; the returned `filter_field_names` are exactly what the
        search tool accepts in `custom_fields`.
        """
        return await (await self.runtime.service()).get_ticket_fields()

    # ------------------------------------------------------------------- write
    @_guarded
    async def add_private_note(
        self, ticket_id: int, body: str, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Add an internal (private) note to a ticket. Human approval required.

        Only registered when the operator sets FRESHDESK_ENABLE_WRITES=true, and
        even then it writes a note the customer cannot see - never a public reply.
        Ask the user to confirm the text before calling, and pass a stable
        `idempotency_key` so a retry cannot double-post (Freshdesk has no native
        idempotency support).
        """
        return await (await self.runtime.service()).add_private_note(
            int(ticket_id), body, idempotency_key=idempotency_key
        )


def _register(server: MCPServer, tools: ConnectorTools, *, writes: bool) -> None:
    read_specs = [
        ("freshdesk_connector_health", tools.connector_health),
        ("freshdesk_rate_limit_status", tools.rate_limit_status),
        ("freshdesk_list_tickets", tools.list_tickets),
        ("freshdesk_get_ticket", tools.get_ticket),
        ("freshdesk_search_tickets", tools.search_tickets),
        ("freshdesk_search_tickets_by_text", tools.search_tickets_by_text),
        ("freshdesk_list_ticket_conversations", tools.list_ticket_conversations),
        ("freshdesk_get_contact", tools.get_contact),
        ("freshdesk_search_contacts", tools.search_contacts),
        ("freshdesk_get_company", tools.get_company),
        ("freshdesk_list_agents", tools.list_agents),
        ("freshdesk_list_groups", tools.list_groups),
        ("freshdesk_get_ticket_fields", tools.get_ticket_fields),
    ]
    for name, fn in read_specs:
        server.add_tool(fn, name=name, annotations=READ_ONLY)
    if writes:
        server.add_tool(tools.add_private_note, name="freshdesk_add_private_note", annotations=ADDITIVE_WRITE)


def create_server(
    config: Config | None = None,
    *,
    runtime: ConnectorRuntime | None = None,
    name: str = "freshdesk-connector",
) -> MCPServer:
    """Build the MCP server. Pass ``runtime`` to inject a preconfigured client."""
    config = config or Config.from_env()
    runtime = runtime or ConnectorRuntime(config)
    tools = ConnectorTools(runtime)
    server = MCPServer(
        name=name,
        version="0.1.0",
        instructions=SERVER_INSTRUCTIONS,
    )
    _register(server, tools, writes=config.enable_writes)
    return server


def main() -> None:  # pragma: no cover - entry point used by MCP clients
    """Run over stdio, which is how Claude Desktop / Cursor / Agent Studio launch it."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    create_server().run("stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
