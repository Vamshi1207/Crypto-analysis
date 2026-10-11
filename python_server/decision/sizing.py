"""Per-token risk sizing for trenches lanes — dynamic dollars, not flat clips.

Best-practice shape (fixed-fractional + volatility-aware stop):

  risk_usd = base_risk × safety × liquidity × conviction   (clamped)
  stop_frac = base + exit_impact + tape_volatility          (clamped)
  size_usd  = risk_usd / stop_frac                          (clamped + liq cap)

A full stop-out then costs ``risk_usd`` by construction, so a thin illiquid
mint risks fewer dollars than a deep liquid one at the same conviction.
Memecoin books have no usable ATR at open (minutes-old tokens), so the
volatility proxy is exit price impact + recent tape movement instead.

Kelly is deliberately a future ceiling, not today's target: it needs a few
hundred logged trades per setup and the lane books are still accumulating
that log. ``SIZING_ENABLED=0`` restores today's flat size/stop exactly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from decision.config import _env_float

ENABLED = os.getenv("SIZING_ENABLED", "1").strip() == "1"

# Base dollars at risk per strategy when every multiplier is 1.0.
# Defaults match today's implied risk (flat stop on flat size).
RISK_CLUSTER_USD = _env_float("SIZING_RISK_CLUSTER_USD", 1.60)
RISK_LAUNCH_USD = _env_float("SIZING_RISK_LAUNCH_USD", 1.50)
RISK_COPY_USD = _env_float("SIZING_RISK_COPY_USD", 1.60)
RISK_SNIPE_USD = _env_float("SIZING_RISK_SNIPE_USD", 0.80)
MIN_RISK_USD = _env_float("SIZING_MIN_RISK_USD", 0.25)
MAX_RISK_USD = _env_float("SIZING_MAX_RISK_USD", 3.0)

# Stop fraction of size: base + exit impact + tape-vol term, clamped.
STOP_BASE_PCT = _env_float("SIZING_STOP_BASE_PCT", 4.0)
STOP_MIN_PCT = _env_float("SIZING_STOP_MIN_PCT", 2.0)
STOP_MAX_PCT = _env_float("SIZING_STOP_MAX_PCT", 15.0)

# Low floor on purpose: with 10–15% honest stops on thin tape, a $5 floor
# would force 2–3x over-risk on the worst tokens — exactly backwards.
MIN_SIZE_USD = _env_float("SIZING_MIN_SIZE_USD", 2.0)
MAX_SIZE_USD = _env_float("SIZING_MAX_SIZE_USD", 80.0)
# Never own more than this fraction of the pool: the exit is the position.
MAX_LIQ_FRAC = _env_float("SIZING_MAX_LIQ_FRAC", 0.02)


@dataclass(frozen=True)
class SizePlan:
    size_usd: float
    stop_usd: float | None
    risk_usd: float
    stop_frac: float
    why: str


def report_features(report: object) -> tuple[float | None, float | None]:
    """Pull (safety_score, price_impact_pct) off a Gate 0 report, or Nones."""
    if report is None:
        return None, None
    score = getattr(report, "risk_score", None)
    try:
        score = float(score) if score is not None else None
    except (TypeError, ValueError):
        score = None
    impact = getattr(getattr(report, "sellability", None), "price_impact_pct", None)
    try:
        impact = float(impact) if impact is not None else None
    except (TypeError, ValueError):
        impact = None
    return score, impact


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def safety_mult(score: float | None) -> float:
    """Scale risk by Gate 0 risk score (higher score = riskier token)."""
    if score is None:
        return 0.7
    try:
        s = float(score)
    except (TypeError, ValueError):
        return 0.7
    if s < 20:
        return 1.0
    if s < 40:
        return 0.75
    if s < 60:
        return 0.5
    return 0.3


def liquidity_mult(liquidity_usd: float | None) -> float:
    """Scale risk by pool depth. Missing depth reads as thin."""
    if liquidity_usd is None:
        return 0.5
    try:
        liq = float(liquidity_usd)
    except (TypeError, ValueError):
        return 0.5
    if liq <= 0:
        return 0.3
    if liq >= 20_000:
        return 1.0
    if liq >= 8_000:
        return 0.75
    if liq >= 3_000:
        return 0.5
    return 0.3


def plan_size(
    *,
    strategy: str,
    base_risk_usd: float,
    flat_size_usd: float,
    flat_stop_usd: float | None,
    liquidity_usd: float | None = None,
    price_impact_pct: float | None = None,
    safety_score: float | None = None,
    vol_proxy: float = 0.05,
    conviction_mult: float = 1.0,
) -> SizePlan:
    """Risk-size one open. Pure function of its inputs (no I/O).

    * ``vol_proxy`` — recent tape movement as a fraction (m5/100, lift/100).
    * ``conviction_mult`` — caller tape/edge conviction, clamped to 0.25..1.0.
    * flat_* — today's values, returned verbatim when disabled.
    """
    if not ENABLED:
        stop = float(flat_stop_usd) if flat_stop_usd is not None else None
        return SizePlan(
            size_usd=round(float(flat_size_usd), 2),
            stop_usd=round(stop, 2) if stop is not None else None,
            risk_usd=round(stop, 2) if stop is not None else 0.0,
            stop_frac=(
                round(stop / flat_size_usd, 4)
                if stop is not None and flat_size_usd > 0
                else 0.0
            ),
            why="sizing_off",
        )
    try:
        impact_frac = max(float(price_impact_pct or 0.0), 0.0) / 100.0
    except (TypeError, ValueError):
        impact_frac = 0.0
    try:
        vol = _clamp(abs(float(vol_proxy or 0.0)), 0.0, 0.20)
    except (TypeError, ValueError):
        vol = 0.05
    conviction = _clamp(float(conviction_mult or 0.0), 0.25, 1.0)

    risk = _clamp(
        float(base_risk_usd)
        * safety_mult(safety_score)
        * liquidity_mult(liquidity_usd)
        * conviction,
        MIN_RISK_USD,
        MAX_RISK_USD,
    )
    stop_frac = _clamp(
        STOP_BASE_PCT / 100.0 + impact_frac + 0.5 * vol,
        STOP_MIN_PCT / 100.0,
        STOP_MAX_PCT / 100.0,
    )
    size = _clamp(risk / stop_frac, MIN_SIZE_USD, MAX_SIZE_USD)
    try:
        liq = float(liquidity_usd) if liquidity_usd else 0.0
    except (TypeError, ValueError):
        liq = 0.0
    if liq > 0:
        size = min(size, max(liq * MAX_LIQ_FRAC, 1.0))
    size = round(size, 2)
    stop = round(size * stop_frac, 2)
    return SizePlan(
        size_usd=size,
        stop_usd=stop,
        risk_usd=round(risk, 2),
        stop_frac=round(stop_frac, 4),
        why=(
            f"{strategy} risk=${risk:.2f} "
            f"(safety×{safety_mult(safety_score):.2f} "
            f"liq×{liquidity_mult(liquidity_usd):.2f} "
            f"conv×{conviction:.2f}) stop={stop_frac:.1%}"
        ),
    )
