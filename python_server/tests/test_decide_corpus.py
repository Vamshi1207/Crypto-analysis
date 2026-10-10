"""Phase 2 forecast + decide tests against the historical OHLCV corpus."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from decision import calibrate, dataset, forecast
from decision.decide import decide
from decision.packet import packet_from_corpus
from decision.schema import Action, DecideMode
from decision.tradability import check_tradability


pytestmark = pytest.mark.corpus


@pytest.fixture(scope="module")
def fitted_residuals():
    os.environ["FORECAST_DISABLE_CHRONOS"] = "1"
    os.environ["FORECAST_DISABLE_TIMESFM"] = "1"
    calibrate.RESIDUALS_PATH = (
        Path(os.getenv("DECISION_STORE_DIR", "/app/store"))
        / "calibration"
        / "test_residuals.json"
    )
    calibrate._cache = None
    payload = calibrate.fit_from_corpus(limit_tokens=4, max_windows_per_token=20)
    assert payload["n"] >= 30, payload
    return payload


def test_stat_forecast_returns_ordered_bands(small_token):
    os.environ["FORECAST_DISABLE_CHRONOS"] = "1"
    os.environ["FORECAST_DISABLE_TIMESFM"] = "1"
    _, candles = dataset.load_timeframe(small_token.candles_path, "1", tail=128)
    ens = forecast.forecast_closes(dataset.closes(candles), horizon=10, timeframe="1")

    assert ens.backend == "stat"
    assert ens.raw.p10 <= ens.raw.p50 <= ens.raw.p90
    assert any(m.name == "stat" and m.available for m in ens.models)


def test_conformal_applies_with_residuals(fitted_residuals, small_token):
    os.environ["FORECAST_DISABLE_CHRONOS"] = "1"
    os.environ["FORECAST_DISABLE_TIMESFM"] = "1"
    _, candles = dataset.load_timeframe(small_token.candles_path, "1", tail=128)
    ens = forecast.forecast_closes(dataset.closes(candles), horizon=10)
    calibrated = calibrate.apply_conformal(ens)

    assert calibrated.residual_count >= 30
    assert calibrated.calibrated.p10 <= calibrated.calibrated.p50 <= calibrated.calibrated.p90
    assert calibrated.calibrated.p50 == ens.raw.p50


def test_decide_on_corpus_produces_card(fitted_residuals, small_token):
    os.environ["FORECAST_DISABLE_CHRONOS"] = "1"
    os.environ["FORECAST_DISABLE_TIMESFM"] = "1"
    card = decide(
        corpus_address=small_token.address,
        mode=DecideMode.FAST,
        timeframe="1",
        horizon=10,
        run_safety=False,
    )

    assert card.action in Action
    assert card.forecast.horizon_bars == 10
    assert "gate1" in card.gates_passed
    assert card.token["source"] == "corpus"
    assert card.summary()


def test_tradability_rejects_short_history(small_token):
    pkt = packet_from_corpus(small_token, timeframe="1", tail=50)
    result = check_tradability(pkt, timeframe="1", min_history=10_000)
    assert result.passed is False
    assert any("insufficient history" in r for r in result.reasons)
