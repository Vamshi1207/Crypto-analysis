"""Self-built candles from sampled DexScreener prices."""

from __future__ import annotations

import time

from decision import pricefeed


def _seed(mint: str, prices: list[float], *, step: float = 5.0) -> None:
    pricefeed.track(mint=mint, pool="Pool1", symbol="MEME")
    base = time.time() - step * len(prices)
    with pricefeed._lock:  # noqa: SLF001 - direct seed keeps the test off the network
        row = pricefeed._tracks[mint]  # noqa: SLF001
        row.samples.clear()
        for i, price in enumerate(prices):
            row.samples.append((base + i * step, price, 1.0))


def test_candles_bucket_samples_into_ohlc():
    pricefeed.reset()
    _seed("Mint1", [1.0, 1.5, 0.8, 2.0], step=5.0)
    bars = pricefeed.candles("Mint1", "5S")
    assert len(bars) == 4
    assert bars[0]["open"] == 1.0 and bars[0]["close"] == 1.0
    assert [b["close"] for b in bars] == [1.0, 1.5, 0.8, 2.0]

    # A coarser bucket folds the same samples into one bar with the true extremes.
    coarse = pricefeed.candles("Mint1", "30S")
    assert len(coarse) <= 2
    assert max(b["high"] for b in coarse) == 2.0
    assert min(b["low"] for b in coarse) == 0.8


def test_timeframes_skips_series_with_one_bar():
    pricefeed.reset()
    _seed("Mint2", [1.0, 1.1], step=5.0)
    tfs = pricefeed.timeframes("Mint2")
    assert "5S" in tfs
    # 10s of samples cannot fill two 1m buckets.
    assert "1" not in tfs


def test_untracked_mint_has_no_candles():
    pricefeed.reset()
    assert pricefeed.candles("Nope", "5S") == []
    assert pricefeed.sample_count("Nope") == 0


def test_deepest_pair_ignores_pools_where_mint_is_quote():
    pairs = [
        {
            "baseToken": {"address": "Other"},
            "quoteToken": {"address": "Mint1"},
            "liquidity": {"usd": 900_000},
            "priceUsd": "42",
        },
        {
            "baseToken": {"address": "Mint1"},
            "quoteToken": {"address": "So111"},
            "liquidity": {"usd": 10_000},
            "priceUsd": "0.5",
        },
    ]
    best = pricefeed._deepest_pair(pairs, "Mint1")  # noqa: SLF001
    assert best is not None
    # The deeper pool prices "Other", not our mint, so it must be skipped.
    assert pricefeed._price_of(best, "Mint1") == 0.5  # noqa: SLF001
