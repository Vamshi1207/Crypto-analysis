"""Technical-indicator alignment for the decision path.

Uses the talipp snapshot (TA-Lib-class indicators) already on the packet.
Forecast + cost edge remain Gate 2. Indicators answer: does the tape agree
with going long?

Missing indicators (lite stubs, short series) → skip, do not block.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from decision.config import _env_float
from decision.schema import Direction

ENABLED = os.getenv("INDICATOR_GATE", "1").strip() == "1"
MIN_BUY_SCORE = _env_float("INDICATOR_MIN_BUY_SCORE", -0.15)
HARD_BLOCK_SCORE = _env_float("INDICATOR_HARD_BLOCK_SCORE", -0.45)


@dataclass
class IndicatorSignal:
    score: float  # -1 bearish … +1 bullish
    votes: dict[str, float]
    available: bool
    reason: str
    hard_block: bool
    soft_ok: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "votes": {k: round(v, 4) for k, v in self.votes.items()},
            "available": self.available,
            "reason": self.reason,
            "hard_block": self.hard_block,
            "soft_ok": self.soft_ok,
        }


def score_indicators(indicators: Optional[dict[str, Any]]) -> IndicatorSignal:
    """Map a scalar indicator snapshot to a directional score in [-1, 1]."""
    if not indicators or indicators.get("error"):
        return IndicatorSignal(
            score=0.0,
            votes={},
            available=False,
            reason="indicators unavailable",
            hard_block=False,
            soft_ok=True,
        )

    votes: dict[str, float] = {}

    rsi = _f(indicators.get("rsi"))
    if rsi is not None:
        if rsi >= 70:
            votes["rsi"] = min(1.0, (rsi - 50) / 30.0)
        elif rsi <= 30:
            votes["rsi"] = max(-1.0, (rsi - 50) / 30.0)
        else:
            votes["rsi"] = (rsi - 50) / 40.0

    hist = _f(indicators.get("macd_hist"))
    if hist is not None:
        votes["macd_hist"] = max(-1.0, min(1.0, hist * 50.0))

    plus_di = _f(indicators.get("plus_di"))
    minus_di = _f(indicators.get("minus_di"))
    adx = _f(indicators.get("adx"))
    if plus_di is not None and minus_di is not None:
        spread = plus_di - minus_di
        strength = 1.0
        if adx is not None:
            strength = max(0.35, min(1.0, adx / 40.0))
        votes["adx_di"] = max(-1.0, min(1.0, (spread / 25.0) * strength))

    stoch_k = _f(indicators.get("stoch_k"))
    if stoch_k is not None:
        votes["stoch_k"] = max(-1.0, min(1.0, (stoch_k - 50.0) / 50.0))

    stoch_rsi_k = _f(indicators.get("stoch_rsi_k"))
    if stoch_rsi_k is not None:
        votes["stoch_rsi"] = max(-1.0, min(1.0, (stoch_rsi_k - 50.0) / 50.0))

    williams = _f(indicators.get("williams_r"))
    if williams is not None:
        # Williams %R: -100 oversold … 0 overbought
        votes["williams"] = max(-1.0, min(1.0, (williams + 50.0) / 50.0))

    cci = _f(indicators.get("cci"))
    if cci is not None:
        votes["cci"] = max(-1.0, min(1.0, cci / 200.0))

    roc = _f(indicators.get("roc"))
    if roc is not None:
        votes["roc"] = max(-1.0, min(1.0, roc / 8.0))

    ao = _f(indicators.get("ao"))
    if ao is not None:
        votes["ao"] = max(-1.0, min(1.0, ao * 20.0))

    boll_pct = _f(indicators.get("boll_percent"))
    if boll_pct is not None:
        votes["boll"] = max(-1.0, min(1.0, (boll_pct - 0.5) * 2.0))

    st_trend = indicators.get("supertrend_trend")
    if st_trend is not None:
        s = str(st_trend).lower()
        if "up" in s or s in ("1", "bull", "bullish"):
            votes["supertrend"] = 0.55
        elif "down" in s or s in ("-1", "bear", "bearish"):
            votes["supertrend"] = -0.55

    ema_cross = indicators.get("ema_cross")
    if isinstance(ema_cross, (int, float)):
        if ema_cross > 0:
            votes["ema_cross"] = 0.55
        elif ema_cross < 0:
            votes["ema_cross"] = -0.55
    elif isinstance(ema_cross, str):
        low = ema_cross.lower()
        if "bull" in low or low in ("up", "golden", "1"):
            votes["ema_cross"] = 0.55
        elif "bear" in low or low in ("down", "death", "-1"):
            votes["ema_cross"] = -0.55

    if not votes:
        return IndicatorSignal(
            score=0.0,
            votes={},
            available=False,
            reason="no usable indicator fields",
            hard_block=False,
            soft_ok=True,
        )

    score = sum(votes.values()) / len(votes)
    return IndicatorSignal(
        score=float(score),
        votes=votes,
        available=True,
        reason=f"n={len(votes)} score={score:+.2f}",
        hard_block=False,
        soft_ok=True,
    )


def evaluate_for_direction(
    indicators: Optional[dict[str, Any]],
    direction: Direction,
) -> IndicatorSignal:
    sig = score_indicators(indicators)
    if not ENABLED or not sig.available:
        sig.reason = sig.reason + ("; gate skipped" if not ENABLED else "")
        return sig

    if direction is not Direction.UP:
        sig.soft_ok = True
        sig.hard_block = False
        sig.reason = f"{sig.reason}; direction={direction.value} (no buy veto)"
        return sig

    if sig.score <= HARD_BLOCK_SCORE:
        sig.hard_block = True
        sig.soft_ok = False
        sig.reason = (
            f"indicators contradict UP ({sig.score:+.2f} ≤ hard {HARD_BLOCK_SCORE})"
        )
        return sig

    if sig.score < MIN_BUY_SCORE:
        sig.hard_block = False
        sig.soft_ok = False
        sig.reason = (
            f"indicators weak/conflict for UP ({sig.score:+.2f} < min {MIN_BUY_SCORE})"
        )
        return sig

    sig.soft_ok = True
    sig.hard_block = False
    sig.reason = f"indicators align with UP ({sig.score:+.2f})"
    return sig


def confidence_multiplier(sig: IndicatorSignal) -> float:
    if not sig.available or not ENABLED:
        return 1.0
    return round(0.925 + 0.225 * max(-1.0, min(1.0, sig.score)), 3)


def _f(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None
