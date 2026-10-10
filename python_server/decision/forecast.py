"""Zero-shot / statistical forecast ensemble.

Backends (tried in order, all that succeed are ensembled):

  * ``stat``     — always available: drift + realized-vol quantile bands
  * ``chronos``  — Chronos-Bolt when ``chronos-forecasting`` + torch are installed
  * ``timesfm``  — TimesFM 2.5 when ``timesfm[torch]`` is installed

Heavy models are imported lazily and cached. Missing deps degrade the ensemble
rather than crashing ``/decide``.
"""

from __future__ import annotations

import math
import os
import time
from typing import Optional

import numpy as np

from decision.packet import COVERAGE_TARGET, HORIZON_BARS
from decision.schema import ForecastEnsemble, ModelForecast, ReturnBand

_chronos_pipe = None
_timesfm_model = None
_chronos_error: Optional[str] = None
_timesfm_error: Optional[str] = None


def available_backends() -> dict[str, bool]:
    return {
        "stat": True,
        "chronos": _try_load_chronos()[0] is not None,
        "timesfm": _try_load_timesfm()[0] is not None,
    }


def forecast_closes(
    closes: list[float],
    *,
    horizon: int = HORIZON_BARS,
    timeframe: str = "1",
    coverage_target: float = COVERAGE_TARGET,
) -> ForecastEnsemble:
    if len(closes) < 16:
        raise ValueError(f"need ≥16 closes for a forecast, got {len(closes)}")

    series = np.asarray(closes, dtype=float)
    models: list[ModelForecast] = []

    models.append(_stat_forecast(series, horizon))

    chronos, chronos_err = _try_load_chronos()
    if chronos is not None:
        models.append(_chronos_forecast(chronos, series, horizon))
    elif chronos_err:
        models.append(
            ModelForecast(name="chronos", available=False, detail=chronos_err)
        )

    timesfm, timesfm_err = _try_load_timesfm()
    if timesfm is not None:
        models.append(_timesfm_forecast(timesfm, series, horizon))
    elif timesfm_err:
        models.append(
            ModelForecast(name="timesfm", available=False, detail=timesfm_err)
        )

    usable = [m for m in models if m.available and m.p50 is not None]
    if not usable:
        raise RuntimeError("no forecast backend produced a prediction")

    raw = _ensemble_band(usable)
    agreement = _agreement(usable)
    backend = "+".join(m.name for m in usable)

    return ForecastEnsemble(
        horizon_bars=horizon,
        timeframe=timeframe,
        coverage_target=coverage_target,
        raw=raw,
        calibrated=raw,  # filled in by calibrate.apply_conformal
        agreement=agreement,
        models=models,
        residual_count=0,
        backend=backend,
    )


