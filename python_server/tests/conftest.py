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


@pytest.fixture(autouse=True)
def fresh_monitoring_session():
    """Each test gets an isolated monitoring session window."""
    from datetime import datetime, timezone

    from decision import session as decision_session

    decision_session.begin_session(datetime.now(timezone.utc).isoformat())


@pytest.fixture(autouse=True)
def paper_concentration_headroom(monkeypatch):
    """Paper portfolio is process-global; give tests headroom unless they tighten caps."""
    from decision import paper

    paper.reset(starting_cash_usd=1000.0)
    paper.set_kill_switch(False)
    monkeypatch.setattr(paper, "MAX_TRADES_PER_MINT_DAY", 50)
    monkeypatch.setattr(paper, "MAX_NOTIONAL_PER_MINT_DAY", 10_000.0)
    monkeypatch.setattr(paper, "MAX_DAILY_LOSS_PER_MINT", 10_000.0)
    monkeypatch.setattr(paper, "BLOCK_REPEAT_MINT_AFTER_STOP", False)


@pytest.fixture(autouse=True)
def labs_off_unless_requested(monkeypatch, request):
    """Keep the old single-book tests on main. test_labs.py turns labs on."""
    if request.node.fspath.basename == "test_labs.py":
        return
    from decision import labs

    monkeypatch.setattr(labs, "ENABLED", False)


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
