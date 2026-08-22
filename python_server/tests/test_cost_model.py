"""Gate 2 and the executor must price a trade identically.

The regression: `packet._est_round_trip_cost_pct` counted slippage once and
omitted exit slippage entirely, reporting 1.86% where `decision.paper` charged
3.20% on the fill. Buys cleared Gate 2 against a cost execution would not
honour, so they could not reach the profit target and drifted into the stop.
"""

from __future__ import annotations

import pytest

from decision import costs, paper
from decision.packet import _est_round_trip_cost_pct


def test_gate_cost_matches_executor_cost():
    """Every component the executor charges must appear in the gate's estimate."""
    impact_pct = 1.5
    gate = _est_round_trip_cost_pct(slippage_bps=impact_pct * 100.0)

    executor = (
        costs.entry_slip_pct(price_impact_pct=impact_pct)
        + costs.ENTRY_FEE_PCT
        + costs.EXIT_FEE_PCT
        + costs.EXIT_SLIP_PCT
        + costs.PRIORITY_TIP_PCT
    )
    assert gate == pytest.approx(executor, abs=1e-6)


def test_gate_cost_includes_both_slippage_legs():
    """The old formula's specific hole: exit slippage was free."""
    gate = _est_round_trip_cost_pct(slippage_bps=150.0)
    assert gate > costs.DEFAULT_ENTRY_SLIP_PCT + costs.EXIT_SLIP_PCT
    assert gate == pytest.approx(3.2, abs=1e-6)


def test_measured_impact_overrides_the_default():
    cheap = _est_round_trip_cost_pct(slippage_bps=10.0)
    dear = _est_round_trip_cost_pct(slippage_bps=900.0)
    assert cheap < dear
    assert cheap == pytest.approx(0.1 + 0.3 + 0.3 + 1.0 + 0.1, abs=1e-6)


def test_barrier_moves_account_for_entry_slippage():
    """A $1 target on $36.12 is a +5.6% move, not a +2.8% one."""
    moves = costs.barrier_moves(size_usd=36.12, target_usd=1.0, stop_usd=1.5)
    assert moves["target_from_fill_pct"] == pytest.approx(4.07, abs=0.02)
    assert moves["target_from_quote_pct"] == pytest.approx(5.63, abs=0.02)
    # The stop is far closer than the target: 1.4% vs 5.6%.
    assert moves["stop_from_quote_pct"] == pytest.approx(-1.40, abs=0.02)
    assert abs(moves["stop_from_quote_pct"]) < moves["target_from_quote_pct"]


def test_stop_inside_exit_cost_is_degenerate():
    """$1.50 on a $200 position is less than the 1.3% exit cost."""
    reason = costs.degenerate_stop_reason(size_usd=200.0, target_usd=1.0, stop_usd=1.5)
    assert reason is not None
    assert "immediately" in reason


def test_sane_barriers_are_not_flagged():
    assert costs.degenerate_stop_reason(size_usd=36.12, target_usd=1.0, stop_usd=1.5) is None


def test_executor_refuses_degenerate_barriers(monkeypatch):
    """A guaranteed-loss configuration must be refused, not opened."""
    from tests.test_risk_guards import _card

    monkeypatch.setattr(paper, "STOP_LOSS_USD", 1.5)
    monkeypatch.setattr(paper, "REENTRY_COOLDOWN_SEC", 0.0)
    paper.set_kill_switch(False)

    card = _card(address="DegeneratePool111")
    card.position.max_size_usd = 500.0
    monkeypatch.setattr("decision.paper.SCALP_SIZE_USD", 500.0)

    result = paper.execute_decision(card, mark_price=1.0)
    assert result["status"] == "refused"
    assert "stops out immediately" in result["reason"]


def test_zero_size_is_rejected():
    with pytest.raises(ValueError):
        costs.barrier_moves(size_usd=0.0, target_usd=1.0, stop_usd=1.5)


def test_card_sized_at_zero_is_refused(monkeypatch):
    """An explicit $0.00 must not be reinterpreted as full scalp size.

    `_position` returns 0.0 when liquidity is unmeasured, and a risk veto sets it
    to 0.0 too. Both used to fill at $40 because 0.0 is falsy in Python.
    """
    from tests.test_risk_guards import _card

    monkeypatch.setattr(paper, "REENTRY_COOLDOWN_SEC", 0.0)
    paper.set_kill_switch(False)

    card = _card(address="ZeroSizePool111")
    card.position.max_size_usd = 0.0
    card.position.size_basis = "scalp_size_x_confidence_liq_capped"

    result = paper.execute_decision(card, mark_price=1.0)
    assert result["status"] == "refused"
    assert "$0.00" in result["reason"]
    assert "liq_capped" in result["reason"]


def test_executor_skips_when_median_cannot_reach_target(monkeypatch):
    """A fixed dollar target needs a bigger % move as size shrinks."""
    from tests.test_risk_guards import _card

    monkeypatch.setattr(paper, "REENTRY_COOLDOWN_SEC", 0.0)
    monkeypatch.setattr(paper, "TARGET_PROFIT_USD", 1.0)
    paper.set_kill_switch(False)

    # +7% median clears the barrier on $36 but not on $15.
    card = _card(address="TooSmallPool111", band=(-5.0, 7.0, 20.0))
    card.position.max_size_usd = 15.0
    result = paper.execute_decision(card, mark_price=1.0)
    assert result["status"] == "skipped"
    assert "cannot reach" in result["reason"]

    card = _card(address="BigEnoughPool111", band=(-5.0, 7.0, 20.0))
    card.position.max_size_usd = 36.12
    assert paper.execute_decision(card, mark_price=1.0)["status"] == "opened"
