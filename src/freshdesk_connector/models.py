"""Domain models and projections.

Three jobs live here:

1. **Translate** between the labels a human or LLM uses ("urgent", "open") and the
   numeric enums Freshdesk's API actually wants (``priority:4``, ``status:2``).
2. **Project** raw Freshdesk JSON into token-lean, agent-shaped records. A raw
   ticket payload with ``includes`` embedded can easily exceed 3 KB; the compact
   projection is typically 300-600 bytes, which is the difference between fitting
   20 tickets in a context window and fitting 3.
3. **Build** Freshdesk search queries from structured arguments, so the model
   never hand-writes the query mini-language (and so we can enforce the
   512-character query limit and account-specific field-name rules at the edge).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .errors import ValidationError

# --------------------------------------------------------------------------- #
# Enum maps (Freshdesk API v2)
# --------------------------------------------------------------------------- #

STATUS_MAP: dict[int, str] = {
    2: "open",
    3: "pending",
    4: "resolved",
    5: "closed",
    6: "waiting_on_customer",
    7: "waiting_on_third_party",
}
STATUS_IDS: dict[str, int] = {v: k for k, v in STATUS_MAP.items()}
# Convenience aliases an LLM is likely to emit.
STATUS_IDS.update({"waiting on customer": 6, "waiting on third party": 7, "awaiting_customer": 6})

PRIORITY_MAP = {1: "low", 2: "medium", 3: "high", 4: "urgent"}
PRIORITY_IDS: dict[str, int] = {v: k for k, v in PRIORITY_MAP.items()}

SOURCE_MAP = {
    1: "email",
    2: "portal",
    3: "phone",
    4: "forum",
    5: "twitter",
    6: "facebook",
    7: "chat",
    8: "mobihelp",
    9: "feedback_widget",
    10: "outbound_email",
    11: "ecommerce",
}
SOURCE_IDS: dict[str, int] = {v: k for k, v in SOURCE_MAP.items()}

# Freshdesk's per-response cap on the "list all tickets" endpoint.
MAX_PER_PAGE = 100
# The filter/search endpoint returns a fixed 30 per page and refuses page > 10.
SEARCH_PER_PAGE = 30
SEARCH_MAX_PAGE = 10
SEARCH_QUERY_MAX_CHARS = 512
LIST_MAX_PAGE = 300

# Substrings that leak credentials or noise into agent context.
SCRUB_KEYS = {"api_key", "password", "access_token", "refresh_token", "authorization"}


def status_label(value: Any) -> str:
    try:
        return STATUS_MAP.get(int(value), f"custom_status_{value}")
    except (TypeError, ValueError):
        return "unknown"


def priority_label(value: Any) -> str:
    try:
        return PRIORITY_MAP.get(int(value), f"priority_{value}")
    except (TypeError, ValueError):
        return "unknown"


def source_label(value: Any) -> str:
    try:
        return SOURCE_MAP.get(int(value), f"source_{value}")
    except (TypeError, ValueError):
        return "unknown"


def resolve_status(value: str | int) -> int:
    if isinstance(value, int):
        return value
    key = str(value).strip().lower().replace(" ", "_")
    if key.isdigit():
        return int(key)
    if key in STATUS_IDS:
        return STATUS_IDS[key]
    raise ValidationError(
        f"Unknown ticket status {value!r}.",
        details={"allowed": sorted(set(STATUS_IDS) - {"waiting on customer", "waiting on third party"})},
    )


def resolve_priority(value: str | int) -> int:
    if isinstance(value, int):
        return value
    key = str(value).strip().lower()
    if key.isdigit():
        return int(key)
    if key in PRIORITY_IDS:
        return PRIORITY_IDS[key]
    raise ValidationError(f"Unknown priority {value!r}.", details={"allowed": sorted(PRIORITY_IDS)})


def resolve_source(value: str | int) -> int:
    if isinstance(value, int):
        return value
    key = str(value).strip().lower()
    if key.isdigit():
        return int(key)
    if key in SOURCE_IDS:
        return SOURCE_IDS[key]
    raise ValidationError(f"Unknown source {value!r}.", details={"allowed": sorted(SOURCE_IDS)})


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

_PUBLIC_NOTE_HINT = "internal"


def _parse_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _clip(text: Any, limit: int) -> str | None:
    if text is None:
        return None
    s = str(text).strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


def _scrub(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k.lower() not in SCRUB_KEYS}


def _custom_fields(ticket: Mapping[str, Any], limit: int = 12) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in ticket.items():
        if not isinstance(key, str) or not key.startswith("cf_"):
            continue
        if value in (None, "", []):
            continue
        out[key[3:]] = value
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# Projections
# --------------------------------------------------------------------------- #


def compact_ticket(
    ticket: Mapping[str, Any],
    *,
    portal_url: str,
    include_description: bool = False,
    description_limit: int = 240,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Agent-facing ticket. ``raw`` is available when a caller really needs it."""
    now = now or datetime.now(timezone.utc)
    due_by = ticket.get("due_by")
    stats = ticket.get("stats") or {}
    resolved_at = stats.get("resolved_at") or stats.get("closed_at")

    breached: bool | None = None
    due_dt = _parse_ts(due_by)
    if due_dt:
        end = _parse_ts(resolved_at) or now
        breached = end > due_dt

    out: dict[str, Any] = {
        "id": ticket.get("id"),
        "subject": _clip(ticket.get("subject"), 200),
        "status": status_label(ticket.get("status")),
        "status_id": ticket.get("status"),
        "priority": priority_label(ticket.get("priority")),
        "priority_id": ticket.get("priority"),
        "source": source_label(ticket.get("source")),
        "type": ticket.get("type"),
        "tags": ticket.get("tags") or [],
        "requester_id": ticket.get("requester_id"),
        "responder_id": ticket.get("responder_id"),
        "group_id": ticket.get("group_id"),
        "company_id": ticket.get("company_id"),
        "created_at": ticket.get("created_at"),
        "updated_at": ticket.get("updated_at"),
        "due_by": due_by,
        "sla_breached": breached,
        "is_escalated": bool(ticket.get("fr_escalated") or ticket.get("is_escalated")),
        "custom_fields": _custom_fields(ticket),
        "url": f"{portal_url}/a/tickets/{ticket.get('id')}",
    }
    if include_description:
        body = ticket.get("description_text") or ticket.get("description")
        out["description_excerpt"] = _clip(body, description_limit)
    # Nested requesters/companies are only present when `include` was used.
    if isinstance(ticket.get("requester"), Mapping):
        out["requester"] = compact_contact(ticket["requester"])
    if isinstance(ticket.get("stats"), Mapping):
        out["timeline"] = {
            k: stats.get(k)
            for k in ("first_responded_at", "requester_responded_at", "resolved_at", "closed_at", "pending_since")
            if stats.get(k)
        }
    return _scrub(out)


