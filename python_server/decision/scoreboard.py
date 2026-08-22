"""Phase 5 — realized outcome scoreboard vs DecisionCard forecasts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from decision import pipeline_log
from decision import store as decision_store


def record_outcome(
    *,
    address: str,
    mint: Optional[str],
    timeframe: str,
    horizon_bars: int,
    predicted_p50: float,
    predicted_p10: float,
    predicted_p90: float,
    entry_price: float,
    exit_price: float,
    action: str,
) -> dict[str, Any]:
    if entry_price <= 0:
        raise ValueError("entry_price must be > 0")
    realized_pct = (exit_price / entry_price - 1.0) * 100.0
    covered = predicted_p10 <= realized_pct <= predicted_p90
    record = {
        "address": address,
        "mint": mint,
        "timeframe": timeframe,
        "horizon_bars": horizon_bars,
        "action": action,
        "predicted_p50": predicted_p50,
        "predicted_p10": predicted_p10,
        "predicted_p90": predicted_p90,
        "realized_pct": round(realized_pct, 4),
        "error_pct": round(realized_pct - predicted_p50, 4),
        "covered_80": covered,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "scored_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        decision_store.append("outcomes", record)
    except OSError:
        pass
    pipeline_log.emit(
        "scoreboard",
        "outcome",
        address=address,
        mint=mint,
        action=action,
        realized_pct=record["realized_pct"],
        error_pct=record["error_pct"],
        covered_80=covered,
        predicted_p50=predicted_p50,
        timeframe=timeframe,
    )
    return record


def summarize(day=None) -> dict[str, Any]:
    rows = list(decision_store.read("outcomes", day))
    if not rows:
        return {"n": 0, "coverage": None, "mae": None, "mean_error": None, "rows": []}
    n = len(rows)
    covered = sum(1 for r in rows if r.get("covered_80"))
    errors = [float(r.get("error_pct") or 0.0) for r in rows]
    abs_errors = [abs(e) for e in errors]
    return {
        "n": n,
        "coverage": round(covered / n, 3),
        "mae": round(sum(abs_errors) / n, 4),
        "mean_error": round(sum(errors) / n, 4),
        "rows": rows[-50:],
    }


def score_from_live_mark(
    card: dict[str, Any],
    *,
    exit_price: float,
) -> Optional[dict[str, Any]]:
    """Score one logged/decision-like card against a later mark price."""
    targets = card.get("price_targets") or {}
    entry = targets.get("entry")
    if not entry or not exit_price:
        return None
    band = card.get("expected_return_pct") or {}
    tok = card.get("token") or {}
    return record_outcome(
        address=tok.get("address") or "",
        mint=tok.get("mint"),
        timeframe=card.get("timeframe") or "1",
        horizon_bars=int(card.get("horizon_bars") or 0),
        predicted_p50=float(band.get("p50") or 0.0),
        predicted_p10=float(band.get("p10") or 0.0),
        predicted_p90=float(band.get("p90") or 0.0),
        entry_price=float(entry),
        exit_price=float(exit_price),
        action=str(card.get("action") or ""),
    )
