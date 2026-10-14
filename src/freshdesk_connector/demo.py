"""Three merchant workflows, end to end, using only the connector's own tools.

These are the jobs a forward-deployed engineer actually gets asked for on a
payments platform's support desk:

1. **SLA-risk triage** - which open tickets are already past (or about to pass)
   their first-response/due-by clock, and which team owns them?
2. **Customer 360** - a customer is on the phone; what is the full context?
3. **Theme scan** - a merchant says "autopay is broken"; how many tickets does
   that theme actually cover, and how are they distributed?

Each function is deterministic given the same helpdesk state and returns a plain
dict, so it can be asserted on in tests or printed as JSON in a demo.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .errors import FreshdeskError
from .service import FreshdeskService


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _hours_until(value: Any, now: datetime) -> float | None:
    dt = _parse(value)
    return None if dt is None else round((dt - now).total_seconds() / 3600, 1)


async def sla_risk_triage(
    service: FreshdeskService,
    *,
    horizon_hours: int = 12,
    limit: int = 50,
    exclude_groups: set[int] | None = None,
) -> dict[str, Any]:
    """Open/pending tickets at or near their SLA due-by, with owners attached.

    Uses one structured search + one metadata lookup (groups), so it is cheap
    enough to run on a schedule - which is how it would be deployed.
    """
    now = datetime.now(timezone.utc)
    search = await service.search_tickets(
        status=["open", "pending", "waiting_on_customer", "waiting_on_third_party"],
        priority_at_least="high",
        limit=limit,
        max_pages=2,
    )
    if not search.get("ok"):
        return {"workflow": "sla_risk_triage", "ok": False, "error": search["error"]}

    tickets = search["data"]
    at_risk = []
    for ticket in tickets:
        hours_left = _hours_until(ticket.get("due_by"), now)
        if hours_left is None or hours_left > horizon_hours:
            continue
        if exclude_groups and ticket.get("group_id") in exclude_groups:
            continue
        at_risk.append(
            {
                "id": ticket["id"],
                "subject": ticket["subject"],
                "priority": ticket["priority"],
                "status": ticket["status"],
                "group_id": ticket.get("group_id"),
                "responder_id": ticket.get("responder_id"),
                "due_by": ticket.get("due_by"),
                "hours_to_due": hours_left,
                "already_breached": hours_left < 0,
                "url": ticket.get("url"),
            }
        )

    groups = await service.list_groups()
    group_names = {g["id"]: g["name"] for g in groups.get("data", [])} if groups.get("ok") else {}
    agents = await service.list_agents()
    agent_names = {a["id"]: a.get("name") for a in agents.get("data", [])} if agents.get("ok") else {}

    for item in at_risk:
        item["group_name"] = group_names.get(item.get("group_id"), "Unassigned")
        item["agent_name"] = agent_names.get(item.get("responder_id")) or "Unassigned"
    at_risk.sort(key=lambda t: t["hours_to_due"] if t["hours_to_due"] is not None else 1e9)

    by_group: dict[str, int] = {}
    for item in at_risk:
        by_group[item["group_name"]] = by_group.get(item["group_name"], 0) + 1

    return {
        "workflow": "sla_risk_triage",
        "ok": True,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "horizon_hours": horizon_hours,
        "candidates_examined": len(tickets),
        "at_risk_count": len(at_risk),
        "already_breached": sum(1 for t in at_risk if t["already_breached"]),
        "by_group": by_group,
        "tickets": at_risk,
        "caveats": search.get("warnings", []),
        "next_actions": [
            "Escalate breached tickets first; the connector returns private-note guidance if writes are enabled.",
            "If at_risk_count == limit, the search ceiling may be hiding tickets: split by group_id.",
        ],
    }


async def customer_360(service: FreshdeskService, *, email: str, ticket_limit: int = 10) -> dict[str, Any]:
    """Assemble everything the desk knows about one customer, in three calls."""
    contact = await service.get_contact(email=email)
    if not contact.get("ok"):
        return {"workflow": "customer_360", "ok": False, "identifier": email, "error": contact["error"]}

    person = contact["data"]
    company = None
    if person.get("company_id"):
        company_result = await service.get_company(int(person["company_id"]))
        if company_result.get("ok"):
            company = company_result["data"]["company"]

    tickets = await service.search_tickets(requester_id=int(person["id"]), limit=ticket_limit, max_pages=1)
    recent = tickets.get("data", []) if tickets.get("ok") else []

    open_tickets = [t for t in recent if t.get("status") in {"open", "pending", "waiting_on_customer", "waiting_on_third_party"}]
    latest = None
    if recent:
        newest = max(recent, key=lambda t: str(t.get("updated_at") or ""))
        conv = await service.get_ticket(int(newest["id"]), include_conversations=True, conversation_limit=5)
        if conv.get("ok"):
            public = [c for c in conv["data"].get("conversations", []) if c.get("customer_visible")]
            latest = {
                "ticket_id": newest["id"],
                "subject": newest["subject"],
                "last_public_message": public[-1]["body_excerpt"] if public else None,
                "internal_notes_hidden": sum(
                    1 for c in conv["data"].get("conversations", []) if c.get("visibility") == "internal"
                ),
            }

    return {
        "workflow": "customer_360",
        "ok": True,
        "identifier": email,
        "contact": person,
        "company": company,
        "ticket_summary": {
            "recent_returned": len(recent),
            "open": len(open_tickets),
            "open_tickets": [
                {"id": t["id"], "subject": t["subject"], "priority": t["priority"], "status": t["status"]}
                for t in open_tickets
            ],
        },
        "latest_context": latest,
        "caveats": (tickets.get("warnings", []) if tickets.get("ok") else [])
        + ["Private notes were counted but their text is not surfaced here - never quote internal content."],
    }


async def theme_scan(
    service: FreshdeskService, *, term: str, updated_since: str, max_pages: int = 3, include_descriptions: bool = True
) -> dict[str, Any]:
    """Quantify how much of the recent ticket stream a free-text theme covers.

    The scan is bounded and says so: ``scan.complete`` tells the caller whether the
    number is a floor or a total.
    """
    scan = await service.search_tickets_by_text(
        term, updated_since=updated_since, include_descriptions=include_descriptions, max_pages=max_pages, limit=60
    )
    if not scan.get("ok"):
        return {"workflow": "theme_scan", "ok": False, "term": term, "error": scan["error"]}

    tickets = scan["data"]
    by_status: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    by_group: dict[str, int] = {}
    breached = 0
    for ticket in tickets:
        by_status[ticket["status"]] = by_status.get(ticket["status"], 0) + 1
        by_priority[ticket["priority"]] = by_priority.get(ticket["priority"], 0) + 1
        by_group[str(ticket.get("group_id"))] = by_group.get(str(ticket.get("group_id")), 0) + 1
        if ticket.get("sla_breached"):
            breached += 1

    return {
        "workflow": "theme_scan",
        "ok": True,
        "term": term,
        "matched": len(tickets),
        "scan": scan.get("pagination"),
        "by_status": by_status,
        "by_priority": by_priority,
        "by_group_id": by_group,
        "sla_breached": breached,
        "sample": [
            {"id": t["id"], "subject": t["subject"], "status": t["status"], "priority": t["priority"]}
            for t in tickets[:5]
        ],
        "caveats": scan.get("warnings", []),
    }


async def run_all(service: FreshdeskService, *, email: str, term: str, updated_since: str) -> dict[str, Any]:
    """Run the three workflows in sequence; used by the CLI demo."""
    results: dict[str, Any] = {}
    for name, coro in (
        ("sla_risk_triage", sla_risk_triage(service)),
        ("customer_360", customer_360(service, email=email)),
        ("theme_scan", theme_scan(service, term=term, updated_since=updated_since)),
    ):
        try:
            results[name] = await coro
        except FreshdeskError as exc:
            results[name] = {"workflow": name, "ok": False, "error": exc.to_dict()["error"]}
    return results
