"""Gap-aware stops, per-mint concentration, and live calibration refit."""

from __future__ import annotations

import json

import pytest

from decision import calibrate, paper
from tests.test_risk_guards import _card


@pytest.fixture(autouse=True)
def _paper_defaults(monkeypatch):
    monkeypatch.setattr(paper, "REENTRY_COOLDOWN_SEC", 0.0)
    monkeypatch.setattr(paper, "STOP_FILL_MODE", "barrier")
    monkeypatch.setattr(paper, "MAX_TRADES_PER_MINT_DAY", 4)
    monkeypatch.setattr(paper, "MAX_NOTIONAL_PER_MINT_DAY", 200.0)
    monkeypatch.setattr(paper, "MAX_DAILY_LOSS_PER_MINT", 50.0)
    paper.set_kill_switch(False)


def test_stop_fills_at_barrier_not_gapped_mark():
    """A -25% gap must book the stop, not the full gapped mark loss."""
    card = _card(address="GapBarrierPool", band=(-5.0, 8.0, 20.0), agreement=0.8)
    opened = paper.execute_decision(card, mark_price=100.0)
    assert opened["status"] == "opened", opened
    entry = opened["position"]["entry_price"]

    # Mark crashes ~25% — well past the stop.
    closed = paper.mark_and_maybe_exit(address="GapBarrierPool", mark_price=entry * 0.75)
    assert closed
    pos = closed[0]
    expected_stop = paper.stop_loss_for_size(pos["size_usd"])
    assert pos["realized_pnl_usd"] == pytest.approx(-expected_stop, abs=0.01)
    assert "barrier" in (pos.get("close_reason") or "")
    assert "mark would be" in (pos.get("close_reason") or "")


def test_mark_mode_still_gaps_but_caps_max_loss(monkeypatch):
    monkeypatch.setattr(paper, "STOP_FILL_MODE", "mark")
    monkeypatch.setattr(paper, "MAX_LOSS_PCT", 8.0)
    card = _card(address="GapMarkPool", band=(-5.0, 8.0, 20.0))
    opened = paper.execute_decision(card, mark_price=100.0)
    assert opened["status"] == "opened"
    size = opened["position"]["size_usd"]
    entry = opened["position"]["entry_price"]

    closed = paper.mark_and_maybe_exit(address="GapMarkPool", mark_price=entry * 0.50)
    assert closed
    pos = closed[0]
    # Floor at 8% of size, not a free-fall to -50%.
    assert pos["realized_pnl_usd"] >= -size * 0.08 - 0.01
    assert "gap capped" in (pos.get("close_reason") or "")


def test_add_on_allowed_when_analysis_buys_again(monkeypatch):
    monkeypatch.setattr(paper, "ALLOW_ADD_ON", True)
    monkeypatch.setattr(paper, "MAX_OPEN_POSITIONS", 5)
    monkeypatch.setattr(paper, "MAX_NOTIONAL_USD", 500.0)
    monkeypatch.setattr(paper, "MAX_TRADES_PER_MINT_DAY", 0)
    card = _card(address="AddOnPool", band=(-5.0, 8.0, 20.0))
    first = paper.execute_decision(card, mark_price=10.0)
    second = paper.execute_decision(card, mark_price=10.2)
    assert first["status"] == "opened"
    assert second["status"] == "opened"
    assert second.get("add_on") is True
    open_same = [p for p in paper.snapshot()["open"] if p["address"] == "AddOnPool"]
    assert len(open_same) == 2


def test_add_on_can_be_disabled(monkeypatch):
    monkeypatch.setattr(paper, "ALLOW_ADD_ON", False)
    card = _card(address="NoAddPool", band=(-5.0, 8.0, 20.0))
    assert paper.execute_decision(card, mark_price=10.0)["status"] == "opened"
    again = paper.execute_decision(card, mark_price=10.0)
    assert again["status"] == "skipped"
    assert "already open" in again["reason"]


