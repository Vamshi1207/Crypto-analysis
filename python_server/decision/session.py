"""Start a clean monitoring session for pipeline performance.

Archives today's JSONL logs (does not delete history), resets paper cash,
clears discover cool-offs, and zeroes arb session counters.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from decision import arb, discover, paper, pipeline_log
from decision import store as decision_store


KINDS = (
    "decisions",
    "paper",
    "outcomes",
    "pipeline",
    "safety",
)


def reset_board(*, reason: str = "manual") -> dict[str, Any]:
    """Archive today + reset in-memory book for a clean performance window."""
    started = datetime.now(timezone.utc).isoformat()
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
            "Today's prior JSONL logs were archived; readiness/scoreboard now "
            "reflect only post-reset activity. Paper bankroll reset to $1000."
        ),
    }
