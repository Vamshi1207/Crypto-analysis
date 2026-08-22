"""Shared fixtures for the candle corpus under ML_Training_datasets/CandleData."""

from __future__ import annotations

import pytest

from decision import dataset


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live: hits real Solana / DexScreener / Jupiter (needs network + HELIUS_API_KEY)",
    )
    config.addinivalue_line(
        "markers",
        "corpus: needs the local CandleData OHLCV extract; skipped when the dir is empty",
    )


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Redirect the append-only log to a temp dir for every test.

    Paper trades and scoreboard outcomes are written on close, so without this
    the suite's synthetic TEST positions land in the real store and corrupt the
    forecast-vs-realized record we rely on to decide whether there is any edge.
    """
    from decision import store as decision_store

    monkeypatch.setattr(decision_store, "STORE_DIR", tmp_path / "store")
    return tmp_path / "store"


@pytest.fixture(scope="session")
def corpus_tokens():
    tokens = dataset.list_tokens()
    if not tokens:
        pytest.skip(
            f"no candle files under {dataset.candles_dir()} "
            "(set CANDLE_DATA_DIR or extract via the extension)"
        )
    return tokens


@pytest.fixture(scope="session")
def small_token(corpus_tokens):
    """Prefer the smallest file so offline OHLCV tests stay fast."""
    return min(corpus_tokens, key=lambda t: t.size_bytes)
