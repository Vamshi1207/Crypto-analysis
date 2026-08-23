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
    entry_slip = 1.5
    gate = _est_round_trip_cost_pct(slippage_bps=entry_slip * 100.0)
    fixed = costs.ENTRY_FEE_PCT + costs.EXIT_FEE_PCT + costs.PRIORITY_TIP_PCT
    assert gate == pytest.approx(entry_slip + costs.EXIT_SLIP_PCT + fixed, abs=1e-6)
    # Both legs are present, so the total exceeds either slippage leg alone.
    assert gate > entry_slip + costs.EXIT_SLIP_PCT


def test_measured_impact_overrides_the_default():
    cheap = _est_round_trip_cost_pct(slippage_bps=10.0)
    dear = _est_round_trip_cost_pct(slippage_bps=900.0)
    assert cheap < dear
    fixed = costs.ENTRY_FEE_PCT + costs.EXIT_FEE_PCT + costs.PRIORITY_TIP_PCT
    assert cheap == pytest.approx(0.1 + costs.EXIT_SLIP_PCT + fixed, abs=1e-6)
    # A measured impact is used instead of the pessimistic fallback.
    assert cheap < _est_round_trip_cost_pct(slippage_bps=None)


def test_barrier_moves_account_for_entry_slippage():
    """The target is measured from the fill, which already sits above the quote."""
    slip = 1.5
    moves = costs.barrier_moves(
        size_usd=36.12, target_usd=1.0, stop_usd=1.5, entry_slip_pct_=slip
    )
    exit_frac = costs.exit_cost_pct() / 100.0
    expected_fill = (exit_frac + 1.0 / 36.12) * 100.0
    assert moves["target_from_fill_pct"] == pytest.approx(expected_fill, abs=0.02)
    # Entry slippage makes the move from the quote strictly larger.
    assert moves["target_from_quote_pct"] > moves["target_from_fill_pct"] + slip - 0.1
    # The stop is far closer than the target.
    assert abs(moves["stop_from_quote_pct"]) < moves["target_from_quote_pct"]


def test_stop_inside_exit_cost_is_degenerate():
    """A stop smaller than the exit cost on this size sits above the fill."""
    stop = 1.5
    # Size chosen so the exit cost alone exceeds the stop, whatever the config.
    size = stop / (costs.exit_cost_pct() / 100.0) * 1.2
    reason = costs.degenerate_stop_reason(size_usd=size, target_usd=1.0, stop_usd=stop)
    assert reason is not None
    assert "immediately" in reason


def test_sane_barriers_are_not_flagged():
    stop = 1.5
    # Comfortably smaller than the size at which the exit cost swallows the stop.
    size = stop / (costs.exit_cost_pct() / 100.0) * 0.5
    assert costs.degenerate_stop_reason(size_usd=size, target_usd=1.0, stop_usd=stop) is None


def test_required_move_falls_as_size_rises():
    """Why sizing decides tradability: the target's share of notional shrinks."""
    small = costs.required_move_pct(size_usd=40.0, target_usd=1.0)
    large = costs.required_move_pct(size_usd=200.0, target_usd=1.0)
    assert small > large
    cost = costs.round_trip_cost_pct()
    assert small == pytest.approx(cost + 2.5, abs=1e-6)
    assert large == pytest.approx(cost + 0.5, abs=1e-6)
    # Cost is the floor no size can get under.
    assert large > cost


def test_executor_refuses_degenerate_barriers(monkeypatch):
    """A guaranteed-loss configuration must be refused, not opened."""
    from tests.test_risk_guards import _card

    monkeypatch.setattr(paper, "STOP_LOSS_USD", 1.5)
    monkeypatch.setattr(paper, "STOP_LOSS_PCT", 0.0)
    monkeypatch.setattr(paper, "REENTRY_COOLDOWN_SEC", 0.0)
    paper.set_kill_switch(False)

    # Big enough that the exit cost alone exceeds the flat $1.50 stop.
    size = 1.5 / (costs.exit_cost_pct() / 100.0) * 1.5
    card = _card(address="DegeneratePool111")
    card.position.max_size_usd = size
    monkeypatch.setattr("decision.paper.SCALP_SIZE_USD", size)

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
    monkeypatch.setenv("PAPER_RISK_ON", "0")
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


def test_paper_risk_on_skips_median_hurdle(monkeypatch):
    from tests.test_risk_guards import _card

    monkeypatch.setattr(paper, "REENTRY_COOLDOWN_SEC", 0.0)
    monkeypatch.setattr(paper, "TARGET_PROFIT_USD", 1.0)
    monkeypatch.setenv("PAPER_RISK_ON", "1")
    monkeypatch.setenv("LIVE_TRADING", "0")
    paper.set_kill_switch(False)
    paper.reset()

    card = _card(address="RiskOnSmall111", band=(-5.0, 7.0, 20.0))
    card.position.max_size_usd = 15.0
    assert paper.execute_decision(card, mark_price=1.0)["status"] == "opened"
