# Captured demo output

Produced by `python -m freshdesk_connector.cli demo --mock` against the bundled
mock Freshdesk (45 fictional tickets). Re-run it yourself to reproduce; ticket
timestamps are generated relative to the run time, so ids and counts match while
relative times stay meaningful.

_Captured: 2026-10-14_

---

## Workflow 1 — SLA-risk triage

Question: *which open/high-priority tickets are past or near their due-by clock, and who owns them?*

One structured search (open + pending + waiting, high priority or above) plus group/agent
metadata lookups. Cheap enough to run on a schedule.

```json
{
  "workflow": "sla_risk_triage",
  "ok": true,
  "generated_at": "2026-10-14T17:21:14Z",
  "horizon_hours": 12,
  "candidates_examined": 27,
  "at_risk_count": 22,
  "already_breached": 10,
  "by_group": {
    "Payments Support": 11,
    "Settlements & Reconciliation": 8,
    "Escalations": 3
  }
}
```

Top of the returned list:

```json
[
  {
    "id": 10027,
    "subject": "Autopay mandate registration failing for new customers",
    "priority": "urgent",
    "status": "open",
    "group_id": 9001,
    "responder_id": 2001,
    "due_by": "2026-10-13T21:21:14Z",
    "hours_to_due": -20.0,
    "already_breached": true,
    "url": "https://acme.freshdesk.com/a/tickets/10027",
    "group_name": "Payments Support",
    "agent_name": "Priya Nair"
  },
  {
    "id": 10014,
    "subject": "Refund is processing for 12 days, customer disputing",
    "priority": "urgent",
    "status": "open",
    "group_id": 9002,
    "responder_id": 2004,
    "due_by": "2026-10-14T05:21:14Z",
    "hours_to_due": -12.0,
    "already_breached": true,
    "url": "https://acme.freshdesk.com/a/tickets/10014",
    "group_name": "Settlements & Reconciliation",
    "agent_name": "Daniel Fernandes"
  },
  {
    "id": 10020,
    "subject": "Chargeback raised on order ORD-88231",
    "priority": "urgent",
    "status": "open",
    "group_id": 9002,
    "responder_id": 2004,
    "due_by": "2026-10-14T09:21:14Z",
    "hours_to_due": -8.0,
    "already_breached": true,
    "url": "https://acme.freshdesk.com/a/tickets/10020",
    "group_name": "Settlements & Reconciliation",
    "agent_name": "Daniel Fernandes"
  }
]
```

Caveats the connector attached (repeated by the agent in its answer):


---

## Workflow 2 — Customer 360

Question: *a customer is on the phone (`vikram.rao@example.com`); what is the full context?*

Three calls: contact lookup → company → their tickets → latest conversation.

```json
{
  "contact": {
    "id": 3002,
    "name": "Vikram Rao",
    "email": "vikram.rao@example.com",
    "phone": "+91 90000 22222",
    "mobile": "9000022222",
    "job_title": "Finance Manager",
    "company_id": 5001,
    "tags": [],
    "created_at": "2026-06-17T17:21:14Z",
    "updated_at": "2026-10-04T17:21:14Z",
    "other_company_ids": []
  },
  "company": {
    "id": 5001,
    "name": "Vayu Wellness Pvt Ltd",
    "domains": [
      "vayuwellness.example"
    ],
    "health_score": "Happy",
    "account_tier": "Growth",
    "industry": "Health & Wellness",
    "created_at": "2025-12-18T17:21:14Z"
  },
  "ticket_summary": {
    "recent_returned": 5,
    "open": 4
  }
}
```

Open tickets for this contact:

```json
[
  {
    "id": 10003,
    "subject": "Settlement for 30 Sep cycle is short by Rs 12,400",
    "priority": "high",
    "status": "open"
  },
  {
    "id": 10018,
    "subject": "Customer charged in USD instead of INR",
    "priority": "high",
    "status": "open"
  },
  {
    "id": 10021,
    "subject": "Payout account change request pending approval",
    "priority": "high",
    "status": "waiting_on_customer"
  },
  {
    "id": 10043,
    "subject": "Autopay mandate revoked but debits continue",
    "priority": "urgent",
    "status": "open"
  }
]
```

Latest conversation context (internal notes are counted, never quoted):

```json
{
  "ticket_id": 10043,
  "subject": "Autopay mandate revoked but debits continue",
  "last_public_message": "Thanks for reaching out - we are investigating and will update you shortly.",
  "internal_notes_hidden": 1
}
```

---

## Workflow 3 — Theme scan

Question: *how many recent tickets are about `autopay`, and how are they distributed?*

Freshdesk has no full-text search, so this is a **bounded scan** that reports its own coverage:

```json
{
  "matched": 10,
  "scan": {
    "returned": 10,
    "pages_scanned": 1,
    "tickets_examined": 40,
    "scan_complete": true,
    "requested_limit": 60
  },
  "by_status": {
    "open": 6,
    "pending": 3,
    "resolved": 1
  },
  "by_priority": {
    "urgent": 5,
    "high": 2,
    "medium": 2,
    "low": 1
  },
  "by_group_id": {
    "9001": 8,
    "9003": 2
  },
  "sla_breached": 5
}
```

Sample matches:

```json
[
  {
    "id": 10001,
    "subject": "Autopay mandate failed but customer was still charged",
    "status": "open",
    "priority": "urgent"
  },
  {
    "id": 10015,
    "subject": "Autopay mandate created but not debited on due date",
    "status": "open",
    "priority": "urgent"
  },
  {
    "id": 10043,
    "subject": "Autopay mandate revoked but debits continue",
    "status": "open",
    "priority": "urgent"
  },
  {
    "id": 10036,
    "subject": "Autopay debit date changed without consent",
    "status": "open",
    "priority": "urgent"
  },
  {
    "id": 10041,
    "subject": "Customer disputes autopay debit as unauthorised",
    "status": "open",
    "priority": "urgent"
  }
]
```

Caveats returned with the result:

- Freshdesk exposes no full-text search. This result comes from a bounded scan of the most recently updated tickets, not from an index of the whole helpdesk.

