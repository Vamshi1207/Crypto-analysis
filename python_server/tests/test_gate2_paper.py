"""Gate 2 paper thresholds and entry guards."""

from __future__ import annotations

import pytest

from decision import paper
from decision.packet import gate2_thresholds
from tests.test_risk_guards import _card


def test_gate2_paper_uses_fraction_of_dollar_target(monkeypatch):
    monkeypatch.setattr("decision.packet.TARGET_PROFIT_USD", 1.0)
    monkeypatch.setattr("decision.packet.PAPER_GATE2_EDGE_FRACTION", 0.55)
    monkeypatch.setattr("decision.packet.MIN_EDGE_PCT", 1.5)
    monkeypatch.setattr("decision.packet.SCALP_SIZE_USD", 40.0)

    need, target = gate2_thresholds(live_trading=False)
    assert target == pytest.approx(0.55)
    assert need == pytest.approx(1.5)  # max(1.5%, 1.375%)


def test_gate2_live_uses_full_target(monkeypatch):
    monkeypatch.setattr("decision.packet.TARGET_PROFIT_USD", 1.0)
    monkeypatch.setattr("decision.packet.PAPER_GATE2_EDGE_FRACTION", 0.55)
    monkeypatch.setattr("decision.packet.MIN_EDGE_PCT", 1.5)
    monkeypatch.setattr("decision.packet.SCALP_SIZE_USD", 40.0)

    need, target = gate2_thresholds(live_trading=True)
    assert target == pytest.approx(1.0)
    assert need == pytest.approx(2.5)


def test_block_repeat_mint_after_stop(monkeypatch):
    monkeypatch.setattr(paper, "BLOCK_REPEAT_MINT_AFTER_STOP", True)
    monkeypatch.setattr(paper, "ALLOW_ADD_ON", True)
    mint = "StopMint1111111111111111111111111111111"
    card = _card(address="StopAddr", band=(-5.0, 8.0, 20.0))
    card.token["mint"] = mint
    opened = paper.execute_decision(card, mark_price=10.0)
    assert opened["status"] == "opened"
    paper.mark_and_maybe_exit(address="StopAddr", mark_price=1.0)

    again = paper.execute_decision(card, mark_price=10.0)
    assert again["status"] == "skipped"
    assert "stop loss" in again["reason"]


def test_block_add_on_when_open_lot_losing(monkeypatch):
    monkeypatch.setattr(paper, "BLOCK_ADD_ON_IF_OPEN_LOSING", True)
    monkeypatch.setattr(paper, "BLOCK_REPEAT_MINT_AFTER_STOP", False)
    monkeypatch.setattr(paper, "ALLOW_ADD_ON", True)
    monkeypatch.setattr(paper, "MAX_OPEN_LOTS_PER_ADDRESS", 3)
    card = _card(address="LosingAddr", band=(-5.0, 8.0, 20.0))
    assert paper.execute_decision(card, mark_price=10.0)["status"] == "opened"
    add = paper.execute_decision(card, mark_price=9.0)
    assert add["status"] == "skipped"
    assert "losing" in add["reason"]
