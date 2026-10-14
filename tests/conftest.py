"""Shared fixtures.

Every test runs against the bundled mock Freshdesk app, in-process, over
``httpx.ASGITransport``. That means the suite needs no network, no Freshdesk
account, no credentials, and does not consume anybody's rate-limit budget.

Failure modes are injected deterministically via ``MockSettings.fail_on_call``.
"""

from __future__ import annotations

import socket
import threading
from typing import AsyncIterator, Iterator

import httpx
import pytest
import pytest_asyncio

from freshdesk_connector.client import FreshdeskClient
from freshdesk_connector.config import Config
from freshdesk_connector.mock.app import MockSettings, MockState, build_app
from freshdesk_connector.ratelimit import RateLimitGovernor
from freshdesk_connector.service import FreshdeskService

TEST_API_KEY = "test-api-key"


def make_config(**overrides) -> Config:
    base = dict(
        domain="acme",
        api_key=TEST_API_KEY,
        base_url="http://mock.local",
        plan="growth",
        timeout=5.0,
        max_retries=3,
        request_deadline=10.0,
        enable_writes=False,
    )
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


@pytest.fixture
def settings() -> MockSettings:
    """A permissive mock: no rate limiting, no chaos."""
    return MockSettings(api_key=TEST_API_KEY, overall_per_minute=0)


@pytest.fixture
def app(settings: MockSettings):
    return build_app(settings)


@pytest.fixture
def mock_state(app) -> MockState:
    return app.state.mock


@pytest.fixture
def config() -> Config:
    return make_config()


@pytest_asyncio.fixture
async def client(config: Config, app) -> AsyncIterator[FreshdeskClient]:
    async with FreshdeskClient(config, transport=httpx.ASGITransport(app=app)) as c:
        yield c


@pytest_asyncio.fixture
async def service(client: FreshdeskClient, config: Config) -> FreshdeskService:
    return FreshdeskService(client, config)


# --------------------------------------------------------------------------- #
# A real TCP mock server, for tests that must exercise the full HTTP stack
# (the MCP stdio end-to-end test spawns a separate server process).
# --------------------------------------------------------------------------- #

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def live_mock_server(settings: MockSettings) -> Iterator[str]:
    """Run the mock under uvicorn on a real port; yields the base URL."""
    import uvicorn

    port = _free_port()
    app = build_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    for _ in range(200):  # up to ~10s
        if server.started:
            break
        threading.Event().wait(0.05)
    if not server.started:  # pragma: no cover
        raise RuntimeError("mock server did not start")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def frozen_clock():
    """A monotonic clock plus an async sleeper that advances it instantly.

    Lets rate-limit/backoff tests run without real sleeping.
    """

    class Clock:
        def __init__(self) -> None:
            self.now = 0.0
            self.slept: list[float] = []

        def __call__(self) -> float:
            return self.now

        async def sleep(self, seconds: float) -> None:
            self.slept.append(seconds)
            self.now += seconds

        @property
        def total_slept(self) -> float:
            return sum(self.slept)

    return Clock()


@pytest.fixture
def governor(frozen_clock):
    return RateLimitGovernor(
        "growth",
        clock=frozen_clock,
        sleeper=frozen_clock.sleep,
        jitter=lambda a, b: 0.0,
    )
