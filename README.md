# Freshdesk connector for Agent Studio

A private connector that lets an Agent Studio agent read a merchant's Freshdesk
helpdesk — tickets, conversations, contacts, companies, agents and routing groups —
over the Model Context Protocol.

```
Agent (Agent Studio / Claude Desktop / Cursor)
        │  MCP over stdio
        ▼
freshdesk-connector  ──►  Freshdesk REST API v2  (merchant account)
  server.py     tools + guardrails
  service.py    merchant operations            (envelopes, projections, query building)
  client.py     auth, retries, cursors, audit  (rate-limit aware)
  ratelimit.py  per-endpoint budget governance
```

**Everything in this repo runs offline.** A faithful mock Freshdesk (45 fictional
tickets, real rate-limit headers, injectable 429s) backs the demo and the test
suite, so no Freshdesk account and no rate-limit budget are consumed.

| Document | What it covers |
| --- | --- |
| `CAPABILITIES.md` | **What the agent can and cannot do** — the deliverable's capability statement |
| `LIMITATIONS.md` | Assumptions, limitations, mitigations and the long-term fixes I would ship next |
| `docs/mcp-tools.json` | The machine-readable MCP tool specification (14 tools with schemas + annotations) |
| `docs/demo-output.md` | Captured output of the three merchant workflows |

---

## 1. Setup

```bash
git clone <repo-url> && cd freshdesk-mcp-connector

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt       # runtime + dev/test extras

# Optional: point at a real account instead of the bundled mock
cp .env.example .env && $EDITOR .env  # set FRESHDESK_DOMAIN + FRESHDESK_API_KEY
```

`pip install -e .` also works; the only runtime dependencies are `httpx` and `mcp`.

## 2. Run it — three commands

```bash
# a) prove it works, offline, in ~6 seconds (17 end-to-end checks)
python -m freshdesk_connector.cli self-test

# b) run the three merchant workflows against the mock
python -m freshdesk_connector.cli demo --mock

# c) serve the MCP tools to an agent
python -m freshdesk_connector.cli serve            # stdio transport
```

Sample `self-test` output:

```
PASS  auth + connectivity
PASS  cursor resumes exactly where the last page stopped
PASS  structured search translates labels into Freshdesk enums   (status:2 AND (priority:3 OR priority:4))
PASS  the 30-day default window is disclosed, not hidden         (default=40 widened=45 tickets)
PASS  two injected 429s are survived by honouring Retry-After    (throttled=2 retries=2)
PASS  a backoff longer than the deadline degrades to partial results, not a hang
PASS  bad credentials produce a clear, non-retryable auth error
17/17 checks passed
```

### Run against a real Freshdesk account

Set the credentials and drop `--mock`:

```bash
export FRESHDESK_DOMAIN=yourcompany
export FRESHDESK_API_KEY=xxxxxxxxxxxxxxxx
python -m freshdesk_connector.cli demo \
    --email customer@example.com --term "autopay" --updated-since 2026-09-01
```

Use a **trial account** and read-mostly calls; see `LIMITATIONS.md` §B.5 about
shared budgets before pointing it at production.

### Wire it into an MCP client

```jsonc
// ~/Library/Application Support/Claude/claude_desktop_config.json  (macOS)
{
  "mcpServers": {
    "freshdesk": {
      "command": "python",
      "args": ["-m", "freshdesk_connector.cli", "serve"],
      "cwd": "/absolute/path/to/freshdesk-mcp-connector",
      "env": {
        "FRESHDESK_DOMAIN": "yourcompany",
        "FRESHDESK_API_KEY": "…",
        "PYTHONPATH": "src"
      }
    }
  }
}
```

The connector is read-only by default. Add `"FRESHDESK_ENABLE_WRITES": "true"` only
if you want the single guarded write tool (private internal notes) exposed.

## 3. Talk to the mock by hand

```bash
# terminal 1 — a mock Freshdesk on :8099 with a deliberately tight budget
python -m freshdesk_connector.cli mock --port 8099 --rate-limit 20 --ticket-list-limit 5

# terminal 2
curl -s -u mock-api-key:X http://127.0.0.1:8099/api/v2/tickets?per_page=2 | head -40
curl -si -u mock-api-key:X 'http://127.0.0.1:8099/api/v2/tickets?per_page=3' | grep -i ratelimit
curl -s -u mock-api-key:X 'http://127.0.0.1:8099/api/v2/search/tickets?query=priority:4'
curl -s http://127.0.0.1:8099/__mock/stats      # call accounting per endpoint bucket
```

## 4. Tests

```bash
pytest -q                          # 91 tests, ~11s, no network
pytest tests/test_mcp_protocol.py -q   # spawns the real MCP server over stdio
```

The suite covers authentication and the error taxonomy, query building, pagination
and cursor exactness, rate-limit pacing and 429 recovery, the service primitives,
the MCP surface (tool registration, annotations, error envelopes) and a full
stdio round-trip in which a real MCP client drives a real server process against a
real HTTP server.

## 5. Command reference

| Command | Purpose |
| --- | --- |
| `serve [--transport stdio\|sse\|streamable-http]` | Run the MCP server. |
| `mock [--port] [--rate-limit] [--ticket-list-limit] [--chaos-429]` | Run the mock Freshdesk API. |
| `demo [--mock] [--email] [--term] [--updated-since]` | Run the three merchant workflows. |
| `self-test` | 17 end-to-end checks against the mock. |
| `tools [--writes] [--out FILE]` | Emit the MCP tool specification as JSON. |

