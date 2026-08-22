"""Phase 3–5 hermetic tests: vibe desk, paper portfolio, scoreboard."""

from __future__ import annotations

from decision import paper, scoreboard, vibe
from decision.schema import (
    Action,
    DecisionCard,
    DecideMode,
    Direction,
    ForecastEnsemble,
    MarketPacket,
    ReturnBand,
    RiskBlock,
)


def _card(action: Action = Action.BUY, edge: float = 5.0) -> DecisionCard:
    return DecisionCard(
        token={"address": "Pool111", "mint": "Mint111", "name": "TEST", "source": "live"},
        horizon_bars=6,
        timeframe="5S",
        action=action,
        action_confidence=0.7,
        confidence_basis="test",
        direction=Direction.UP if edge > 0 else Direction.DOWN,
        expected_return_pct=ReturnBand(p10=-2, p50=edge + 2, p90=10),
        cost_adjusted_edge_pct=edge,
        position={"max_size_usd": 40, "size_basis": "test"},
        risk=RiskBlock(risk_pass=action is Action.BUY),
        forecast=ForecastEnsemble(
            horizon_bars=6,
            timeframe="5S",
            raw=ReturnBand(p10=-2, p50=edge + 2, p90=10),
            calibrated=ReturnBand(p10=-2, p50=edge + 2, p90=10),
            agreement=0.8,
            models=[],
            residual_count=10,
            backend="stat",
        ),
        price_targets={"entry": 1.0},
    )


def test_vibe_desk_runs_and_adds_drivers():
    card = _card()
    packet = MarketPacket(address="Pool111", mint="Mint111", timeframes={})
    review = vibe.review_card(card, packet, mode=DecideMode.FULL)
    assert review.opinions
    out = vibe.apply_review(card, review)
    assert any(d.source.startswith("vibe:") for d in out.drivers)


def test_risk_committee_blocks_negative_edge_buy():
    card = _card(action=Action.BUY, edge=-3.0)
    packet = MarketPacket(address="Pool111", timeframes={})
    review = vibe.review_card(card, packet, mode=DecideMode.FULL)
    out = vibe.apply_review(card, review)
    assert out.action is Action.AVOID
    assert out.risk.risk_pass is False


def test_paper_open_and_target_exit():
    paper.set_kill_switch(False)
    # Reset soft: close by draining via snapshot only — open fresh id
    card = _card()
    opened = paper.execute_decision(card, mark_price=1.0)
    assert opened["status"] in ("opened", "skipped")
    if opened["status"] == "opened":
        # + enough to clear $1 target after fees
        closed = paper.mark_and_maybe_exit(address="Pool111", mark_price=1.10)
        assert isinstance(closed, list)


def test_scoreboard_coverage_math():
    row = scoreboard.record_outcome(
        address="a",
        mint="m",
        timeframe="5S",
        horizon_bars=6,
        predicted_p50=2.0,
        predicted_p10=-1.0,
        predicted_p90=5.0,
        entry_price=100.0,
        exit_price=102.0,
        action="buy",
    )
    assert row["realized_pct"] == 2.0
    assert row["covered_80"] is True
    summary = scoreboard.summarize()
    assert summary["n"] >= 1