def test_mint_daily_trade_cap_blocks_reentry(monkeypatch):
    monkeypatch.setattr(paper, "MAX_TRADES_PER_MINT_DAY", 2)
    mint = "MintCapAAA1111111111111111111111111111111"
    for i in range(2):
        card = _card(address=f"MintCapAddr{i}", band=(-5.0, 8.0, 20.0))
        card.token["mint"] = mint
        assert paper.execute_decision(card, mark_price=1.0)["status"] == "opened"
        paper.mark_and_maybe_exit(address=f"MintCapAddr{i}", mark_price=0.5)

    card = _card(address="MintCapAddrX", band=(-5.0, 8.0, 20.0))
    card.token["mint"] = mint
    again = paper.execute_decision(card, mark_price=1.0)
    assert again["status"] == "refused"
    assert "trade cap" in again["reason"]


def test_mint_daily_loss_cap_blocks_after_bleed(monkeypatch):
    # Flat stop keeps the single loss this asserts on independent of size config.
    monkeypatch.setattr(paper, "STOP_LOSS_PCT", 0.0)
    monkeypatch.setattr(paper, "STOP_LOSS_USD", 1.5)
    monkeypatch.setattr(paper, "MAX_DAILY_LOSS_PER_MINT", 1.5)
    monkeypatch.setattr(paper, "STOP_FILL_MODE", "barrier")
    mint = "MintLossBBB2222222222222222222222222222222"
    card = _card(address="MintLossAddr1", band=(-5.0, 8.0, 20.0))
    card.token["mint"] = mint
    assert paper.execute_decision(card, mark_price=10.0)["status"] == "opened"
    paper.mark_and_maybe_exit(address="MintLossAddr1", mark_price=1.0)  # stop -$1.50

    card2 = _card(address="MintLossAddr2", band=(-5.0, 8.0, 20.0))
    card2.token["mint"] = mint
    blocked = paper.execute_decision(card2, mark_price=10.0)
    assert blocked["status"] == "refused"
    assert "daily loss" in blocked["reason"]


def test_open_stamps_forecast_onto_position():
    card = _card(address="ForecastStampPool", band=(-4.0, 6.5, 12.0))
    opened = paper.execute_decision(card, mark_price=50.0)
    assert opened["status"] == "opened"
    pos = opened["position"]
    assert pos["predicted_p50"] == pytest.approx(6.5)
    assert pos["predicted_p10"] == pytest.approx(-4.0)
    assert pos["timeframe"] == "5S"


def test_fit_from_outcomes_merges_live_residuals(tmp_path, monkeypatch):
    from decision import store as decision_store

    monkeypatch.setattr(decision_store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(calibrate, "RESIDUALS_PATH", tmp_path / "calibration" / "residuals.json")
    monkeypatch.setattr(calibrate, "_cache", None)

    # Seed a tiny corpus floor.
    calibrate.save_residuals(
        {
            "coverage_target": 0.8,
            "horizon": 6,
            "timeframe": "1",
            "errors": [4.8] * 40,
            "n": 40,
            "error_p80": 4.8,
        }
    )

    for i in range(35):
        decision_store.append(
            "outcomes",
            {
                "predicted_p50": 2.0,
                "realized_pct": 2.0 + (i % 5),
                "predicted_p10": -3.0,
                "predicted_p90": 7.0,
                "covered_80": True,
                "address": f"a{i}",
                "action": "paper_scalp",
            },
        )

    result = calibrate.fit_from_outcomes(min_errors=30, merge_with_existing=True, days=1)
    assert result["status"] == "fitted"
    assert result["live_n"] == 35
    assert result["timeframe"] == "live"
    store = calibrate.load_residuals()
    assert store["n"] == 35 + 40
    assert store["source"] == "live_outcomes+corpus"


def test_fit_from_outcomes_insufficient_keeps_corpus(tmp_path, monkeypatch):
    from decision import store as decision_store

    monkeypatch.setattr(decision_store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(calibrate, "RESIDUALS_PATH", tmp_path / "calibration" / "residuals.json")
    monkeypatch.setattr(calibrate, "_cache", None)

    calibrate.save_residuals(
        {"coverage_target": 0.8, "horizon": 6, "timeframe": "1", "errors": [3.0] * 50, "n": 50}
    )
    decision_store.append(
        "outcomes",
        {"predicted_p50": 1.0, "realized_pct": 2.0, "address": "x", "action": "paper_scalp"},
    )
    result = calibrate.fit_from_outcomes(min_errors=30, days=1)
    assert result["status"] == "insufficient"
    assert calibrate.load_residuals()["n"] == 50