def compact_contact(contact: Mapping[str, Any]) -> dict[str, Any]:
    return _scrub(
        {
            "id": contact.get("id"),
            "name": contact.get("name"),
            "email": contact.get("email"),
            "phone": contact.get("phone"),
            "mobile": contact.get("mobile"),
            "job_title": contact.get("job_title"),
            "company_id": contact.get("company_id"),
            "tags": contact.get("tags") or [],
            "created_at": contact.get("created_at"),
            "updated_at": contact.get("updated_at"),
            "other_company_ids": contact.get("other_company_ids") or [],
        }
    )


def compact_company(company: Mapping[str, Any]) -> dict[str, Any]:
    return _scrub(
        {
            "id": company.get("id"),
            "name": company.get("name"),
            "domains": company.get("domains") or [],
            "health_score": company.get("health_score"),
            "account_tier": company.get("account_tier"),
            "industry": company.get("industry"),
            "created_at": company.get("created_at"),
        }
    )


def compact_agent(agent: Mapping[str, Any]) -> dict[str, Any]:
    return _scrub(
        {
            "id": agent.get("id"),
            "name": agent.get("contact", {}).get("name") if isinstance(agent.get("contact"), Mapping) else None,
            "email": agent.get("contact", {}).get("email") if isinstance(agent.get("contact"), Mapping) else None,
            "available": agent.get("available"),
            "occasional": agent.get("occasional"),
            "group_ids": agent.get("group_ids") or [],
            "ticket_scope": agent.get("ticket_scope"),
        }
    )


