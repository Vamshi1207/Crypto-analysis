"""Indicator alignment signal used after Gate 2."""

from __future__ import annotations

from decision import indicators_signal
from decision.schema import Direction


def test_bullish_snapshot_scores_positive():
    sig = indicators_signal.score_indicators(
        {
            "rsi": 62,
            "macd_hist": 0.02,
            "adx": 28,
            "plus_di": 30,
            "minus_di": 15,
            "stoch_k": 65,
            "ema_cross": "bullish",
        }
    )
    assert sig.available
    assert sig.score > 0.2


def test_bearish_snapshot_blocks_up_buy(monkeypatch):
    monkeypatch.setattr(indicators_signal, "ENABLED", True)
    monkeypatch.setattr(indicators_signal, "MIN_BUY_SCORE", -0.15)
    monkeypatch.setattr(indicators_signal, "HARD_BLOCK_SCORE", -0.45)
    sig = indicators_signal.evaluate_for_direction(
        {
            "rsi": 28,
            "macd_hist": -0.03,
            "adx": 35,
            "plus_di": 12,
            "minus_di": 32,
            "stoch_k": 18,
        },
        Direction.UP,
    )
    assert sig.available
    assert sig.score < -0.3
    assert sig.soft_ok is False


def test_missing_indicators_skip_gate():
    sig = indicators_signal.evaluate_for_direction({"error": "boom"}, Direction.UP)
    assert sig.available is False
    assert sig.soft_ok is True
    assert sig.hard_block is False


def test_confidence_multiplier_moves_with_score():
    bull = indicators_signal.IndicatorSignal(
        score=0.8, votes={"rsi": 0.8}, available=True, reason="ok", hard_block=False, soft_ok=True
    )
    bear = indicators_signal.IndicatorSignal(
        score=-0.8, votes={"rsi": -0.8}, available=True, reason="bad", hard_block=False, soft_ok=True
    )
    assert indicators_signal.confidence_multiplier(bull) > 1.0
    assert indicators_signal.confidence_multiplier(bear) < 1.0
