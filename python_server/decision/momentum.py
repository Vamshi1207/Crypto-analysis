"""Tape-based entry signal for memecoin scalps.

The conformal forecast answers "what does the recent distribution imply?", which
is the right question for mean-reverting tape and the wrong one for a token that
just broke out on rising volume: the median of a quiet series never predicts the
move that is already starting. Gate 2 therefore rejects every breakout until the
move has already happened.

This module scores the tape directly — recent thrust, breakout versus the prior
range, volume expansion, and buy/sell imbalance — so a strong setup can enter on
evidence the forecast structurally cannot see. It is deliberately strict: a
momentum entry bypasses the forecast hurdle, so it must clear several
independent conditions rather than one.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from decision.config import _env_float, _env_int, paper_risk_on

ENABLED = os.getenv("MOMENTUM_ENTRY", "0").strip() == "1"
# Bars used to measure the current thrust.
THRUST_BARS = _env_int("MOMENTUM_THRUST_BARS", 3)
# Bars of prior range the breakout must clear.
RANGE_BARS = _env_int("MOMENTUM_RANGE_BARS", 20)
# Minimum percent gain over the thrust window.
MIN_THRUST_PCT = _env_float("MOMENTUM_MIN_THRUST_PCT", 1.2)
# Price must sit at least this far above the prior range high (percent).
MIN_BREAKOUT_PCT = _env_float("MOMENTUM_MIN_BREAKOUT_PCT", 0.1)
# Recent volume must be this multiple of the prior average.
MIN_VOLUME_RATIO = _env_float("MOMENTUM_MIN_VOLUME_RATIO", 1.3)
# Buy/sell count ratio floor when order flow is available.
MIN_BUY_SELL_RATIO = _env_float("MOMENTUM_MIN_BUY_SELL_RATIO", 1.1)
# Reject entries this far above the range high — the move is already extended.
MAX_EXTENSION_PCT = _env_float("MOMENTUM_MAX_EXTENSION_PCT", 25.0)
# Composite score needed to fire.
MIN_SCORE = _env_float("MOMENTUM_MIN_SCORE", 0.55)


def evaluate(
    candles: list[dict[str, Any]],
    *,
    orderflow: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Score the tape for a long momentum entry.

    Returns ``{"enter": bool, "score": float, "reason": str, ...}``. ``enter`` is
    only true when every hard condition passes *and* the composite score clears
    ``MIN_SCORE``.
    """
    closes, volumes = _series(candles)
    need = RANGE_BARS + THRUST_BARS
    if len(closes) < need:
        return _no(f"need {need} bars, have {len(closes)}")

    price = closes[-1]
    if price <= 0:
        return _no("no price")

    thrust_from = closes[-(THRUST_BARS + 1)]
    thrust_pct = ((price / thrust_from) - 1.0) * 100.0 if thrust_from > 0 else 0.0

    prior = closes[-(need):-THRUST_BARS]
    range_high = max(prior)
    range_low = min(prior)
    breakout_pct = ((price / range_high) - 1.0) * 100.0 if range_high > 0 else 0.0

    recent_vol = _mean(volumes[-THRUST_BARS:])
    prior_vol = _mean(volumes[-(need):-THRUST_BARS])
    # No volume data (sampled feeds can report zeros) is neutral, not a veto.
    volume_ratio = (recent_vol / prior_vol) if prior_vol > 0 else None

    ratio = _buy_sell_ratio(orderflow)

    checks: dict[str, bool] = {
        "thrust": thrust_pct >= MIN_THRUST_PCT,
        "breakout": breakout_pct >= MIN_BREAKOUT_PCT,
        "not_extended": breakout_pct <= MAX_EXTENSION_PCT,
        "volume": volume_ratio is None or volume_ratio >= MIN_VOLUME_RATIO,
        "flow": ratio is None or ratio >= MIN_BUY_SELL_RATIO,
    }

    score = _score(
        thrust_pct=thrust_pct,
        breakout_pct=breakout_pct,
        volume_ratio=volume_ratio,
        buy_sell_ratio=ratio,
    )

    failed = [name for name, ok in checks.items() if not ok]
    detail = {
        "score": round(score, 4),
        "thrust_pct": round(thrust_pct, 4),
        "breakout_pct": round(breakout_pct, 4),
        "range_high": range_high,
        "range_low": range_low,
        "volume_ratio": None if volume_ratio is None else round(volume_ratio, 3),
        "buy_sell_ratio": None if ratio is None else round(ratio, 3),
        "checks": checks,
    }

    if failed:
        primary = {**detail, "enter": False, "reason": "failed " + ",".join(failed), "path": "breakout"}
    elif score < MIN_SCORE:
        primary = {**detail, "enter": False, "reason": f"score {score:.2f} < {MIN_SCORE:.2f}", "path": "breakout"}
    else:
        return {
            **detail,
            "enter": True,
            "path": "breakout",
            "reason": (
                f"thrust {thrust_pct:+.2f}% over {THRUST_BARS} bars, "
                f"{breakout_pct:+.2f}% above {RANGE_BARS}-bar high, score {score:.2f}"
            ),
        }

    if paper_risk_on():
        cont = _continuation(closes, volumes, orderflow)
        if cont.get("enter"):
            return cont
    return primary