def compact_group(group: Mapping[str, Any]) -> dict[str, Any]:
    return _scrub(
        {
            "id": group.get("id"),
            "name": group.get("name"),
            "description": _clip(group.get("description"), 160),
            "agent_ids": group.get("agent_ids") or [],
            "escalate_to": group.get("escalate_to"),
            "business_hour_id": group.get("business_hour_id"),
        }
    )


def compact_conversation(conv: Mapping[str, Any]) -> dict[str, Any]:
    """Conversation projection.

    ``visibility`` is the important field: Freshdesk conversations include *private
    notes* that customers cannot see. Labelling them explicitly is a guardrail -
    an agent that quotes a private note back to a customer has leaked internal
    content.
    """
    is_private = bool(conv.get("private"))
    return _scrub(
        {
            "id": conv.get("id"),
            "ticket_id": conv.get("ticket_id"),
            "user_id": conv.get("user_id"),
            "direction": "outbound" if conv.get("incoming") is False else "inbound",
            "visibility": "internal" if is_private else "public",
            "customer_visible": not is_private,
            "body_excerpt": _clip(conv.get("body_text") or conv.get("body"), 500),
            "attachments": len(conv.get("attachments") or []),
            "created_at": conv.get("created_at"),
            "updated_at": conv.get("updated_at"),
        }
    )


def ticket_field_summary(field: Mapping[str, Any]) -> dict[str, Any]:
    """Condensed ticket-field metadata used to teach the agent valid filters.

    Custom fields are addressed as ``cf_<name>`` in filters, but Freshdesk already
    prefixes custom field names with ``cf_``, so the prefix must not be doubled.
    """
    name = str(field.get("name") or "")
    if name.startswith("cf_"):
        expression = name
    elif field.get("default") is False:
        expression = f"cf_{name}"
    else:
        expression = name
    choices = field.get("choices") or {}
    return _scrub(
        {
            "name": name,
            "label": field.get("label"),
            "type": field.get("type"),
            "searchable_expression": expression,
            "choices": choices if isinstance(choices, Mapping) and choices else None,
        }
    )


# --------------------------------------------------------------------------- #
# Constrained search-query builder
# --------------------------------------------------------------------------- #

#: Fields the connector will happily build filters for, with their value type.
#: Deliberately a whitelist: Freshdesk field names are account-specific and
#: case-sensitive, and an unknown field produces an opaque HTTP 400.
TICKET_FILTER_FIELDS: dict[str, str] = {
    "status": "status",
    "priority": "priority",
    "source": "source",
    "type": "string",
    "agent_id": "id",
    "responder_id": "id",
    "group_id": "id",
    "requester_id": "id",
    "company_id": "id",
    "product_id": "id",
    "tag": "string",
    "created_at": "date",
    "updated_at": "date",
    "due_by": "date",
    "fr_due_by": "date",
    "closed_at": "date",
    "resolved_at": "date",
    "is_escalated": "bool",
    "fr_escalated": "bool",
    "spam": "bool",
}

#: Sentinel accepted instead of an id to mean "unassigned".
NULL_SENTINELS = {"null", "none", "unassigned", "no_agent", "-1"}


def _quote(value: str) -> str:
    if "'" in value or '"' in value:
        raise ValidationError(
            f"Filter value {value!r} contains a quote character, which Freshdesk's query "
            "mini-language cannot escape.",
            details={"field": "value"},
        )
    return f"'{value}'"


