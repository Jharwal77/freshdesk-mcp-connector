"""Configuration loading for the connector.

Credentials never leave this module: :meth:`Config.redacted` is the only shape
that is safe to log or to return from a tool.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

# Valid values for FRESHDESK_PLAN.
PLANS = ("auto", "trial", "growth", "pro", "enterprise")

_TRUE = {"1", "true", "yes", "on"}


def load_dotenv(path: str | Path = ".env", *, override: bool = False) -> dict[str, str]:
    """Minimal .env reader so the connector has no extra runtime dependency.

    Supports ``KEY=value``, ``export KEY=value``, ``#`` comments, and single or
    double quoted values. Values already present in the environment win unless
    ``override`` is set.
    """
    loaded: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return loaded
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in _TRUE


def _as_float(value: str | None, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError as exc:  # pragma: no cover - guarded by tests
        raise ConfigError(f"Expected a number, got {value!r}") from exc


def _as_int(value: str | None, default: int) -> int:
    return int(_as_float(value, float(default)))


@dataclass(frozen=True)
class Config:
    """Validated runtime configuration."""

    domain: str
    api_key: str
    base_url: str
    plan: str = "auto"
    timeout: float = 15.0
    max_retries: int = 3
    request_deadline: float = 25.0
    enable_writes: bool = False
    default_compact: bool = True
    audit_log: Path | None = None
    extras: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------ helpers
    @property
    def portal_url(self) -> str:
        return f"https://{self.domain}.freshdesk.com"

    @classmethod
    def from_env(cls, env_path: str | Path | None = ".env", **overrides: object) -> "Config":
        """Build config from ``.env`` + process environment + explicit overrides."""
        if env_path is not None:
            load_dotenv(env_path)
        env = os.environ

        domain = str(overrides.get("domain") or env.get("FRESHDESK_DOMAIN") or "").strip()
        api_key = str(overrides.get("api_key") or env.get("FRESHDESK_API_KEY") or "").strip()

        if not domain:
            raise ConfigError("FRESHDESK_DOMAIN is not set (expected your subdomain, e.g. 'acme').")
        if not api_key:
            raise ConfigError("FRESHDESK_API_KEY is not set. Generate one under Profile Settings > API Key.")

        domain = domain.removeprefix("https://").removeprefix("http://").split("/")[0]
        if domain.endswith(".freshdesk.com"):
            domain = domain[: -len(".freshdesk.com")]

        plan = str(overrides.get("plan") or env.get("FRESHDESK_PLAN") or "auto").strip().lower()
        if plan not in PLANS:
            raise ConfigError(f"FRESHDESK_PLAN must be one of {PLANS}; got {plan!r}.")

        base_url = str(overrides.get("base_url") or env.get("FRESHDESK_BASE_URL") or "").strip()
        if not base_url:
            base_url = f"https://{domain}.freshdesk.com"
        else:
            # Allow pointing at a local mock such as http://127.0.0.1:8099
            base_url = base_url.rstrip("/")
            if base_url.endswith("/api/v2"):
                base_url = base_url[: -len("/api/v2")]

        audit_raw = overrides.get("audit_log") or env.get("FRESHDESK_AUDIT_LOG")
        audit_log = Path(str(audit_raw)).expanduser() if audit_raw else None

        return cls(
            domain=domain,
            api_key=api_key,
            base_url=base_url,
            plan=plan,
            timeout=_as_float(env.get("FRESHDESK_TIMEOUT"), 15.0),
            max_retries=_as_int(env.get("FRESHDESK_MAX_RETRIES"), 3),
            request_deadline=_as_float(env.get("FRESHDESK_REQUEST_DEADLINE"), 25.0),
            enable_writes=_as_bool(env.get("FRESHDESK_ENABLE_WRITES"), False),
            default_compact=_as_bool(env.get("FRESHDESK_DEFAULT_COMPACT"), True),
            audit_log=audit_log,
        )

    def redacted(self) -> dict[str, object]:
        """Safe-to-log view. Never returns the API key."""
        return {
            "domain": self.domain,
            "base_url": self.base_url,
            "plan_profile": self.plan,
            "timeout_seconds": self.timeout,
            "max_retries": self.max_retries,
            "request_deadline_seconds": self.request_deadline,
            "reads_only": not self.enable_writes,
            "api_key": "***set***" if self.api_key else "***missing***",
            "audit_log": str(self.audit_log) if self.audit_log else None,
        }
