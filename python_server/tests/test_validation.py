"""Statistical gate for the parallel lanes."""

from __future__ import annotations

import pytest

from decision import labs, validation


def test_eval_trades_basic():
    stats = validation.eval_trades([2.0, -1.0, 3.0, 2.0])
    assert stats["n"] == 4
    assert stats["win_rate"] == 0.75
    assert stats["expectancy_usd"] == pytest.approx(1.5)
    assert stats["total_pnl_usd"] == pytest.approx(6.0)


def test_eval_trades_zero_variance_significant():
    stats = validation.eval_trades([2.0] * 40)
    assert stats["t_stat"] == 999.0  # capped infinity
    assert stats["p_value"] == 0.0
    assert stats["max_drawdown_usd"] == 0.0


def test_eval_trades_drawdown_from_ordered_pnls():
    stats = validation.eval_trades([-5.0, -3.0, 10.0])
    assert stats["max_drawdown_usd"] == pytest.approx(8.0)


def test_significant_requires_min_n():
    stats = validation.eval_trades([2.0] * 5)
    passed, reason = validation.significant(stats, min_n=30)
    assert not passed
    assert "n=" in reason


def test_significant_rejects_negative_expectancy():
    stats = validation.eval_trades([-2.0] * 40)
    passed, reason = validation.significant(stats)
    assert not passed
    assert "≤ 0" in reason


def test_significant_rejects_noise():
    stats = validation.eval_trades([2.0, -1.9] * 40)  # mean 0.05, high variance
    passed, reason = validation.significant(stats)
    assert not passed
    assert "t=" in reason


def test_significant_passes_clear_edge():
    stats = validation.eval_trades([2.0, 1.0, 3.0, 2.5, 2.0] * 8)  # n=40, positive
    passed, reason = validation.significant(stats)
    assert passed, reason


def test_evaluate_lane_extracts_pnl():
    closes = [
        {"realized_pnl_usd": 2.0},
        {"realized_pnl_usd": -1.0},
        {"realized_pnl_usd": None},
        {},
        {"realized_pnl_usd": 3.0},
    ]
    out = validation.evaluate_lane(closes)
    assert out["stats"]["n"] == 3


def test_lane_config_hash_stable_and_sensitive():
    h1 = labs.lane_config_hash(labs.SNIPE_LANES[0])
    h2 = labs.lane_config_hash(labs.SNIPE_LANES[0])
    assert h1 == h2
    assert len(h1) == 16
    assert h1 != labs.lane_config_hash(labs.SNIPE_LANES[1])


def test_lane_board_exposes_significance():
    from decision import paper

    paper.reset()
    with paper.use_lane("hold"):
        book = paper._cur()
        for i in range(40):
            pos = paper.PaperPosition(
                id=f"h{i}",
                address=f"Pool{i}",
                mint=f"Mint{i}",
                name="H",
                entry_price=1.0,
                size_usd=40.0,
                qty=40.0,
                opened_at="2026-01-01T00:00:00+00:00",
                target_profit_usd=1.0,
                stop_loss_usd=1.5,
                realized_pnl_usd=2.0,
            )
            book.closed.append(pos)
    board = paper.lane_board()
    hold = next(r for r in board["lanes"] if r["id"] == "hold")
    assert hold["significant"] is True
    assert hold["stats"]["n"] == 40
