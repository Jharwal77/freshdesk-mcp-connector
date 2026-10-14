"""Private Freshdesk connector for an Agent Studio agent.

Public surface:

* :class:`~freshdesk_connector.config.Config` - environment-driven configuration.
* :class:`~freshdesk_connector.client.FreshdeskClient` - authenticated, rate-limit
  aware HTTP client with retries, cursors and an audit trail.
* :class:`~freshdesk_connector.service.FreshdeskService` - merchant-facing read
  operations (tickets, conversations, contacts, companies, discovery).
* :func:`~freshdesk_connector.server.create_server` - the MCP server an agent
  connects to.
"""

from .client import FreshdeskClient, Page
from .config import Config
from .errors import (
    AuthError,
    ConfigError,
    FreshdeskError,
    LocalBudgetExceeded,
    NetworkError,
    NotFoundError,
    PermissionDenied,
    RateLimited,
    UpstreamError,
    ValidationError,
    WriteDisabled,
)
from .ratelimit import PLAN_PROFILES, RateLimitGovernor
from .service import FreshdeskService

__version__ = "0.1.0"

__all__ = [
    "AuthError",
    "Config",
    "ConfigError",
    "FreshdeskClient",
    "FreshdeskError",
    "FreshdeskService",
    "LocalBudgetExceeded",
    "NetworkError",
    "NotFoundError",
    "PLAN_PROFILES",
    "Page",
    "PermissionDenied",
    "RateLimited",
    "RateLimitGovernor",
    "UpstreamError",
    "ValidationError",
    "WriteDisabled",
    "__version__",
]
