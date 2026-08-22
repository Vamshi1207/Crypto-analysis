"""Conformal calibration of forecast intervals from logged residuals.

This is calibration on forecast-vs-realized errors — not model training.
Residuals are collected offline from the OHLCV corpus (and later from live
shadow logs) and stored as a sorted absolute-error sample. At decision time
we widen/narrow the raw p10/p90 so the stated coverage matches history.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional

import numpy as np

from decision import dataset
from decision.forecast import forecast_closes
from decision.packet import COVERAGE_TARGET, HORIZON_BARS
from decision.schema import ForecastEnsemble, ReturnBand

STORE_DIR = Path(os.getenv("DECISION_STORE_DIR", "/app/store"))
RESIDUALS_PATH = STORE_DIR / "calibration" / "residuals.json"

_lock = threading.Lock()
_cache: Optional[dict] = None


def residuals_path() -> Path:
    return RESIDUALS_PATH


def load_residuals() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        if not RESIDUALS_PATH.exists():
            _cache = {"coverage_target": COVERAGE_TARGET, "horizon": HORIZON_BARS, "errors": []}
            return _cache
        with RESIDUALS_PATH.open("r", encoding="utf-8") as handle:
            _cache = json.load(handle)
        return _cache


def save_residuals(payload: dict) -> Path:
    global _cache
    RESIDUALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        with RESIDUALS_PATH.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        _cache = payload
    return RESIDUALS_PATH


def apply_conformal(
    ensemble: ForecastEnsemble,
    *,
    coverage_target: Optional[float] = None,
) -> ForecastEnsemble:
    """Widen raw bands using the empirical residual quantile.

    The calibrated interval is the *union* of the raw ensemble spread and the
    symmetric conformal band around p50. Calibration may only ever widen: the
    residual sample is a floor on our uncertainty, never a ceiling. Replacing
    the raw spread outright lets a fixed radius collapse a wildly disagreeing
    ensemble into a confident one — and when p50 exceeds the radius the whole
    band lands on one side of zero, implying loss is impossible.

    If we have fewer than 30 residuals, return the raw band unchanged and note
    residual_count=0 so callers know coverage is not yet empirical.
    """
    target = coverage_target or ensemble.coverage_target
    store = load_residuals()
    errors = [float(e) for e in store.get("errors") or []]
    if len(errors) < 30:
        return ensemble.model_copy(
            update={
                "calibrated": ensemble.raw,
                "residual_count": len(errors),
                "calibration_basis": f"raw_passthrough (n={len(errors)}<30)",
            }
        )

    # Absolute residual quantile at the desired coverage (symmetric interval).
    q = float(np.quantile(errors, target))
    raw = ensemble.raw
    conformal_lo, conformal_hi = raw.p50 - q, raw.p50 + q
    lo, hi = min(raw.p10, conformal_lo), max(raw.p90, conformal_hi)

    if lo < conformal_lo or hi > conformal_hi:
        basis = f"union(raw_spread, conformal_q{target:.0%}={q:.2f}%)"
    else:
        basis = f"conformal_q{target:.0%}={q:.2f}%"
    fit_tf = store.get("timeframe")
    if fit_tf and fit_tf != ensemble.timeframe:
        basis += f" [residuals fit on tf={fit_tf}, not {ensemble.timeframe}]"

    calibrated = ReturnBand(
        p10=round(lo, 4),
        p50=round(raw.p50, 4),
        p90=round(hi, 4),
    )
    return ensemble.model_copy(
        update={
            "calibrated": calibrated,
            "coverage_target": target,
            "residual_count": len(errors),
            "calibration_basis": basis,
        }
    )


def fit_from_corpus(
    *,
    limit_tokens: int = 8,
    timeframe: str = "1",
    horizon: int = HORIZON_BARS,
    context: int = 128,
    step: int = 16,
    coverage_target: float = COVERAGE_TARGET,
    max_windows_per_token: int = 40,
) -> dict:
    """Walk historical OHLCV, forecast, score vs realized move, save residuals.

    Uses the statistical backend only so fitting does not require torch and
    finishes in seconds on the candle corpus.
    """
    # Force-disable heavy backends for calibration speed and determinism.
    os.environ.setdefault("FORECAST_DISABLE_CHRONOS", "1")
    os.environ.setdefault("FORECAST_DISABLE_TIMESFM", "1")

    tokens = sorted(dataset.list_tokens(), key=lambda t: t.size_bytes)[:limit_tokens]
    errors: list[float] = []
    used = 0

    for token in tokens:
        _, candles = dataset.load_timeframe(token.candles_path, timeframe, tail=context + horizon + step * max_windows_per_token)
        closes = dataset.closes(candles)
        if len(closes) < context + horizon + 1:
            continue

        windows = 0
        start = context
        while start + horizon < len(closes) and windows < max_windows_per_token:
            ctx = closes[start - context : start]
            realized = (closes[start + horizon - 1] / closes[start - 1] - 1.0) * 100.0
            try:
                ens = forecast_closes(ctx, horizon=horizon, timeframe=timeframe)
            except Exception:
                start += step
                continue
            pred = ens.raw.p50
            errors.append(abs(realized - pred))
            windows += 1
            start += step
        used += 1

    payload = {
        "coverage_target": coverage_target,
        "horizon": horizon,
        "timeframe": timeframe,
        "tokens_used": used,
        "errors": errors,
        "error_p50": float(np.median(errors)) if errors else None,
        "error_p80": float(np.quantile(errors, 0.8)) if errors else None,
        "error_p90": float(np.quantile(errors, 0.9)) if errors else None,
        "n": len(errors),
    }
    save_residuals(payload)
    return payload


def coverage_check(
    *,
    limit_tokens: int = 5,
    timeframe: str = "1",
    horizon: int = HORIZON_BARS,
    context: int = 128,
    step: int = 20,
    max_windows_per_token: int = 25,
) -> dict:
    """Empirical coverage of calibrated 80% intervals on held-out windows."""
    os.environ.setdefault("FORECAST_DISABLE_CHRONOS", "1")
    os.environ.setdefault("FORECAST_DISABLE_TIMESFM", "1")

    tokens = sorted(dataset.list_tokens(), key=lambda t: t.size_bytes)[:limit_tokens]
    hits = 0
    total = 0

    for token in tokens:
        _, candles = dataset.load_timeframe(
            token.candles_path,
            timeframe,
            tail=context + horizon + step * max_windows_per_token,
        )
        closes = dataset.closes(candles)
        if len(closes) < context + horizon + 1:
            continue
        start = context
        windows = 0
        while start + horizon < len(closes) and windows < max_windows_per_token:
            ctx = closes[start - context : start]
            realized = (closes[start + horizon - 1] / closes[start - 1] - 1.0) * 100.0
            try:
                ens = apply_conformal(forecast_closes(ctx, horizon=horizon, timeframe=timeframe))
            except Exception:
                start += step
                continue
            lo, hi = ens.calibrated.p10, ens.calibrated.p90
            if lo <= realized <= hi:
                hits += 1
            total += 1
            windows += 1
            start += step

    rate = (hits / total) if total else None
    return {
        "hits": hits,
        "total": total,
        "coverage": round(rate, 4) if rate is not None else None,
        "target": COVERAGE_TARGET,
        "residuals": load_residuals().get("n", 0),
    }