def _stat_forecast(series: np.ndarray, horizon: int) -> ModelForecast:
    started = time.perf_counter()
    # Log returns; memecoin prices span many orders of magnitude.
    rets = np.diff(np.log(np.clip(series, 1e-12, None)))
    if len(rets) < 8:
        rets = np.array([0.0])

    # Recent window dominates: last 64 bars or half the series.
    window = rets[-min(len(rets), max(32, len(rets) // 2)) :]
    mu = float(np.mean(window))
    sigma = float(np.std(window, ddof=1)) if len(window) > 1 else 0.0
    sigma = max(sigma, 1e-6)

    # Horizon scaling under random-walk assumption.
    mean_h = mu * horizon
    std_h = sigma * math.sqrt(horizon)
    # Rough normal quantiles for 80% central interval.
    z = 1.2815515655446004
    p10 = (math.exp(mean_h - z * std_h) - 1.0) * 100.0
    p50 = (math.exp(mean_h) - 1.0) * 100.0
    p90 = (math.exp(mean_h + z * std_h) - 1.0) * 100.0

    return ModelForecast(
        name="stat",
        available=True,
        p10=round(p10, 4),
        p50=round(p50, 4),
        p90=round(p90, 4),
        detail=f"drift={mu:.6f}/bar vol={sigma:.6f}",
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


def _ensemble_band(models: list[ModelForecast]) -> ReturnBand:
    p10 = float(np.mean([m.p10 for m in models if m.p10 is not None]))
    p50 = float(np.mean([m.p50 for m in models if m.p50 is not None]))
    p90 = float(np.mean([m.p90 for m in models if m.p90 is not None]))
    # Keep ordering sane even if a model inverts bands.
    lo, mid, hi = sorted([p10, p50, p90])
    return ReturnBand(p10=round(lo, 4), p50=round(mid, 4), p90=round(hi, 4))


def _agreement(models: list[ModelForecast]) -> float:
    if len(models) < 2:
        return 1.0
    signs = [1 if (m.p50 or 0) > 0.25 else (-1 if (m.p50 or 0) < -0.25 else 0) for m in models]
    if all(s == signs[0] for s in signs) and signs[0] != 0:
        dir_score = 1.0
    elif all(s == 0 for s in signs):
        dir_score = 0.7
    else:
        dir_score = 0.3

    p50s = np.array([m.p50 for m in models], dtype=float)
    spread = float(np.std(p50s))
    scale = max(float(np.mean(np.abs(p50s))), 1.0)
    mag_score = max(0.0, 1.0 - spread / scale)
    return round(0.6 * dir_score + 0.4 * mag_score, 3)


def _try_load_chronos():
    global _chronos_pipe, _chronos_error
    if _chronos_pipe is not None or _chronos_error is not None:
        return _chronos_pipe, _chronos_error
    if os.getenv("FORECAST_DISABLE_CHRONOS", "").strip() == "1":
        _chronos_error = "disabled by FORECAST_DISABLE_CHRONOS=1"
        return None, _chronos_error
    try:
        import torch
        from chronos import BaseChronosPipeline

        model_id = os.getenv("CHRONOS_MODEL", "amazon/chronos-bolt-mini")
        _chronos_pipe = BaseChronosPipeline.from_pretrained(
            model_id,
            device_map="cpu",
            torch_dtype=torch.float32,
        )
        return _chronos_pipe, None
    except Exception as exc:  # noqa: BLE001
        _chronos_error = f"{type(exc).__name__}: {exc}"
        return None, _chronos_error


def _try_load_timesfm():
    global _timesfm_model, _timesfm_error
    if _timesfm_model is not None or _timesfm_error is not None:
        return _timesfm_model, _timesfm_error
    if os.getenv("FORECAST_DISABLE_TIMESFM", "").strip() == "1":
        _timesfm_error = "disabled by FORECAST_DISABLE_TIMESFM=1"
        return None, _timesfm_error
    try:
        import timesfm
        from timesfm import ForecastConfig

        model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(
            os.getenv("TIMESFM_MODEL", "google/timesfm-2.5-200m-pytorch")
        )
        # TimesFM 2.5 requires compile() before forecast().
        model.compile(
            ForecastConfig(
                max_context=int(os.getenv("TIMESFM_MAX_CONTEXT", "512")),
                max_horizon=int(os.getenv("TIMESFM_MAX_HORIZON", "64")),
                normalize_inputs=True,
                use_continuous_quantile_head=True,
                force_flip_invariance=True,
                infer_is_positive=True,
                fix_quantile_crossing=True,
            )
        )
        _timesfm_model = model
        return _timesfm_model, None
    except Exception as exc:  # noqa: BLE001
        _timesfm_error = f"{type(exc).__name__}: {exc}"
        return None, _timesfm_error


def _chronos_forecast(pipe, series: np.ndarray, horizon: int) -> ModelForecast:
    started = time.perf_counter()
    try:
        import torch

        context = torch.tensor(series[-min(len(series), 512) :], dtype=torch.float32)
        # predict returns quantiles; Chronos-Bolt uses [0.1, 0.5, 0.9] by default
        # in many builds. Fall back gracefully.
        forecast = pipe.predict(context, prediction_length=horizon)
        arr = forecast.detach().cpu().numpy() if hasattr(forecast, "detach") else np.asarray(forecast)
        # Shapes vary: (1, num_quantiles, horizon) or (num_quantiles, horizon)
        arr = np.squeeze(arr)
        if arr.ndim == 1:
            last = float(arr[-1])
            p50_px = last
            p10_px = last
            p90_px = last
        elif arr.ndim == 2:
            # Assume rows are quantiles low→high or fixed [0.1,0.5,0.9]
            q = arr.shape[0]
            mid = q // 2
            p10_px = float(arr[0, -1])
            p50_px = float(arr[mid, -1])
            p90_px = float(arr[-1, -1])
        else:
            raise RuntimeError(f"unexpected chronos shape {arr.shape}")

        last_px = float(series[-1])
        return ModelForecast(
            name="chronos",
            available=True,
            p10=round((p10_px / last_px - 1.0) * 100.0, 4),
            p50=round((p50_px / last_px - 1.0) * 100.0, 4),
            p90=round((p90_px / last_px - 1.0) * 100.0, 4),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
    except Exception as exc:  # noqa: BLE001
        return ModelForecast(
            name="chronos",
            available=False,
            detail=f"{type(exc).__name__}: {exc}",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


def _timesfm_forecast(model, series: np.ndarray, horizon: int) -> ModelForecast:
    started = time.perf_counter()
    try:
        context = series[-min(len(series), 512) :].astype(float)
        # Signature is forecast(horizon, inputs) — keyword order matters.
        point, quantile = model.forecast(horizon=horizon, inputs=[context])
        last_px = float(series[-1])
        point_arr = np.asarray(point)
        p50_px = float(point_arr[0, -1])
        q = np.asarray(quantile)
        if q.ndim == 3:
            # (1, horizon, n_quantiles) — use low / mid / high quantile heads.
            n_q = q.shape[-1]
            p10_px = float(q[0, -1, 0])
            p50_px = float(q[0, -1, n_q // 2])
            p90_px = float(q[0, -1, n_q - 1])
        else:
            p10_px = p50_px
            p90_px = p50_px

        return ModelForecast(
            name="timesfm",
            available=True,
            p10=round((p10_px / last_px - 1.0) * 100.0, 4),
            p50=round((p50_px / last_px - 1.0) * 100.0, 4),
            p90=round((p90_px / last_px - 1.0) * 100.0, 4),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
    except Exception as exc:  # noqa: BLE001
        return ModelForecast(
            name="timesfm",
            available=False,
            detail=f"{type(exc).__name__}: {exc}",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
