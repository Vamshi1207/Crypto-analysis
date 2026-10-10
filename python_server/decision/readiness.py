"""Live-readiness scorecard from paper logs.

Answers: are we ready to turn ``LIVE_TRADING=1``? Scoring is explicit and
conservative — speed subscriptions do not change this. Paper must clear the
bars below first.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

from decision import paper, scoreboard, session
from decision import store as decision_store
from decision.config import _env_float, _env_int

# Go/no-go thresholds (overridable via env for paper learning).
MIN_SCALP_CLOSES = _env_int("READY_MIN_SCALP_CLOSES", 50)
MIN_ARB_FILLS = _env_int("READY_MIN_ARB_FILLS", 30)
MIN_DECISIONS = _env_int("READY_MIN_DECISIONS", 200)
MIN_SCOREBOARD_N = _env_int("READY_MIN_SCOREBOARD_N", 30)
MIN_SCALP_EV_USD = _env_float("READY_MIN_SCALP_EV_USD", 0.0)
MIN_ARB_EV_USD = _env_float("READY_MIN_ARB_EV_USD", 0.0)
MIN_COVERAGE = _env_float("READY_MIN_COVERAGE", 0.55)
MAX_MAE = _env_float("READY_MAX_MAE", 8.0)


def evaluate(*, day=None) -> dict[str, Any]:
    """Compute readiness checklist + overall go/no-go (current session window)."""
    _ = day  # legacy arg; metrics are session-scoped
    session_started = session.started_at_iso()

    decisions = session.read_since("decisions")
    paper_rows = session.read_since("paper")
    outcomes = session.read_since("outcomes")
    pipeline_n = session.count_since("pipeline")

    scalp_closes = [
        r
        for r in paper_rows
        if r.get("event") == "close"
        or (isinstance(r.get("position"), dict) and r.get("event") == "close")
    ]
    # Prefer explicit close events; fall back to closed positions embedded.
    if not scalp_closes:
        scalp_closes = [r for r in paper_rows if r.get("event") == "close"]

    scalp_pnls: list[float] = []
    for r in paper_rows:
        if r.get("event") != "close":
            continue
        pos = r.get("position") or {}
        try:
            scalp_pnls.append(float(pos.get("realized_pnl_usd") or r.get("realized_pnl_usd") or 0.0))
        except (TypeError, ValueError):
            continue

    arb_fills = [r for r in paper_rows if r.get("event") == "paper_arb"]
    arb_pnls = []
    for r in arb_fills:
        try:
            arb_pnls.append(float(r.get("realized_pnl_usd") or 0.0))
        except (TypeError, ValueError):
            continue

    actions: dict[str, int] = {}
    for d in decisions:
        a = d.get("action")
        if a is None and isinstance(d.get("card"), dict):
            a = d["card"].get("action")
        key = str(a or "unknown")
        actions[key] = actions.get(key, 0) + 1

    buys = actions.get("buy", 0)
    holds = actions.get("hold", 0)
    avoids = actions.get("avoid", 0)
    n_dec = len(decisions)
    buy_rate = (buys / n_dec) if n_dec else 0.0

    sb = scoreboard.summarize_rows(outcomes)
    # Prefer directional (non-arb) outcomes for coverage math when possible.
    dir_outcomes = [o for o in outcomes if o.get("action") != "paper_arb"]
    if dir_outcomes:
        cov = sum(1 for o in dir_outcomes if o.get("covered_80")) / max(1, len(dir_outcomes))
        errs = [abs(float(o.get("error_pct") or 0.0)) for o in dir_outcomes]
        mae = sum(errs) / len(errs) if errs else None
        sb_n = len(dir_outcomes)
    else:
        cov = sb.get("coverage")
        mae = sb.get("mae")
        sb_n = int(sb.get("n") or 0)

    scalp_n = len(scalp_pnls)
    scalp_ev = (sum(scalp_pnls) / scalp_n) if scalp_n else None
    scalp_sum = sum(scalp_pnls) if scalp_pnls else 0.0

    arb_n = len(arb_pnls)
    arb_ev = (sum(arb_pnls) / arb_n) if arb_n else None
    arb_sum = sum(arb_pnls) if arb_pnls else 0.0

    port = paper.snapshot()
    port_arb_fills = int(port.get("arb_fills") or 0)
    if port_arb_fills > 0:
        arb_n = port_arb_fills
        arb_sum = float(port.get("arb_realized_pnl_usd") or 0.0)
        arb_ev = (arb_sum / arb_n) if arb_n else None

    live_trading = os.getenv("LIVE_TRADING", "0").strip() == "1"

    checks = [
        _check(
            "scalp_sample",
            scalp_n >= MIN_SCALP_CLOSES,
            f"{scalp_n}/{MIN_SCALP_CLOSES} directional paper closes",
            weight=2,
        ),
        _check(
            "scalp_ev",
            scalp_ev is not None and scalp_ev >= MIN_SCALP_EV_USD,
            (
                f"mean scalp PnL ${scalp_ev:+.3f} (need ≥ ${MIN_SCALP_EV_USD:.2f})"
                if scalp_ev is not None
                else "no scalp closes yet"
            ),
            weight=3,
        ),
        _check(
            "arb_sample",
            arb_n >= MIN_ARB_FILLS,
            f"{arb_n}/{MIN_ARB_FILLS} paper arb fills",
            weight=1,
        ),
        _check(
            "arb_ev",
            arb_ev is not None and arb_ev >= MIN_ARB_EV_USD,
            (
                f"mean arb PnL ${arb_ev:+.3f} (need ≥ ${MIN_ARB_EV_USD:.2f})"
                if arb_ev is not None
                else "no arb fills yet"
            ),
            weight=2,
        ),
        _check(
            "decisions_volume",
            n_dec >= MIN_DECISIONS,
            f"{n_dec}/{MIN_DECISIONS} bot opinions this session",
            weight=1,
        ),
        _check(
            "scoreboard_n",
            sb_n >= MIN_SCOREBOARD_N,
            f"{sb_n}/{MIN_SCOREBOARD_N} scored outcomes",
            weight=2,
        ),
        _check(
            "forecast_coverage",
            cov is not None and cov >= MIN_COVERAGE,
            (
                f"coverage {cov:.0%} (need ≥ {MIN_COVERAGE:.0%})"
                if cov is not None
                else "no coverage yet"
            ),
            weight=2,
        ),
        _check(
            "forecast_mae",
            mae is not None and mae <= MAX_MAE,
            f"MAE {mae:.2f} (need ≤ {MAX_MAE:.1f})" if mae is not None else "no MAE yet",
            weight=1,
        ),
        _check(
            "live_flag_off",
            not live_trading,
            "LIVE_TRADING still 0 (safe)" if not live_trading else "LIVE_TRADING=1 already on",
            weight=1,
        ),
        _check(
            "kill_switch_off",
            not bool(port.get("kill_switch")),
            "kill switch off" if not port.get("kill_switch") else "kill switch ON",
            weight=1,
        ),
    ]

    # Soft insight: buy rate not stuck at 0 forever (not a hard gate).
    insights = []
    if n_dec >= 50 and buy_rate < 0.01:
        insights.append(
            "buy rate ~0% — Gate 2 may be starving the scalp sample; rotation/universe still learning"
        )
    if scalp_n == 0 and arb_n > 0:
        insights.append("arb paper is producing fills; directional scalp still has no closes")
    if sb_n and cov is not None and cov < 0.5:
        insights.append("forecast intervals under-cover realized moves — refit /calibrate")

    passed = sum(1 for c in checks if c["pass"])
    weighted = sum(c["weight"] for c in checks if c["pass"])
    weight_total = sum(c["weight"] for c in checks)
    score = round(100.0 * weighted / max(1, weight_total), 1)
    # Hard blockers for "go"
    critical = {"scalp_sample", "scalp_ev", "scoreboard_n", "live_flag_off"}
    critical_ok = all(c["pass"] for c in checks if c["id"] in critical)
    ready = critical_ok and score >= 75.0 and (scalp_ev or 0) >= MIN_SCALP_EV_USD

    session_stats = {
        "decisions": n_dec,
        "actions": actions,
        "buy_rate": round(buy_rate, 4),
        "scalp_closes": scalp_n,
        "scalp_pnl_sum": round(scalp_sum, 4),
        "scalp_ev": None if scalp_ev is None else round(scalp_ev, 4),
        "arb_fills": arb_n,
        "arb_pnl_sum": round(arb_sum, 4),
        "arb_ev": None if arb_ev is None else round(arb_ev, 4),
        "scoreboard_n": sb_n,
        "coverage": None if cov is None else round(float(cov), 3),
        "mae": None if mae is None else round(float(mae), 4),
        "pipeline_events": pipeline_n,
        "paper_open": port.get("open_count"),
        "paper_realized": port.get("realized_pnl_usd"),
        "paper_arb_fills": port.get("arb_fills"),
        "paper_arb_realized": port.get("arb_realized_pnl_usd"),
        "paper_arb_notional": port.get("arb_notional_usd"),
    }

    return {
        "ready_for_live": ready,
        "score": score,
        "passed": passed,
        "total_checks": len(checks),
        "checks": checks,
        "insights": insights,
        "session_started_at": session_started,
        "thresholds": {
            "min_scalp_closes": MIN_SCALP_CLOSES,
            "min_arb_fills": MIN_ARB_FILLS,
            "min_decisions": MIN_DECISIONS,
            "min_scoreboard_n": MIN_SCOREBOARD_N,
            "min_scalp_ev_usd": MIN_SCALP_EV_USD,
            "min_arb_ev_usd": MIN_ARB_EV_USD,
            "min_coverage": MIN_COVERAGE,
            "max_mae": MAX_MAE,
            "min_score_pct": 75.0,
        },
        "session": session_stats,
        "today": session_stats,
        "recommendation": (
            "Looks ready for a tiny real-money trial — but only after you personally review the paper results. Keep sizes tiny."
            if ready
            else "Keep practicing with fake money. Do not turn on live trading yet — not enough proof the bot is consistently making money after fees."
            if score < 50
            else "Getting there. Let it run longer so you have more finished trades to trust the average profit."
        ),
        "plain_english": {
            "what_this_is": (
                "A practice scorecard. It asks: with fake money, are the bot's ideas usually "
                "profitable after fees? Only then consider real money."
            ),
            "score_means": (
                "Higher is better. Passing the important checks (enough finished quick trades, "
                "average profit not negative, live trading still off) matters most."
            ),
        },
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }


def _check(cid: str, ok: bool, detail: str, *, weight: int = 1) -> dict[str, Any]:
    return {"id": cid, "pass": bool(ok), "detail": detail, "weight": weight}