def _continuation(
    closes: list[float],
    volumes: list[float],
    orderflow: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Paper-risk grind-up: buy strength that is already printing.

    Memecoins that pay $1 on a $150 clip often grind +0.4–1.5% over a handful of
    bars without clearing a 20-bar high. The breakout path misses those.
    """
    lookback = 8
    if len(closes) < lookback:
        return _no(f"continuation needs {lookback} bars")

    price = closes[-1]
    window = 4
    start = closes[-(window + 1)]
    thrust_pct = ((price / start) - 1.0) * 100.0 if start > 0 else 0.0
    pairs = list(zip(closes[-(window + 1) : -1], closes[-window:]))
    greens = sum(1 for prev, cur in pairs if cur > prev)

    prior = closes[:-3] if len(closes) > 8 else closes[:-1]
    range_high = max(prior) if prior else price
    extension_pct = ((price / range_high) - 1.0) * 100.0 if range_high > 0 else 0.0
    ratio = _buy_sell_ratio(orderflow)

    min_thrust = 0.35
    max_ext = MAX_EXTENSION_PCT
    if thrust_pct < min_thrust:
        return {**_no(f"continuation thrust {thrust_pct:+.2f}%"), "path": "continuation"}
    if greens < 3:
        return {**_no(f"continuation greens {greens}/4"), "path": "continuation"}
    if extension_pct > max_ext:
        return {**_no(f"continuation extended {extension_pct:+.2f}%"), "path": "continuation"}
    if ratio is not None and ratio < MIN_BUY_SELL_RATIO:
        return {**_no("continuation flow"), "path": "continuation"}

    score = _clamp(thrust_pct / 1.5) * 0.6 + _clamp(greens / 4.0) * 0.4
    return {
        "enter": True,
        "path": "continuation",
        "score": round(score, 4),
        "thrust_pct": round(thrust_pct, 4),
        "breakout_pct": round(extension_pct, 4),
        "volume_ratio": None,
        "buy_sell_ratio": None if ratio is None else round(ratio, 3),
        "reason": (
            f"continuation {thrust_pct:+.2f}% / {greens} green bars, "
            f"ext {extension_pct:+.2f}%"
        ),
        "checks": {"thrust": True, "greens": True, "not_extended": True, "flow": True},
    }


def _score(
    *,
    thrust_pct: float,
    breakout_pct: float,
    volume_ratio: Optional[float],
    buy_sell_ratio: Optional[float],
) -> float:
    """Blend the four signals into 0..1. Missing inputs score neutral."""
    thrust = _clamp(thrust_pct / (MIN_THRUST_PCT * 3.0))
    breakout = _clamp(breakout_pct / max(MIN_BREAKOUT_PCT * 10.0, 1.0))
    volume = 0.5 if volume_ratio is None else _clamp(volume_ratio / (MIN_VOLUME_RATIO * 2.0))
    flow = 0.5 if buy_sell_ratio is None else _clamp(buy_sell_ratio / (MIN_BUY_SELL_RATIO * 2.0))
    return 0.35 * thrust + 0.25 * breakout + 0.20 * volume + 0.20 * flow


def _series(candles: list[dict[str, Any]]) -> tuple[list[float], list[float]]:
    closes: list[float] = []
    volumes: list[float] = []
    for row in candles or []:
        if not isinstance(row, dict):
            continue
        try:
            close = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue
        closes.append(close)
        try:
            volumes.append(float(row.get("volume") or 0.0))
        except (TypeError, ValueError):
            volumes.append(0.0)
    return closes, volumes


def _buy_sell_ratio(orderflow: Optional[dict[str, Any]]) -> Optional[float]:
    if not isinstance(orderflow, dict):
        return None
    for key in ("buy_sell_ratio_5m", "buy_sell_ratio", "buy_sell_ratio_1m"):
        raw = orderflow.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _mean(rows: list[float]) -> float:
    return (sum(rows) / len(rows)) if rows else 0.0


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _no(reason: str) -> dict[str, Any]:
    return {"enter": False, "reason": reason, "score": 0.0}
