# Assumptions, limitations, and what I would fix next

Written for a reviewer who wants to know where the edges are, and for the engineer
who inherits this. Grouped by *honest limitation* → *mitigation shipped* → *the
proper long-term fix*.

---

## A. Assumptions this connector makes

1. **One process = one Freshdesk account.** Credentials come from the environment
   (`FRESHDESK_DOMAIN`, `FRESHDESK_API_KEY`) and are never passed per call. Multi-merchant
   hosting needs a per-tenant credential store; see §D.1.
2. **Read-heavy usage.** The connector is built for retrieval and analysis. A single
   guarded write (private note) exists for agent hand-off, and nothing else.
3. **Freshdesk v2 API shape.** Field names, enum values and pagination behaviour follow
   the v2 REST API as documented by Freshworks and corroborated against community
   reports of the per-endpoint limits.
4. **Rate-limit profiles are conservative defaults, not gospel.** Freshworks has been
   migrating accounts from hourly to per-minute windows in batches, and published
   Growth figures have differed (100 vs 200/min) between article revisions. The
   `auto` plan profile therefore starts at the *stricter* number and ratchets up using
   the `X-RateLimit-Total` header the API returns on every response.
5. **`X-RateLimit-Total` describes the account's real ceiling.** It is treated as
   authoritative in `auto` mode. If a merchant's plan is upgraded mid-session, the
   connector follows the header rather than its local table.
6. **Demo data is fictional.** The bundled mock's 45 tickets, 4 agents and 3 companies
   are invented (see `mock/fixtures.py`). No real customer data, credentials or PII is
   in this repository.

## B. Limitations, and what is done about them

