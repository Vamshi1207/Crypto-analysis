"""Guards against the failure mode that produced 5 stop-outs in 6 trades.

Every losing entry shared a signature: a low-agreement ensemble, an interval
whose p10 was positive (so max_loss_pct read 0.0), and a token that had just
gone near-vertical. The swarm then re-bought the same token on the next tick
after each stop.
"""

from __future__ import annotations

import pytest

from decision import paper, vibe
from decision.schema import (
    Action,
    DecideMode,
    DecisionCard,
    Direction,
    ForecastEnsemble,
    MarketPacket,
    ReturnBand,
    RiskBlock,
    TimeframePacket,
)


def _card(
    *,
    action: Action = Action.BUY,
    edge: float = 5.0,
    agreement: float = 0.8,
    band: tuple[float, float, float] = (-2.0, 7.0, 10.0),
    address: str = "Pool111",
) -> DecisionCard:
    rb = ReturnBand(p10=band[0], p50=band[1], p90=band[2])
    return DecisionCard(
        token={"address": address, "mint": "Mint111", "name": "TEST", "source": "live"},
        horizon_bars=6,
        timeframe="5S",
        action=action,
        action_confidence=0.7,
        confidence_basis="test",
        direction=Direction.UP if edge > 0 else Direction.DOWN,
        expected_return_pct=rb,
        cost_adjusted_edge_pct=edge,
        position={"max_size_usd": 40, "size_basis": "test"},
        risk=RiskBlock(risk_pass=action is Action.BUY),
        forecast=ForecastEnsemble(
            horizon_bars=6,
            timeframe="5S",
            raw=rb,
            calibrated=rb,
            agreement=agreement,
            models=[],
            residual_count=360,
            backend="stat",
        ),
        price_targets={"entry": 1.0},
    )


def _packet_with_run(pct_run: float) -> MarketPacket:
    """A 12-bar window whose total return is `pct_run` percent."""
    n = 12
    step = (1.0 + pct_run / 100.0) ** (1.0 / (n - 1))
    closes = [100.0 * step**i for i in range(n)]
    return MarketPacket(
        address="Pool111",
        mint="Mint111",
        timeframes={
            "5S": TimeframePacket(
                timeframe="5S",
                candle_count=n,
                ohlcv_tail=[{"close": c} for c in closes],
            )
        },
    )


def test_low_agreement_buy_is_vetoed():
    card = _card(agreement=0.18)
    review = vibe.review_card(card, _packet_with_run(5.0), mode=DecideMode.FULL)
    out = vibe.apply_review(card, review)
    assert out.action is Action.AVOID
    assert any("agreement" in r for r in out.risk.veto_reasons)


def test_all_positive_interval_is_vetoed():
    """p10 > 0 means the model claims a riskless trade — reject it."""
    card = _card(band=(5.7075, 10.5077, 15.3079))
    review = vibe.review_card(card, _packet_with_run(5.0), mode=DecideMode.FULL)
    out = vibe.apply_review(card, review)
    assert out.action is Action.AVOID
    assert any("implausible interval" in r for r in out.risk.veto_reasons)


@pytest.mark.parametrize("run_pct", [-60.0, -10.0, 0.0, 8.0, 30.0, 275.3, 855.3])
def test_momentum_never_votes_directionally(run_pct):
    """Trailing return showed no forward edge in the corpus, so it abstains.

    tools/barrier_study.py found every trailing-return bucket within noise of
    the round-trip cost. A zero weight keeps this desk out of the buy/avoid
    tally: it used to vote buy above +3%, which bought an +855% vertical move.
    """
    op = vibe._momentum_specialist(_card(), _packet_with_run(run_pct))
    assert op.vote == "hold"
    assert op.weight == 0.0


def test_momentum_still_describes_the_tape():
    assert "blowoff" in vibe._momentum_specialist(_card(), _packet_with_run(855.3)).claim
    assert "extended" in vibe._momentum_specialist(_card(), _packet_with_run(30.0)).claim
    assert "rising" in vibe._momentum_specialist(_card(), _packet_with_run(8.0)).claim
    assert "flat" in vibe._momentum_specialist(_card(), _packet_with_run(0.0)).claim
    assert "falling" in vibe._momentum_specialist(_card(), _packet_with_run(-10.0)).claim


def test_reentry_is_blocked_during_cooldown(monkeypatch):
    monkeypatch.setattr(paper, "REENTRY_COOLDOWN_SEC", 120.0)
    paper.set_kill_switch(False)
    # Own address: the paper portfolio is module-level state shared across tests.
    card = _card(address="CooldownPool111")
    address = card.token["address"]

    opened = paper.execute_decision(card, mark_price=1.0)
    assert opened["status"] == "opened", opened

    # Stop it out, then immediately attempt the same entry again.
    closed = paper.mark_and_maybe_exit(address=address, mark_price=0.5)
    assert closed, "expected a stop-loss close"

    again = paper.execute_decision(card, mark_price=1.0)
    assert again["status"] == "skipped"
    assert "cooldown" in again["reason"]


def test_cooldown_expires(monkeypatch):
    """Same address becomes tradable again once the quiet period elapses."""
    monkeypatch.setattr(paper, "REENTRY_COOLDOWN_SEC", 120.0)
    paper.set_kill_switch(False)
    card = _card(address="ExpiryPool111")
    address = card.token["address"]

    assert paper.execute_decision(card, mark_price=1.0)["status"] == "opened"
    assert paper.mark_and_maybe_exit(address=address, mark_price=0.5)
    assert paper._cooldown_remaining(address) > 0

    monkeypatch.setattr(paper, "REENTRY_COOLDOWN_SEC", 1e-9)
    assert paper._cooldown_remaining(address) == 0.0
    assert paper.execute_decision(card, mark_price=1.0)["status"] == "opened"


def test_cooldown_of_zero_disables_the_check(monkeypatch):
    monkeypatch.setattr(paper, "REENTRY_COOLDOWN_SEC", 0.0)
    assert paper._cooldown_remaining("CooldownPool111") == 0.0
