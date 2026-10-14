"""Offline stand-in for the Freshdesk v2 API, built from fictional fixtures.

Used by the test suite and the demo, so neither requires a Freshdesk account, a
network connection, or anyone's rate-limit budget.

The fixtures themselves are pure standard library; the HTTP layer needs FastAPI,
which is why the app objects are imported lazily.
"""

from . import fixtures

__all__ = ["MockSettings", "MockState", "build_app", "fixtures"]

_MOCK_EXTRAS = (
    "The offline mock server needs its optional dependencies. Install them with:\n"
    "    pip install 'freshdesk-mcp-connector[dev]'\n"
    "or, from a checkout:\n"
    "    pip install -r requirements.txt"
)


def __getattr__(name: str):
    if name in {"MockSettings", "MockState", "build_app"}:
        try:
            from . import app as _app
        except ModuleNotFoundError as exc:  # pragma: no cover - depends on environment
            raise ModuleNotFoundError(f"{_MOCK_EXTRAS} (missing: {exc.name})") from exc
        return getattr(_app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
