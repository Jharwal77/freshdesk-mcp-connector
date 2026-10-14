# What the agent can and cannot do

A capability statement for the `freshdesk-connector` MCP server. Written to be
read by the person configuring an Agent Studio agent, and by the agent itself
(the same text is shipped as server `instructions`).

---

## 1. What the agent **can** do

| Capability | Tool | Notes |
| --- | --- | --- |
| Verify it can reach the helpdesk | `freshdesk_connector_health` | Cheap auth probe + latency. |
| Learn this account's real filter fields | `freshdesk_get_ticket_fields` | **Call this first.** Standard and custom field names are account-specific and case-sensitive. |
| Learn routing teams | `freshdesk_list_groups` | `group_id` filters are useless without it. |
| Learn who agents are | `freshdesk_list_agents` | Turns `responder_id` into a human name. |
| Browse tickets in order | `freshdesk_list_tickets` | Newest-updated first; paginated with resumable cursors. |
| Read one ticket in full | `freshdesk_get_ticket` | Description, SLA clock, timeline, optional conversation history. |
| Filter tickets by structured fields | `freshdesk_search_tickets` | Status, priority, source, type, agent, group, requester, company, tags, dates, custom fields. |
| Find tickets by keyword | `freshdesk_search_tickets_by_text` | Bounded scan; reports its own coverage. |
| Read a ticket's history | `freshdesk_list_ticket_conversations` | Every entry labelled `visibility: internal \| public`. |
| Find a customer | `freshdesk_get_contact`, `freshdesk_search_contacts` | By id, email, phone, name or company. |
| Read the account context | `freshdesk_get_company` | Tier, health score, industry, optionally contacts. |
| Check its own budget | `freshdesk_rate_limit_status` | Free (no API call). Reads the shared account budget. |
| Add an internal note *(opt-in)* | `freshdesk_add_private_note` | Only when `FRESHDESK_ENABLE_WRITES=true`. Private notes only, never a customer-visible reply. |

Concretely, an agent can answer questions such as:

* *"Which open urgent tickets have already breached their SLA, and who owns them?"*
* *"What's the full context on the customer who just called about order `order_Nk82Hs91Lm`?"*
* *"How many settlement complaints came in this week, and are they concentrated in one team?"*
* *"Pull the conversation on ticket 10014 so we can draft a reply."*
* *"Has this customer written in before, and did we already refund them?"*

## 2. What the agent **cannot** do

| Not possible | Why | What the agent should do instead |
| --- | --- | --- |
| **Full-text search** | Freshdesk has no such endpoint. Search filters structured fields only. | Use `search_tickets_by_text` and *report the scan coverage*, or ask the user for a structured filter (order id, date, tag). |
| **Search arbitrary text fields server-side** | Same reason; `subject`, `description` are not filterable fields in the search grammar. | Scan locally, or set `include_descriptions=true` on the scan. |
| **See more than 300 tickets per search** | Freshdesk caps the search endpoint at page 10 × 30 records. | Split by date range (`created_after` / `created_before`) and merge. |
| **See all history by default** | The list endpoint returns only tickets *created in the last 30 days* unless `updated_since` is passed. | Pass `updated_since`, or state the window in the answer. The connector echoes the window it actually used. |
| **Read attachments, inline images, or email headers** | Not exposed by the connector (attachment binaries are not useful in a text context). | Link the ticket URL and tell the user to open it. |
| **Read satisfaction ratings, time-tracking or canned responses** | Out of scope for the requested primitives. | — |
| **Reply to a customer, or change ticket fields** | Deliberately not implemented. Customer-visible writes need human review. *(see §4)* | Summarise the draft reply and hand it to a human, or add a private note if writes are enabled. |
| **Delete, merge, or close tickets** | Same. | — |
| **Bypass permissions** | The connector uses the configured API key. Anything that key cannot read, the agent cannot read (`permission_denied`, non-retryable). | Report the gap to the user. |
| **Act on another merchant's helpdesk** | One process = one account = one credential. | Run a connector instance per account. |
| **Guarantee fresh data** | Responses are live, but the agent may be working from a cursor issued minutes ago. | Re-query when freshness matters. |

## 3. Guardrails the connector enforces for the agent

1. **Private notes are labelled.** Every conversation entry carries
   `visibility: internal | public` and `customer_visible`. Internal notes are
   private agent notes; quoting one to a customer is a data leak. The tool returns
   a warning whenever internal entries are present.
2. **Partial results announce themselves.** `pagination.truncated` and
   `warnings` are present whenever a result set was cut short by a rate limit, the
   deadline, a page ceiling, or the 300-ticket search cap. The agent is told never
   to present a truncated result as complete.
3. **Rate limits protect the merchant's other integrations.** Budgets are tracked
   per endpoint bucket, the connector paces itself proactively from
   `X-RateLimit-Remaining`, honours `Retry-After`, and refuses to hang: when a
   required backoff would exceed the call deadline it returns partial results with
   an explanation.
4. **Errors are instructions, not stack traces.** Every failure returns
   `{code, message, retryable, agent_hint}`. A `401` says "ask a human to rotate the
   key, do not retry"; a `400` on a filter says "call `freshdesk_get_ticket_fields`".
5. **Writes fail closed.** With `FRESHDESK_ENABLE_WRITES` unset, the write tool is
   not registered at all — an agent cannot call a tool that does not exist.

## 4. Recommended agent policy (human-in-the-loop)

| Action | Policy |
| --- | --- |
| Reading tickets, contacts, conversations | Autonomous. |
| Reporting SLA breaches or volumes | Autonomous, but the answer must repeat any `warnings` and the coverage window. |
| Drafting a customer reply | Autonomous drafting, **human sends**. The connector deliberately cannot send it. |
| Writing an internal note | Human confirms the exact text first. Idempotency keys make retries safe. |
| Changing ticket status, assigning, refunding | Out of scope for this connector; route to the merchant's own workflow with approval. |

## 5. Cost model the agent should reason about

| Operation | Rate-limit cost |
| --- | --- |
| Any single tool call | 1 API call (2 calls when conversations are included with a ticket). |
| `?include=description` (list or scan) | Freshdesk charges **3 units** instead of 1. |
| `search_tickets` page | 1 call per 30 records (endpoint is fixed at 30/page). |
| `list_tickets` call | 1 call for up to 100 records. |
| `rate_limit_status`, `connector_health` | `rate_limit_status` is free; health is 1 call. |

On a Growth plan the *Tickets List* endpoint allows only ~40 calls/min, so a
100-ticket walk at `limit=5` (20 calls) already consumes half that minute's
endpoint budget. Prefer larger `limit` values and narrower filters over many small
page-throughs.