| # | Limitation | Shipped mitigation | Proper long-term fix |
| --- | --- | --- | --- |
| B.1 | **No full-text search in Freshdesk.** An agent asked "find the complaint about a duplicate charge" cannot do it server-side. | `search_tickets_by_text` performs a bounded scan and returns `scan_complete`, `tickets_examined` and `pages_scanned`, plus a warning that it is not an index. | Maintain a search index (Elastic/OpenSearch or the merchant's warehouse) hydrated by Freshdesk webhooks, and expose *that* to the agent. This is the single highest-value addition. |
| B.2 | **Search sees at most 300 tickets** (10 pages × 30). A busy queue can exceed that in a day. | The connector detects a total > 300 and tells the agent to split by date range; the ceiling is exposed in `meta.search_limits`. | Webhook-driven incremental sync into a local store, then query locally with no ceiling. |
| B.3 | **The list endpoint hides tickets older than 30 days** unless `updated_since` is supplied. This is the most dangerous trap because it fails silently. | The window is echoed in `meta.coverage_window` with an explicit `coverage_caveat`, and a test asserts the caveat appears. | Same store-and-sync approach as B.2; freshness then becomes a sync question rather than a query question. |
| B.4 | **Cursor resumption can cost one redundant API call.** Cursors are byte offsets bound to the query; for the fixed-30 search endpoint a mid-page resume re-fetches that page and skips. | Offsets guarantee *correctness* (no skipped or duplicated records), which matters more than one extra call. Page size tracks `limit` on the list endpoint so normal paging is aligned and waste-free. | Server-side snapshot cursors, or a local cache keyed by query so a resume is served locally. |
| B.5 | **Rate-limit accounting is per process.** Two connector instances (or another integration on the same account) each think they have the full budget. | Proactive pacing with a safety margin and a reserve; per-endpoint buckets; AIMD-style learning after 429s; `X-RateLimit-Remaining` drives the pacing. | A shared limiter (Redis token bucket) per Freshdesk account, so all instances draw from one budget. Required before horizontal scaling. |
| B.6 | **The `priority:>N` comparison operator is ambiguous** in Freshworks' own documentation (their example labels `priority:>3` as "Urgent and High"). | The query builder never emits `>` for priority: `priority_at_least="high"` expands to the unambiguous `(priority:3 OR priority:4)`. | Confirm against a live account and, if needed, add a compatibility switch. |
| B.7 | **Rate-limit windows are counted in the client's clock, not Freshdesk's.** The governor assumes a 60-second sliding window. | Waits are bounded by the per-call deadline, so a wrong assumption degrades to "partial results with a warning" instead of a hung agent. | Read the server's window from response headers when Freshworks exposes it; otherwise keep the ceiling conservative. |
| B.8 | **Idempotency for the write tool is client-side only.** Freshdesk's note endpoint has no idempotency key. | In-memory dedupe keyed by `idempotency_key` (or a hash of the body), 15-minute TTL, and the tool asks for a stable key. | Durable idempotency store (Redis/Postgres) so the guarantee survives a process restart, plus a reconcile-on-ambiguity step. |
| B.9 | **Compact projections drop fields.** If an agent needs an exotic field, it is not in the default shape. | `compact_ticket` keeps ids, enums, tags, custom fields, SLA maths and URL; the raw payload is still reachable by extending the projection. | Field-set negotiation per agent, or a `fields=` parameter on the read tools. |
| B.10 | **Attachments, satisfaction ratings and time-tracking are not exposed.** | Out of scope for the requested primitives; the ticket URL is returned so a human can open the ticket. | Add `list_ticket_attachments` returning metadata and short-lived signed URLs. |
| B.11 | **No streaming or webhooks.** Answers are as fresh as the moment of the call. | Each read is live; `updated_since` allows incremental polling. | Subscribe to Freshdesk webhooks, keep a local projection, and let the agent query the projection with an explicit `as_of`. |
| B.12 | **Custom-field semantics are not validated beyond existence.** Freshdesk validates the field name but the connector cannot know a merchant's dropdown options. | `get_ticket_fields` returns `choices` for dropdowns so the agent can pick a valid value. | Validate values against the field metadata before issuing the search, and turn a rejected value into a corrective hint. |
| B.13 | **The mock is faithful, not identical.** It reproduces the shapes and traps that matter (headers on every response, `Retry-After`, per-endpoint sub-limits, `invalid_field` errors, the 30-day window, the 300-ticket ceiling), but it is not a byte-for-byte clone of Freshdesk. | Every connector behaviour is exercised against it; running the same suite against a real trial account is a one-line change (`FRESHDESK_BASE_URL` unset, real credentials in `.env`). | Contract tests against a real sandbox account in CI, plus a recorded-fixture layer for offline runs. |

## C. Known rough edges in the code

* `FreshdeskService._idempotency` is an unbounded dict pruned lazily on write — fine for a
  session, wrong for a long-lived server (B.8).
* `mock/query.py` supports the documented filter grammar but not every undocumented
  operator Freshdesk accepts; a query the mock rejects may work live (and vice versa).
* `search_contacts` matching is exact-ish because it delegates to Freshdesk's contact
  search. Partial-name matching returns nothing; a fuzzy local index would be needed for
  "find the customer whose name sounds like…".
* The audit log writes param *keys* only, deliberately. That is good for privacy and
  slightly annoying for debugging a specific bad query; a redacted-values mode is the
  obvious middle ground.

## D. What I would build next, in priority order

1. **Multi-tenant credential store** (KMS/Secrets Manager + per-tenant cache) so one
   process can serve many merchants, with the agent pinned to a tenant id per session.
2. **Webhook-driven incremental sync** into a local store. This removes B.1–B.3 in one go:
   real search, no 300-ticket ceiling, no 30-day window. Everything else in this list is
   smaller than this.
3. **Shared rate-limit limiter** (Redis token bucket per Freshdesk account) so N
   connector replicas share one budget (B.5).
4. **Evaluation harness for agent answers**: a fixture set of questions with known-correct
   answers, scored automatically on every change, so "did the agent get better?" is a
   number rather than an opinion. The three workflows in `demo.py` are the seed set.
5. **Attachment and customer-visible reply tools**, both behind explicit human approval,
   with an approval record written to the audit log.
