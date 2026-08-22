"""Offline tests against the historical meme-token OHLCV corpus.

The candle files are old. We only use OHLCV values — never live mint resolution,
Gate 0, or Jupiter. These tests prove the series are well-formed and ready for
indicator checks and Phase 2 forecast calibration.
"""

from __future__ import annotations

import pytest

from decision import dataset
from indicators import get_indicators_for_token

REQUIRED_CANDLE_KEYS = {"timestamp", "open", "high", "low", "close", "volume"}
REQUIRED_STAT_KEYS = {
    "buyCount",
    "sellCount",
    "buyVolumeSol",
    "sellVolumeSol",
    "priceSol",
    "createdAt",
}


pytestmark = pytest.mark.corpus


def test_corpus_is_indexed(corpus_tokens):
    assert len(corpus_tokens) >= 10
    addresses = {t.address for t in corpus_tokens}
    assert len(addresses) == len(corpus_tokens)
    for token in corpus_tokens:
        assert len(token.address) >= 32
        assert token.candles_path.exists()
        assert token.size_bytes > 1_000


def test_headers_match_filenames(corpus_tokens):
    sample = sorted(corpus_tokens, key=lambda t: t.size_bytes)[:8]
    for token in sample:
        header = dataset.load_header(token.candles_path)
        assert header["address"] == token.address
        assert isinstance(header["name"], str) and header["name"]
        assert header["updated"]


def test_ohlcv_candles_are_well_formed(small_token):
    header, candles = dataset.load_timeframe(small_token.candles_path, "1", tail=500)

    assert header["address"] == small_token.address
    assert len(candles) >= 50

    for candle in candles:
        assert REQUIRED_CANDLE_KEYS <= set(candle.keys())
        assert candle["high"] >= candle["low"]
        assert candle["high"] >= min(candle["open"], candle["close"])
        assert candle["low"] <= max(candle["open"], candle["close"])
        assert candle["volume"] >= 0
        assert isinstance(candle["timestamp"], int)
        assert candle["close"] > 0

    timestamps = [c["timestamp"] for c in candles]
    assert timestamps == sorted(timestamps), "candles must be time-ordered"


def test_closes_series_for_forecast_input(small_token):
    _, candles = dataset.load_timeframe(small_token.candles_path, "1", tail=256)
    series = dataset.closes(candles)

    assert len(series) == len(candles)
    assert all(isinstance(x, float) and x > 0 for x in series)


def test_ohlcv_rows_are_numeric_only(small_token):
    _, candles = dataset.load_timeframe(small_token.candles_path, "5", tail=100)
    rows = dataset.ohlcv_rows(candles)

    assert len(rows) == len(candles)
    for row in rows:
        assert set(row) == {"timestamp", "open", "high", "low", "close", "volume"}
        assert row["high"] >= row["low"]


def test_forward_returns_label_realized_moves(small_token):
    _, candles = dataset.load_timeframe(small_token.candles_path, "1", tail=200)
    series = dataset.closes(candles)
    labels = dataset.forward_returns(series, horizons=(1, 5, 10))

    assert set(labels) == {1, 5, 10}
    assert labels[1][-1] is None
    assert labels[5][-1] is None
    # Early bars should have realizable forward returns.
    assert labels[1][0] is not None
    assert labels[10][0] is not None
    # A 1-bar label must match the series arithmetic.
    expected = (series[1] / series[0] - 1.0) * 100.0
    assert labels[1][0] == pytest.approx(expected, rel=1e-6)


def test_summarize_candles_on_real_window(small_token):
    _, candles = dataset.load_timeframe(small_token.candles_path, "5", tail=200)
    summary = dataset.summarize_candles(candles)

    assert summary["count"] == len(candles)
    assert summary["high"] >= summary["low"]
    assert summary["close"] is not None
    assert summary["first_ts"] <= summary["last_ts"]
    assert summary["return_pct"] is not None


def test_indicators_run_on_real_ohlcv(small_token):
    _, candles = dataset.load_timeframe(small_token.candles_path, "1", tail=300)
    snapshot = get_indicators_for_token(candles)

    assert snapshot["rsi"] is not None
    assert snapshot["ema20"] is not None
    assert snapshot["atr"]
    assert snapshot["macd_line"] is not None


def test_stats_buckets_when_present(small_token):
    if not small_token.has_stats:
        pytest.skip("no companion stats file for the smallest token")

    header, stats = dataset.load_stats(small_token.stats_path)
    assert header["address"] == small_token.address
    assert len(stats) > 0
    for bucket in stats[:20]:
        assert REQUIRED_STAT_KEYS <= set(bucket.keys())
        assert bucket["buyCount"] >= 0
        assert bucket["sellCount"] >= 0
        assert bucket["priceSol"] >= 0


def test_multiple_tokens_yield_usable_close_series(corpus_tokens):
    """Spot-check several files so Phase 2 isn't calibrated on a single token."""
    sample = sorted(corpus_tokens, key=lambda t: t.size_bytes)[:5]
    for token in sample:
        _, candles = dataset.load_timeframe(token.candles_path, "1", tail=128)
        series = dataset.closes(candles)
        assert len(series) >= 50
        assert min(series) > 0


@pytest.mark.parametrize("timeframe", ["5S", "1", "5", "60"])
def test_every_standard_timeframe_loads(small_token, timeframe):
    _, candles = dataset.load_timeframe(small_token.candles_path, timeframe, tail=50)
    assert len(candles) > 0
    assert len(dataset.closes(candles)) == len(candles)
