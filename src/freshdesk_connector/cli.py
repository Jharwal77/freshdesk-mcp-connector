"""Command line entry points.

    freshdesk-connector serve              # run the MCP server over stdio
    freshdesk-connector mock --port 8099   # run the local mock Freshdesk API
    freshdesk-connector self-test --mock   # end-to-end checks against the mock
    freshdesk-connector demo --mock        # run the three merchant workflows
    freshdesk-connector tools --out docs/mcp-tools.json

Everything works offline against the bundled mock, so a reviewer can verify the
connector without a Freshdesk account and without spending anyone's rate-limit
budget.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from .client import FreshdeskClient
from .config import Config
from .errors import ConfigError, FreshdeskError
from .service import FreshdeskService


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _mock_config(**overrides: Any) -> Config:
    """Config pointing at the in-process mock (no network, no credentials)."""
    base = {
        "domain": "acme",
        "api_key": "mock-api-key",
        "base_url": "http://mock.local",
        "plan": "growth",
    }
    base.update({k: v for k, v in overrides.items() if v is not None})
    return Config(**base)  # type: ignore[arg-type]


def _load_mock():
    """Import the mock HTTP layer, explaining how to fix a missing dependency."""
    try:
        from .mock.app import MockSettings, build_app  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        raise ConfigError(
            "The offline mock needs optional dependencies that are not installed "
            f"(missing: {exc.name}). Install them with: pip install 'freshdesk-mcp-connector[dev]' "
            "or, from a checkout, pip install -r requirements.txt",
            agent_hint=(
                "Install the dev extras or requirements.txt, then re-run the command. "
                "Running against a real account (without --mock) needs no mock dependencies."
            ),
        ) from exc
    return MockSettings, build_app


def _make_service(config: Config, *, use_mock: bool, mock_settings: Any = None) -> tuple[FreshdeskService, Any]:
    """Return (service, cleanup_callable). Cleanup closes the HTTP client."""
    transport = None
    if use_mock:
        import httpx

        MockSettings, build_app = _load_mock()
        settings = mock_settings or MockSettings(api_key=config.api_key, domain=config.domain)
        app = build_app(settings)
        transport = httpx.ASGITransport(app=app)

    client = FreshdeskClient(config, transport=transport)
    return FreshdeskService(client, config), client


async def _with_service(config: Config, use_mock: bool, fn, *, mock_settings: Any = None) -> Any:
    service, client = _make_service(config, use_mock=use_mock, mock_settings=mock_settings)
    async with client:
        return await fn(service, client)


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

async def _cmd_demo(args: argparse.Namespace) -> int:
    from .demo import run_all

    config = _mock_config() if args.mock else Config.from_env()
    results = await _with_service(
        config,
        args.mock,
        lambda service, client: run_all(
            service,
            email=args.email,
            term=args.term,
            updated_since=args.updated_since,
        ),
    )
    _print(results)
    failed = [name for name, r in results.items() if not r.get("ok")]
    if failed:
        print(f"\n{len(failed)} workflow(s) failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


async def _cmd_self_test(args: argparse.Namespace) -> int:
    """End-to-end checks a reviewer can run in one command, fully offline.

    Failure modes are injected deterministically (``fail_next`` counts) rather
    than probabilistically, so a failure here is always reproducible.
    """
    import httpx

    MockSettings, build_app = _load_mock()

    checks: list[tuple[str, bool, str]] = []

    def record(name: str, passed: bool, detail: str = "") -> None:
        checks.append((name, passed, detail))

    config = _mock_config()

    # ---------------------------------------------------------------- happy path
    app = build_app(MockSettings(api_key=config.api_key, overall_per_minute=0))
    async with FreshdeskClient(config, transport=httpx.ASGITransport(app=app)) as client:
        service = FreshdeskService(client, config)

        health = await service.connector_health()
        record("auth + connectivity", bool(health.get("ok") and health["data"]["authenticated"]))

        listing = await service.list_tickets(limit=5)
        first_page_ids = [t["id"] for t in listing.get("data", [])]
        record(
            "list tickets returns compact records",
            bool(listing.get("ok")) and listing["pagination"]["returned"] == 5,
            f"{listing.get('pagination', {}).get('returned')} records",
        )
        first = listing["data"][0] if listing.get("data") else {}
        record(
            "compact projection is agent-sized, not a raw dump",
            "description" not in first and isinstance(first.get("status"), str) and len(first) < 30,
            f"{len(first)} fields",
        )

        cursor = listing.get("pagination", {}).get("next_cursor")
        page2 = await service.list_tickets(limit=5, cursor=cursor)
        second_page_ids = [t["id"] for t in page2.get("data", [])]
        record(
            "cursor resumes exactly where the last page stopped",
            bool(page2.get("ok")) and bool(second_page_ids) and not set(first_page_ids) & set(second_page_ids),
            f"page1={first_page_ids[:2]}... page2={second_page_ids[:2]}...",
        )

        search = await service.search_tickets(status="open", priority_at_least="high", limit=10, max_pages=1)
        generated = search.get("meta", {}).get("generated_freshdesk_query", "")
        record(
            "structured search translates labels into Freshdesk enums",
            bool(search.get("ok"))
            and "status:2" in generated
            and "(priority:3 OR priority:4)" in generated,
            generated,
        )

        bad = await service.search_tickets(custom_fields={"cf_does_not_exist": "x"}, limit=1)
        record(
            "a field that does not exist in this account fails with an actionable error",
            (not bad.get("ok")) and bad["error"]["code"] == "invalid_request",
            bad.get("error", {}).get("agent_hint", "")[:58] + "...",
        )

        text = await service.search_tickets_by_text("autopay", max_pages=2)
        record(
            "bounded text scan reports its own coverage honestly",
            bool(text.get("ok"))
            and "scan_complete" in text.get("pagination", {})
            and len(text.get("warnings", [])) >= 1,
            f"examined={text.get('pagination', {}).get('tickets_examined')} complete={text.get('pagination', {}).get('scan_complete')}",
        )

        note = await service.add_private_note(10001, "test")
        record(
            "writes fail closed unless the operator enabled them",
            (not note.get("ok")) and note["error"]["code"] == "writes_disabled",
        )

        convs = await service.list_ticket_conversations(10001)
        visibilities = {c.get("visibility") for c in convs.get("data", [])}
        record(
            "conversations label private notes so agents cannot leak them",
            bool(convs.get("ok")) and visibilities == {"internal", "public"},
            f"visibilities={sorted(visibilities)}",
        )

        fields = await service.get_ticket_fields()
        names = fields.get("data", {}).get("filter_field_names", [])
        record(
            "ticket fields teach the agent this account's real filter names",
            bool(fields.get("ok")) and "status" in names and "cf_autopay_status" in names,
        )

        # Freshdesk's list endpoint silently hides tickets older than 30 days
        # unless updated_since is passed. A connector that does not surface this
        # causes confident, wrong answers, so it is asserted here.
        default_window = await service.list_tickets(limit=100)
        widened = await service.list_tickets(limit=100, updated_since="2020-01-01")
        default_ids = {t["id"] for t in default_window.get("data", [])}
        widened_ids = {t["id"] for t in widened.get("data", [])}
        record(
            "the 30-day default window is disclosed, not hidden",
            bool(default_window.get("ok"))
            and bool(widened.get("ok"))
            and len(widened_ids) > len(default_ids)
            and "coverage_caveat" in (default_window.get("meta") or {}),
            f"default={len(default_ids)} widened={len(widened_ids)} tickets",
        )

    # ------------------------------------------- reactive 429: retry and recover
    flaky = build_app(
        MockSettings(
            api_key=config.api_key,
            fail_on_call={"default": [1, 2], "tickets_list": [1]},
            retry_after_seconds=1,
            window_seconds=5,
        )
    )
    flaky_config = _mock_config(max_retries=4, request_deadline=60)
    async with FreshdeskClient(flaky_config, transport=httpx.ASGITransport(app=flaky)) as client:
        service = FreshdeskService(client, flaky_config)
        ticket = await service.get_ticket(10001, include_conversations=False)
        record(
            "two injected 429s are survived by honouring Retry-After",
            bool(ticket.get("ok")) and client.stats["throttled"] == 2,
            f"throttled={client.stats['throttled']} retries={client.stats['retries']}",
        )
        listing2 = await service.list_tickets(limit=3)
        record(
            "per-endpoint sub-limit also recovers",
            bool(listing2.get("ok")) and client.stats["throttled"] == 3,
            f"throttled={client.stats['throttled']}",
        )
        learned = client.governor.learned_bucket_caps
        record(
            "a bucket-specific 429 teaches the connector a tighter endpoint cap",
            "tickets_list" in learned,
            f"learned={learned}",
        )

    # ------------------------------- proactive budget awareness from headers
    metered = build_app(MockSettings(api_key=config.api_key, overall_per_minute=50, window_seconds=60))
    async with FreshdeskClient(_mock_config(), transport=httpx.ASGITransport(app=metered)) as client:
        service = FreshdeskService(client, _mock_config())
        await service.list_tickets(limit=2)
        status = client.rate_limit_status()
        bucket_cap = status["buckets"]["tickets_list"]["effective_cap_per_minute"]
        record(
            "X-RateLimit headers are folded into a live budget view",
            status["observed_account_limit_per_minute"] == 50 and bucket_cap <= 50,
            f"observed={status['observed_account_limit_per_minute']} tickets_list_cap={bucket_cap}",
        )

    # ------------------- graceful degradation: never hang an agent on a backoff
    slow = build_app(
        MockSettings(
            api_key=config.api_key,
            fail_on_call={"tickets_search": [2]},  # page 1 succeeds, page 2 hits the ceiling
            retry_after_seconds=45,
            window_seconds=60,
        )
    )
    impatient = _mock_config(max_retries=3, request_deadline=5)
    async with FreshdeskClient(impatient, transport=httpx.ASGITransport(app=slow)) as client:
        service = FreshdeskService(client, impatient)
        degraded = await service.search_tickets(created_after="2020-01-01", limit=60, max_pages=3)
        record(
            "a backoff longer than the deadline degrades to partial results, not a hang",
            bool(degraded.get("ok"))
            and bool(degraded.get("warnings"))
            and degraded["pagination"]["returned"] > 0,
            f"returned={degraded.get('pagination', {}).get('returned')} "
            f"| {(degraded.get('warnings') or ['no warning'])[0][:44]}...",
        )

    # ------------------------------------------------------- auth failure shape
    wrong = build_app(MockSettings(api_key="a-different-key"))
    async with FreshdeskClient(_mock_config(), transport=httpx.ASGITransport(app=wrong)) as client:
        service = FreshdeskService(client, _mock_config())
        result = await service.list_tickets(limit=1)
        err = result.get("error", {})
        record(
            "bad credentials produce a clear, non-retryable auth error",
            err.get("code") == "auth_failed" and err.get("retryable") is False,
            err.get("code", ""),
        )

    # ------------------------------------------------------------------- report
    passed = sum(1 for _, ok, _ in checks if ok)
    width = max(len(name) for name, _, _ in checks)
    print("\nfreshdesk-connector self-test\n" + "=" * (width + 12))
    for name, ok, detail in checks:
        mark = "PASS" if ok else "FAIL"
        suffix = f"  ({detail})" if detail else ""
        print(f"{mark}  {name.ljust(width)}{suffix}")
    print("=" * (width + 12))
    print(f"{passed}/{len(checks)} checks passed\n")
    return 0 if passed == len(checks) else 1


async def _cmd_tools(args: argparse.Namespace) -> int:
    """Dump the MCP tool specification as JSON (used to generate docs)."""
    from .server import ConnectorRuntime, create_server

    config = _mock_config(enable_writes=True) if args.writes else _mock_config()
    server = create_server(config, runtime=ConnectorRuntime(config))
    tools = await server.list_tools()
    spec = {
        "server": {"name": "freshdesk-connector", "version": "0.1.0", "transport": ["stdio"]},
        "writes_included": bool(args.writes),
        "notes": [
            "Freshdesk authenticates with an API key as the HTTP Basic username, so there is no "
            "OAuth flow to document; see CAPABILITIES.md for the credential model.",
            "search_tickets is the workhorse: it accepts human labels (open/urgent) and the "
            "connector translates them into Freshdesk's numeric filter enums.",
            "Tokens are never returned by any tool; errors carry actionable hints instead.",
        ],
        "instructions": server.instructions,
        "tool_count": len(tools),
        "tools": [
            {
                "name": t.name,
                "description": (t.description or "").strip(),
                "input_schema": t.input_schema,
                # Emit the wire format (camelCase) so this file can be diffed
                # against what a client actually receives over MCP.
                "annotations": (
                    t.annotations.model_dump(by_alias=True, exclude_none=True, mode="json")
                    if t.annotations
                    else None
                ),
            }
            for t in sorted(tools, key=lambda t: t.name)
        ],
    }
    payload = json.dumps(spec, indent=2)
    if args.out:
        from pathlib import Path

        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
        print(f"wrote {args.out}: {len(tools)} tools")
    else:
        print(payload)
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from .server import create_server

    server = create_server(Config.from_env(env_path=args.env_file))
    server.run(args.transport)
    return 0


def _cmd_mock(args: argparse.Namespace) -> int:
    import uvicorn

    MockSettings, build_app = _load_mock()
    settings = MockSettings.from_env()
    if args.rate_limit is not None:
        settings.overall_per_minute = args.rate_limit
    if args.ticket_list_limit is not None:
        settings.bucket_caps["tickets_list"] = args.ticket_list_limit
    if args.chaos_429 is not None:
        settings.chaos_429_rate = args.chaos_429
    app = build_app(settings)
    print(
        f"mock Freshdesk on http://{args.host}:{args.port}  "
        f"api_key={settings.api_key}  rate_limit={settings.overall_per_minute or 'off'}/min"
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="freshdesk-connector",
        description="Private Freshdesk connector for Agent Studio (MCP server + client library).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the MCP server")
    serve.add_argument("--transport", default="stdio", choices=["stdio", "sse", "streamable-http"])
    serve.add_argument("--env-file", default=".env", help="path to a .env file with credentials")
    serve.set_defaults(func=_cmd_serve, is_async=False)

    mock = sub.add_parser("mock", help="run the local mock Freshdesk API")
    mock.add_argument("--host", default="0.0.0.0")
    mock.add_argument("--port", type=int, default=8099)
    mock.add_argument("--rate-limit", type=int, default=None, help="overall requests/minute (0 = off)")
    mock.add_argument("--ticket-list-limit", type=int, default=None, help="per-endpoint cap for tickets list")
    mock.add_argument("--chaos-429", type=float, default=None, help="probability of an injected 429 (0-1)")
    mock.set_defaults(func=_cmd_mock, is_async=False)

    demo = sub.add_parser("demo", help="run the three merchant workflows")
    demo.add_argument("--mock", action="store_true", help="use the offline mock instead of a real account")
    demo.add_argument("--email", default="vikram.rao@example.com")
    demo.add_argument("--term", default="autopay")
    demo.add_argument("--updated-since", default=None, help="ISO date to widen the scan window")
    demo.set_defaults(func=_cmd_demo, is_async=True)

    test = sub.add_parser("self-test", help="run end-to-end checks against the offline mock")
    test.set_defaults(func=_cmd_self_test, is_async=True)

    tools = sub.add_parser("tools", help="print or write the MCP tool specification")
    tools.add_argument("--out", default=None, help="write JSON to this path instead of stdout")
    tools.add_argument(
        "--writes",
        action="store_true",
        help="include the opt-in write tool (freshdesk_add_private_note) in the spec",
    )
    tools.set_defaults(func=_cmd_tools, is_async=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "mock", False) and args.command == "demo":
        pass
    try:
        if getattr(args, "is_async", False):
            return asyncio.run(args.func(args))
        return args.func(args)
    except ConfigError as exc:
        print(f"configuration error: {exc.message}\n{exc.agent_hint}", file=sys.stderr)
        return 2
    except FreshdeskError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
