"""Deprecated shim — use `decision.agy_cli` (Antigravity CLI)."""

from decision.agy_cli import (  # noqa: F401
    AgyStatus,
    AgyUnavailable,
    GeminiStatus,
    GeminiUnavailable,
    ask,
    ask_json,
    preflight,
)
