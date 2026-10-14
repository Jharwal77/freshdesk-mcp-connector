"""Rate-limit governance.

Why this module exists
----------------------
Freshdesk rate-limits **per Freshdesk account, not per API key**, and the number
is shared with every other integration the merchant has installed. Two
consequences drive the design:

1. A connector that bursts is not just rude, it degrades the merchant's other
   tools. So we pace *proactively* using the ``X-RateLimit-Remaining`` header
   Freshdesk returns on every response, instead of only reacting to 429s.
2. The account-wide ceiling is not the real ceiling. Freshdesk also applies
   tighter **per-endpoint** sub-limits (historically tickets-list is the
   tightest: 40/min on Growth against a 100/min account budget). A naive
   ``remaining > 0`` check therefore still produces 429s. We track a separate
   budget per endpoint bucket and *learn* a tighter cap when a bucket-specific
   429 arrives while the account budget still looks healthy.

Everything here is dependency-free and clock/sleeper injectable so it can be
tested without real time passing.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .errors import LocalBudgetExceeded

WINDOW_SECONDS = 60.0

# Endpoint buckets ------------------------------------------------
BUCKET_TICKETS_LIST = "tickets_list"
BUCKET_TICKETS_SEARCH = "tickets_search"
BUCKET_CONTACTS_LIST = "contacts_list"
BUCKET_DEFAULT = "default"

BUCKET_PARAM_NAMES = {
    BUCKET_TICKETS_LIST: "Tickets List",
    BUCKET_TICKETS_SEARCH: "Tickets Search / Filter",
    BUCKET_CONTACTS_LIST: "Contacts List",
    BUCKET_DEFAULT: "General",
}


@dataclass(frozen=True)
class PlanProfile:
    """Documented limits for a Freshdesk plan.

    Values follow the Freshworks support article on API rate limits. Freshworks
    has been migrating accounts from hourly to per-minute windows in batches and
    different plans were published with different figures over time, so these are
    treated as *conservative defaults*, not as gospel - ``auto`` mode ratchets up
    from observed headers.
    """

    name: str
    overall_per_minute: int
    bucket_caps: dict[str, int] = field(default_factory=dict)
    note: str = ""


PLAN_PROFILES: dict[str, PlanProfile] = {
    # Free/Sprout has no API access at all; treated as trial to fail fast.
    "trial": PlanProfile(
        name="trial",
        overall_per_minute=50,
        bucket_caps={
            BUCKET_TICKETS_LIST: 20,
            BUCKET_TICKETS_SEARCH: 20,
            BUCKET_CONTACTS_LIST: 20,
        },
        note="Every trial period is capped at 50 calls/min.",
    ),
    "growth": PlanProfile(
        name="growth",
        overall_per_minute=100,
        bucket_caps={
            BUCKET_TICKETS_LIST: 40,
            BUCKET_TICKETS_SEARCH: 40,
            BUCKET_CONTACTS_LIST: 40,
        },
        note=(
            "Some Freshworks docs list 100 calls/min for Growth and others 200; "
            "the connector defaults to the stricter 100 and self-corrects upward "
            "from the X-RateLimit-Total header."
        ),
    ),
    "pro": PlanProfile(
        name="pro",
        overall_per_minute=400,
        bucket_caps={
            BUCKET_TICKETS_LIST: 100,
            BUCKET_TICKETS_SEARCH: 100,
            BUCKET_CONTACTS_LIST: 100,
        },
    ),
    "enterprise": PlanProfile(
        name="enterprise",
        overall_per_minute=700,
        bucket_caps={
            BUCKET_TICKETS_LIST: 200,
            BUCKET_TICKETS_SEARCH: 200,
            BUCKET_CONTACTS_LIST: 200,
        },
    ),
}

# "auto" starts at the strictest real profile and only moves up when Freshdesk's
# own headers prove the account is allowed more.
AUTO_START_PROFILE = "growth"


def classify_bucket(method: str, path: str) -> str:
    """Map a request to the rate-limit bucket Freshdesk counts it against.

    Freshworks publishes tighter caps for the *list* endpoints specifically
    ("Tickets List", "Contacts List"), so single-object reads are charged only
    against the account-wide budget - matching how the platform counts them.
    """
    p = path.lower().rstrip("/")
    get = method.upper() == "GET"
    if "search/tickets" in p:
        return BUCKET_TICKETS_SEARCH
    if p.endswith("/tickets") and get:
        return BUCKET_TICKETS_LIST
    if p.endswith("/contacts") and get:
        return BUCKET_CONTACTS_LIST
    return BUCKET_DEFAULT


@dataclass
class RateLimitHeaders:
    """The three headers Freshdesk returns on *every* response."""

    total: int | None = None
    remaining: int | None = None
    used_current_request: int | None = None
    retry_after: float | None = None

    @classmethod
    def from_headers(cls, headers) -> "RateLimitHeaders":
        def _int(name: str) -> int | None:
            raw = headers.get(name)
            if raw is None:
                return None
            try:
                return int(str(raw).strip())
            except ValueError:
                return None

        def _float(name: str) -> float | None:
            raw = headers.get(name)
            if raw is None:
                return None
            try:
                return float(str(raw).strip())
            except ValueError:
                return None

        return cls(
            total=_int("X-RateLimit-Total"),
            remaining=_int("X-RateLimit-Remaining"),
            used_current_request=_int("X-RateLimit-Used-CurrentRequest"),
            retry_after=_float("Retry-After"),
        )


class RateLimitGovernor:
    """Sliding-window limiter with proactive pacing and adaptive caps."""

    def __init__(
        self,
        plan: str = "auto",
        *,
        safety_margin: float = 0.1,
        reserve: int = 3,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.plan = plan
        self.safety_margin = safety_margin
        self.reserve = reserve
        self._clock = clock
        self._sleep = sleeper
        self._jitter = jitter

        profile = PLAN_PROFILES.get(plan if plan != "auto" else AUTO_START_PROFILE)
        assert profile is not None  # guarded by Config validation
        self._profile = profile

        self._global_calls: deque[float] = deque()
        self._bucket_calls: dict[str, deque[float]] = {}
        self._observed_total: int | None = None
        self._bucket_penalty: dict[str, float] = {}
        self._blocked_until: dict[str, float] = {}
        self._min_interval: float = 0.0
        self.events: list[dict[str, object]] = []
        self.throttle_waits: float = 0.0

    # ------------------------------------------------------------------ caps
    def _overall_cap(self) -> int:
        base = self._profile.overall_per_minute
        if self._observed_total:
            if self.plan == "auto":
                # Freshdesk's own header is authoritative in auto mode: it lets a
                # trial account throttle itself down to 50/min, and lets a Pro or
                # Enterprise account use the ceiling it actually pays for.
                base = self._observed_total
            else:
                # An explicit plan is a human assertion; still never exceed what
                # the account's headers claim.
                base = min(base, self._observed_total)
        # The reserve keeps the account from being drained to zero, but it must
        # scale down for small budgets - reserving 3 calls a minute on a 1/min
        # budget would make the connector useless rather than polite.
        reserve = min(self.reserve, int(base * self.safety_margin))
        return max(1, int(base * (1.0 - self.safety_margin)) - reserve)

    def _bucket_cap(self, bucket: str) -> int:
        documented = self._profile.bucket_caps.get(bucket, self._profile.overall_per_minute)
        if self._observed_total:
            # No endpoint can exceed the account-wide ceiling Freshdesk reports.
            documented = min(documented, max(1, self._observed_total))
        penalty = self._bucket_penalty.get(bucket, 1.0)
        return max(1, int(documented * (1.0 - self.safety_margin) * penalty))

    # --------------------------------------------------------------- observe
    def observe(self, bucket: str, headers: RateLimitHeaders, status_code: int) -> None:
        """Fold response headers into the model of available budget."""
        if headers.total:
            self._observed_total = headers.total
        if headers.remaining is not None and headers.total:
            remaining = headers.remaining
            if remaining <= self.reserve:
                # Spread whatever is left over the rest of the window instead of
                # spending it in one burst and provoking a 429. Capped so a nearly
                # empty budget cannot stall a tool call indefinitely.
                spread = min(10.0, WINDOW_SECONDS / (remaining + 1))
                self._min_interval = max(self._min_interval, spread)
            else:
                self._min_interval = max(0.0, self._min_interval * 0.5)
        if status_code == 429:
            self.note_429(bucket, headers.retry_after)
        elif status_code < 400 and bucket in self._bucket_penalty:
            # Additive recovery: a bucket that 429'd earlier is given its budget
            # back gradually rather than being throttled forever.
            self._bucket_penalty[bucket] = min(1.0, self._bucket_penalty[bucket] * 1.25)

    def note_429(self, bucket: str, retry_after: float | None) -> float:
        """Record a 429. Returns the seconds the caller must wait."""
        wait = (retry_after if retry_after and retry_after > 0 else 20.0) + self._jitter(0.0, 1.5)
        now = self._clock()
        self._blocked_until[bucket] = max(self._blocked_until.get(bucket, 0.0), now + wait)
        # A 429 anywhere is a signal about the whole account: Freshworks warns
        # that ignoring Retry-After can get an IP or key blocked outright.
        self._blocked_until["__global__"] = max(self._blocked_until.get("__global__", 0.0), now + min(wait, 30.0))

        # Adaptive per-endpoint learning. If the account-wide budget still looked
        # healthy when this bucket 429'd, the bucket's own documented cap must be
        # lower than published - so halve our estimate of it. Halving (rather than
        # pinning the cap to what we happened to observe) converges in a few 429s
        # without crippling the connector for the rest of the session.
        self._bucket_penalty[bucket] = max(0.25, self._bucket_penalty.get(bucket, 1.0) * 0.5)

        self.events.append(
            {
                "event": "rate_limited",
                "bucket": bucket,
                "waited_seconds": round(wait, 2),
                "bucket_penalty": self._bucket_penalty[bucket],
                "effective_bucket_cap": self._bucket_cap(bucket),
                "retry_after_header": retry_after,
            }
        )
        return wait

    # --------------------------------------------------------------- acquire
    def _wait_needed(self, bucket: str) -> float:
        now = self._clock()
        waits = [0.0]

        for key in (bucket, "__global__"):
            blocked = self._blocked_until.get(key)
            if blocked and blocked > now:
                waits.append(blocked - now)

        for window in (self._global_calls, self._bucket_calls.get(bucket, deque())):
            while window and now - window[0] >= WINDOW_SECONDS:
                window.popleft()

        if len(self._global_calls) >= self._overall_cap():
            waits.append(WINDOW_SECONDS - (now - self._global_calls[0]))
        calls = self._bucket_calls.get(bucket)
        if calls is not None and len(calls) >= self._bucket_cap(bucket):
            waits.append(WINDOW_SECONDS - (now - calls[0]))

        if self._min_interval > 0 and self._global_calls:
            waits.append(self._min_interval - (now - self._global_calls[-1]))
        return max(waits)

    async def acquire(self, bucket: str, *, deadline: float | None = None) -> None:
        """Reserve one call, sleeping if required.

        Raises :class:`LocalBudgetExceeded` when the required wait would blow the
        caller's deadline, so a tool can return partial results instead of
        hanging the agent.
        """
        wait = self._wait_needed(bucket)
        if wait > 0:
            if deadline is not None and self._clock() + wait > deadline:
                raise LocalBudgetExceeded(
                    f"Would need {wait:.1f}s of rate-limit wait to continue, which exceeds "
                    f"the {self._profile.name} plan budget for this call.",
                    details={
                        "required_wait_seconds": round(wait, 2),
                        "bucket": bucket,
                        "bucket_cap_per_minute": self._bucket_cap(bucket),
                        "plan_profile": self._profile.name,
                    },
                )
            self.throttle_waits += wait
            await self._sleep(wait)
        now = self._clock()
        self._global_calls.append(now)
        self._bucket_calls.setdefault(bucket, deque()).append(now)

    # ---------------------------------------------------------------- status
    def status(self) -> dict[str, object]:
        now = self._clock()

        def _live(window: deque[float]) -> int:
            return len([t for t in window if now - t < WINDOW_SECONDS])

        return {
            "plan_profile": self._profile.name,
            "plan_mode": self.plan,
            "observed_account_limit_per_minute": self._observed_total,
            "effective_account_cap_per_minute": self._overall_cap(),
            "calls_used_this_window": _live(self._global_calls),
            "seconds_until_window_reset": (
                round(WINDOW_SECONDS - (now - self._global_calls[0]), 1) if self._global_calls else 0.0
            ),
            "buckets": {
                b: {
                    "label": BUCKET_PARAM_NAMES.get(b, b),
                    "effective_cap_per_minute": self._bucket_cap(b),
                    "calls_used_this_window": _live(self._bucket_calls.get(b, deque())),
                    "blocked_for_seconds": max(0.0, round(self._blocked_until.get(b, 0.0) - now, 1)),
                    "throttle_penalty": round(self._bucket_penalty.get(b, 1.0), 3),
                }
                for b in (BUCKET_TICKETS_LIST, BUCKET_TICKETS_SEARCH, BUCKET_CONTACTS_LIST, BUCKET_DEFAULT)
            },
            "total_throttle_wait_seconds": round(self.throttle_waits, 2),
            "recent_events": self.events[-10:],
            "plan_note": self._profile.note,
            "sharing_note": (
                "Freshdesk rate limits are applied per account, not per API key, and are shared "
                "with every other integration installed on this helpdesk."
            ),
        }

    @property
    def learned_bucket_caps(self) -> dict[str, int]:
        """Buckets whose effective cap has been revised down after a real 429."""
        return {b: self._bucket_cap(b) for b, p in self._bucket_penalty.items() if p < 1.0}
