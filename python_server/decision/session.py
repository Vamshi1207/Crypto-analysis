"""Start a clean monitoring session for pipeline performance.

Archives today's JSONL logs (does not delete history), resets paper cash,
clears discover cool-offs, and zeroes arb session counters.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from decision import arb, discover, paper, pipeline_log
from decision import store as decision_store

KINDS = (
    "decisions",
    "paper",
    "outcomes",
    "pipeline",
    "safety",
)

_started_at: Optional[str] = None


def started_at_iso() -> str:
    """UTC ISO timestamp for the active monitoring session."""
    if _started_at is None:
        bootstrap(reason="lazy_init")
    return _started_at  # type: ignore[return-value]


def started_at() -> datetime:
    return datetime.fromisoformat(started_at_iso().replace("Z", "+00:00"))


def bootstrap(*, reason: str = "server_boot") -> str:
    """Begin a new monitoring session (server boot or first touch)."""
    global _started_at
    _started_at = datetime.now(timezone.utc).isoformat()
    try:
        decision_store.append(
            "sessions",
            {"event": "session_start", "reason": reason, "started_at": _started_at},
        )
    except OSError:
        pass
    return _started_at


def begin_session(iso: str) -> None:
    """Mark session boundary (reset board)."""
    global _started_at
    _started_at = iso


def is_since(iso_ts: Optional[str]) -> bool:
    """True when ``iso_ts`` falls inside the active session window."""
    if not iso_ts:
        return False
    try:
        ts = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
    except ValueError:
        return False
    return ts >= started_at()


def read_since(kind: str) -> list[dict[str, Any]]:
    return list(decision_store.read_since(kind, started_at()))


def count_since(kind: str) -> int:
    return decision_store.count_since(kind, started_at())


def reset_board(*, reason: str = "manual") -> dict[str, Any]:
    """Archive today + reset in-memory book for a clean performance window."""
    started = datetime.now(timezone.utc).isoformat()
    begin_session(started)
    archived = decision_store.archive_today(KINDS)
    paper_snap = paper.reset(starting_cash_usd=1000.0)
    discover_cleared = discover.clear_rotation()
    arb_snap = arb.reset_counters()

    marker = {
        "event": "session_reset",
        "reason": reason,
        "started_at": started,
        "archived": archived,
        "paper": {
            "cash_usd": paper_snap.get("cash_usd"),
            "open_count": paper_snap.get("open_count"),
            "closed_count": paper_snap.get("closed_count"),
        },
        "discover_cleared": discover_cleared,
        "arb": {
            "paper_fills": arb_snap.get("paper_fills"),
            "realized_pnl_usd": arb_snap.get("realized_pnl_usd"),
        },
    }
    try:
        decision_store.append("sessions", marker)
    except OSError:
        pass

    pipeline_log.emit(
        "session",
        "reset",
        level="warning",
        reason=reason,
        archived_n=len(archived),
        started_at=started,
    )

    # Force a fresh universe hydrate so trade roster refills after cool-off clear.
    scan: dict[str, Any] = {}
    try:
        scan = discover.scan_once()
    except Exception as exc:  # noqa: BLE001
        scan = {"status": "error", "error": str(exc)}

    return {
        "status": "ok",
        "started_at": started,
        "archived": archived,
        "paper": paper_snap,
        "discover_cleared": discover_cleared,
        "arb": arb_snap,
        "discover_scan": {
            "status": scan.get("status"),
            "trade_n": scan.get("trade_n"),
            "observe_n": scan.get("observe_n"),
            "watchlist_n": len(scan.get("watchlist") or []),
            "error": scan.get("error"),
        },
        "note": (
            "Prior JSONL logs were archived; readiness and trade totals now "
            "count only this session (since reset), not UTC midnight. Paper bankroll reset to $1000."
        ),
    }
