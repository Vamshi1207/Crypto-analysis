"""Continuous OHLCV replay — treat a contiguous corpus window as live data."""

from __future__ import annotations

import os

import pytest

from decision.decide import decide
from decision.replay import longest_continuous_run, pick_continuous_live_token
from decision.schema import Action, DecideMode


pytestmark = pytest.mark.corpus


def test_longest_continuous_run_prefers_regular_gaps():
    # 10 regular 15s bars, then a hole, then 3 more.
    base = 1_000_000_000_000
    candles = []
    for i in range(10):
        candles.append({"timestamp": base + i * 15_000, "open": 1, "high": 1, "low": 1, "close": 1.0 + i, "volume": 1})
    for i in range(3):
        candles.append({"timestamp": base + 1_000_000 + i * 15_000, "open": 1, "high": 1, "low": 1, "close": 2.0, "volume": 1})
    start, end, window = longest_continuous_run(candles, 15_000)
    assert end - start == 10
    assert len(window) == 10


def test_pick_continuous_live_token_has_regular_gaps():
    picked = pick_continuous_live_token(min_bars=64, tail=128, scan_limit=20)
    assert picked["live_bars"] >= 64
    tf = picked["timeframe"]
    candles = picked["live_token"]["timeframes"][tf]
    gaps = [candles[i]["timestamp"] - candles[i - 1]["timestamp"] for i in range(1, len(candles))]
    # All gaps in the live window should be nearly identical (continuous).
    med = sorted(gaps)[len(gaps) // 2]
    assert all(0.5 * med <= g <= 2.0 * med for g in gaps)


def test_decide_on_continuous_live_window():
    os.environ["FORECAST_DISABLE_CHRONOS"] = "1"
    os.environ["FORECAST_DISABLE_TIMESFM"] = "1"
    picked = pick_continuous_live_token(min_bars=128, tail=256, scan_limit=20)
    card = decide(
        address=picked["address"],
        live_token=picked["live_token"],
        mode=DecideMode.FAST,
        timeframe=picked["timeframe"],
        horizon=10,
        skip_safety=True,
        run_safety=False,
    )
    assert card.action in Action
    assert "gate1" in card.gates_passed
    assert "forecast" in card.gates_passed
    assert card.forecast.backend.startswith("stat")
    assert card.latency_ms is not None