def _render_condition(field: str, value: Any, op: str = ":") -> str:
    if field.startswith("cf_"):
        kind = "string"
    elif field in TICKET_FILTER_FIELDS:
        kind = TICKET_FILTER_FIELDS[field]
    else:
        raise ValidationError(
            f"{field!r} is not a filterable Freshdesk ticket field.",
            details={
                "field": field,
                "allowed": sorted(TICKET_FILTER_FIELDS) + ["cf_<your_custom_field>"],
                "hint": "Call freshdesk_get_ticket_fields to list this account's real field names.",
            },
        )

    if isinstance(value, str) and value.strip().lower() in NULL_SENTINELS:
        return f"{field}:null"

    if kind == "status":
        return f"{field}:{resolve_status(value)}"
    if kind == "priority":
        return f"{field}:{resolve_priority(value)}"
    if kind == "source":
        return f"{field}:{resolve_source(value)}"
    if kind == "id":
        try:
            return f"{field}:{int(value)}"
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{field} must be a numeric id or 'null' (got {value!r}).") from exc
    if kind == "bool":
        if isinstance(value, bool):
            return f"{field}:{'true' if value else 'false'}"
        text = str(value).strip().lower()
        if text not in {"true", "false"}:
            raise ValidationError(f"{field} must be true or false (got {value!r}).")
        return f"{field}:{text}"
    if kind == "date":
        text = str(value)
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(
                f"{field} must be an ISO date (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ); got {value!r}.",
            ) from exc
        # Freshdesk's grammar is `field:>'2026-09-01'` - the colon is mandatory
        # even when a comparison operator follows it.
        return f"{field}:{op}{_quote(text)}"
    # generic string / custom field: numbers are unquoted, everything else quoted
    if isinstance(value, bool):
        return f"{field}:{'true' if value else 'false'}"
    if isinstance(value, (int, float)):
        return f"{field}:{value}"
    return f"{field}:{_quote(str(value))}"


def _group(field: str, values: Sequence[Any]) -> str:
    parts = [_render_condition(field, v) for v in values]
    return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"


def build_ticket_query(
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
    extra_expression: str | None = None,
    joiner: str = "AND",
) -> str:
    """Build a Freshdesk ticket-search query string.

    Returns e.g. ``"((status:2 OR status:3) AND priority:>2 AND created_at:>'2026-09-01')"``.
    The result is URL-encoded by the HTTP layer; callers get the raw expression so
    it can also be shown to the agent for verification.
    """
    clauses: list[str] = []

    def _seq(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
            return [value]
        return list(value)

    for field, value in (("status", status), ("priority", priority), ("source", source), ("tag", tag)):
        values = _seq(value)
        if values:
            clauses.append(_group(field, values))

    if type:
        clauses.append(_render_condition("type", type))

    for field, value in (
        ("agent_id", agent_id),
        ("responder_id", responder_id),
        ("group_id", group_id),
        ("requester_id", requester_id),
        ("company_id", company_id),
    ):
        if value is not None:
            clauses.append(_render_condition(field, value))

    if include_unassigned_agent and agent_id is None:
        clauses.append("agent_id:null")

    if priority_at_least is not None:
        # Deliberately *not* `priority:>N`. Freshworks' own docs are inconsistent
        # about whether the comparison is inclusive, and getting it wrong silently
        # drops high-priority tickets from an SLA report. An explicit OR list of
        # enum values is unambiguous.
        floor = resolve_priority(priority_at_least)
        clauses.append(_group("priority", [p for p in (1, 2, 3, 4) if p >= floor]))

    for field, value, op in (
        ("created_at", created_after, ">"),
        ("created_at", created_before, "<"),
        ("updated_at", updated_after, ">"),
        ("updated_at", updated_before, "<"),
        ("due_by", due_before, "<"),
    ):
        if value is not None:
            clauses.append(_render_condition(field, value, op))

    for name, value in (custom_fields or {}).items():
        key = name if name.startswith("cf_") else f"cf_{name}"
        clauses.append(_render_condition(key, value))

    if extra_expression:
        clauses.append(f"({extra_expression.strip()})")

    if not clauses:
        raise ValidationError(
            "Refusing to run an unbounded ticket search: supply at least one filter "
            "(for example status, priority, or a created_after date).",
            details={"hint": "Use freshdesk_list_tickets for a plain browsable listing with pagination."},
        )

    query = f" {joiner} ".join(clauses)
    if len(query) > SEARCH_QUERY_MAX_CHARS:
        raise ValidationError(
            f"The generated Freshdesk filter is {len(query)} characters; the API caps queries at "
            f"{SEARCH_QUERY_MAX_CHARS}.",
            details={"generated_query": query, "hint": "Split the search into smaller date ranges."},
        )
    return query


def describe_query(query: str) -> str:
    """Human-readable echo so an agent can sanity-check what it asked for."""
    return query.replace(":", " ").replace("'", "")


def normalize_records(payload: Any) -> list[dict[str, Any]]:
    """Freshdesk list endpoints return a bare JSON array; guard against dicts."""
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []
