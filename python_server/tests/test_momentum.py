"""Tape-based momentum entry signal."""

from __future__ import annotations

from decision import momentum


def _candles(closes: list[float], volumes: list[float] | None = None) -> list[dict]:
    vols = volumes or [1.0] * len(closes)
    return [
        {"timestamp": float(i), "open": c, "high": c, "low": c, "close": c, "volume": v}
        for i, (c, v) in enumerate(zip(closes, vols))
    ]


def test_breakout_on_expanding_volume_enters():
    flat = [1.0] * momentum.RANGE_BARS
    thrust = [1.01, 1.02, 1.035]
    volumes = [1.0] * momentum.RANGE_BARS + [4.0, 4.0, 4.0]
    result = momentum.evaluate(
        _candles(flat + thrust, volumes),
        orderflow={"buy_sell_ratio_5m": 2.0},
    )
    assert result["enter"] is True
    assert result["thrust_pct"] > momentum.MIN_THRUST_PCT
    assert result["breakout_pct"] > 0


def test_quiet_tape_does_not_enter():
    flat = [1.0] * (momentum.RANGE_BARS + momentum.THRUST_BARS)
    result = momentum.evaluate(_candles(flat))
    assert result["enter"] is False
    assert "thrust" in result["reason"]


def test_already_extended_move_is_rejected():
    flat = [1.0] * momentum.RANGE_BARS
    blowoff = [1.5, 1.8, 2.5]
    result = momentum.evaluate(
        _candles(flat + blowoff, [1.0] * momentum.RANGE_BARS + [9.0] * 3),
        orderflow={"buy_sell_ratio_5m": 3.0},
    )
    assert result["enter"] is False
    assert "not_extended" in result["reason"]


def test_selling_pressure_blocks_entry():
    flat = [1.0] * momentum.RANGE_BARS
    thrust = [1.01, 1.02, 1.035]
    result = momentum.evaluate(
        _candles(flat + thrust, [1.0] * momentum.RANGE_BARS + [4.0] * 3),
        orderflow={"buy_sell_ratio_5m": 0.4},
    )
    assert result["enter"] is False
    assert "flow" in result["reason"]


def test_continuation_enters_on_a_grind_up(monkeypatch):
    monkeypatch.setenv("PAPER_RISK_ON", "1")
    monkeypatch.setenv("LIVE_TRADING", "0")
    # Quiet range, then four modest green bars that never clear a 20-bar high
    # enough for the breakout path (0.8% thrust / 0.05% breakout) — but do
    # print the continuation grind paper risk-on is meant to take.
    base = [1.0 + i * 0.0001 for i in range(momentum.RANGE_BARS)]
    grind = [1.003, 1.006, 1.008, 1.011]
    result = momentum.evaluate(_candles(base + grind))
    assert result["enter"] is True
    assert result.get("path") == "continuation"


def test_short_history_is_not_a_signal():
    result = momentum.evaluate(_candles([1.0, 1.2, 1.5]))
    assert result["enter"] is False
    assert "need" in result["reason"]


def test_missing_volume_is_neutral_not_a_veto():
    flat = [1.0] * momentum.RANGE_BARS
    thrust = [1.01, 1.02, 1.035]
    # Sampled feeds can report zero volume; that must not block a clean breakout.
    result = momentum.evaluate(
        _candles(flat + thrust, [0.0] * (momentum.RANGE_BARS + 3)),
        orderflow={"buy_sell_ratio_5m": 2.0},
    )
    assert result["volume_ratio"] is None
    assert result["checks"]["volume"] is True