## 6. Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `FRESHDESK_DOMAIN` | – | Subdomain, the `acme` in `https://acme.freshdesk.com`. |
| `FRESHDESK_API_KEY` | – | API key. Freshdesk uses it as the HTTP Basic username, password `X`. |
| `FRESHDESK_BASE_URL` | derived | Override to point at a mock/self-hosted endpoint. |
| `FRESHDESK_PLAN` | `auto` | `auto \| trial \| growth \| pro \| enterprise`. `auto` starts conservative and learns the real ceiling from response headers. |
| `FRESHDESK_MAX_RETRIES` | `3` | Max HTTP attempts per call, including retries. |
| `FRESHDESK_TIMEOUT` | `15` | Per-request network timeout (seconds). |
| `FRESHDESK_REQUEST_DEADLINE` | `25` | Wall-clock budget per tool call; exceeding it returns partial results rather than hanging. |
| `FRESHDESK_ENABLE_WRITES` | `false` | Registers the single private-note write tool. |
| `FRESHDESK_AUDIT_LOG` | unset | JSONL audit trail of every call (paths, statuses, rate-limit headers, no bodies, no secrets). |

Mock-only variables: `MOCK_API_KEY`, `MOCK_RATE_LIMIT`, `MOCK_TICKET_LIST_LIMIT`,
`MOCK_CHAOS_429`, `MOCK_CHAOS_500`, `MOCK_LATENCY_MS`, `MOCK_WINDOW_SECONDS`,
`MOCK_RETRY_AFTER`.

## 7. Authentication model

**Freshdesk uses API-key Basic auth, not OAuth** — there is no OAuth flow to
implement for a private/server-side integration. The mechanics are:

| Step | Where it happens |
| --- | --- |
| 1. Operator creates the key in Freshdesk → *Profile Settings → API Key* (agent-scoped, revocable) | Freshdesk UI |
| 2. Key is supplied via `FRESHDESK_API_KEY` (env or `.env`, gitignored) | `config.py` |
| 3. Every request is sent as `Authorization: Basic base64("<api_key>:X")` — the API key is the username and the password is the literal string `X` | `client.py` (`httpx.BasicAuth`) |
| 4. Key is never logged, never returned by a tool, and never echoed into an error or the audit trail | `Config.redacted()`, `_scrub()`, `client._audit()` |

Failure handling is part of the contract:

* `401` → `auth_failed`, **non-retryable**, hint tells the agent to stop and ask a
  human to rotate the key. Retrying a `401` cannot succeed and still consumes the
  account's rate-limit budget.
* `403` → `permission_denied`, non-retryable: the key is valid but not allowed to
  read that object. The agent reports the gap instead of looping.
* Missing configuration fails fast at startup with a `config_error` naming the
  exact variable to set.

*Why this shape:* the credential is resolved once at process start and held
in-process, so one connector process serves exactly one merchant account. That is
the right boundary for a "private connector" (a merchant installs it against their
own helpdesk), and it is what makes the credential impossible to leak through tool
arguments or model context. For a multi-tenant deployment, see `LIMITATIONS.md`
§D.1 — the same `Config` boundary is where a per-tenant credential provider would
slot in, and Freshdesk's agent-scoped keys make per-tenant rotation safe.

## 8. Three design decisions worth calling out

**1. Rate-limit governance, not rate-limit handling.** Freshdesk limits are applied
*per account*, shared with every other integration the merchant runs, and the
per-endpoint caps are tighter than the advertised account ceiling (Tickets List is
roughly a third of Growth's budget). The connector therefore tracks a separate
budget per endpoint bucket, paces proactively from `X-RateLimit-Remaining`, honours
`Retry-After`, **halves** a bucket's effective cap when a bucket-specific 429 fires
while the account budget looked healthy, and recovers that cap slowly on success.

**2. Partial results beat hanging, and both beat lying.** Every call has a deadline.
When a backoff would exceed it (or the page ceiling is reached) the connector
returns what it has, sets `pagination.truncated`, and writes a warning the agent is
instructed to repeat to the user. The three silent Freshdesk traps — the 30-day
default window, no full-text search, and the 300-ticket search ceiling — are
surfaced in the tool result itself, not just in docs.

**3. Tool descriptions are prompt engineering.** Each of the 14 tools documents
*when to use it*, *when not to*, its cost, and its failure semantics, because that
text is the only thing the model reads before acting. Discovery tools
(`get_ticket_fields`, `list_groups`) exist purely so the model can learn this
account's real field names and routing ids instead of guessing them and receiving
an opaque HTTP 400.

## 8. Repository layout

```
src/freshdesk_connector/
  server.py        MCP tools (13 read + 1 opt-in write), annotations, instructions
  service.py       merchant operations: envelopes, projections, query building
  client.py        httpx client: auth, retries, offset cursors, audit log
  ratelimit.py     per-endpoint budget governor (AIMD learning)
  models.py        enum translation, token-lean projections, filter builder
  errors.py        error taxonomy with actionable agent hints
  config.py        env loading, validation, redaction
  demo.py          the three merchant workflows
  cli.py           serve / mock / demo / self-test / tools
  mock/            offline Freshdesk stand-in: app, filter-query parser, fixtures
tests/             91 tests, including a real MCP stdio round-trip
docs/              generated tool spec + captured demo output
```
