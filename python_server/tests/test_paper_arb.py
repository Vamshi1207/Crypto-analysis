"""Shared paper ledger for atomic arb round-trips."""

from __future__ import annotations

from decision import paper


def test_arb_fill_updates_shared_cash_and_realized():
    paper.reset(starting_cash_usd=1000.0)
    record = {"event": "paper_arb", "symbol": "MEME", "realized_pnl_usd": 2.5}
    result = paper.execute_arb_fill(
        size_usd=40.0,
        pnl_usd=2.5,
        mint="Mint111",
        symbol="MEME",
        record=record,
    )
    assert result["status"] == "filled"
    snap = paper.snapshot()
    assert snap["cash_usd"] == 1002.5
    assert snap["realized_pnl_usd"] == 2.5
    assert snap["arb_realized_pnl_usd"] == 2.5
    assert snap["arb_fills"] == 1
    assert snap["arb_notional_usd"] == 40.0
    assert snap["scalp_realized_pnl_usd"] == 0.0


def test_arb_fill_refuses_when_cash_insufficient():
    paper.reset(starting_cash_usd=30.0)
    result = paper.execute_arb_fill(
        size_usd=40.0,
        pnl_usd=1.0,
        mint="Mint111",
        symbol="MEME",
        record={"event": "paper_arb"},
    )
    assert result["status"] == "refused"
    assert paper.snapshot()["cash_usd"] == 30.0
    assert paper.snapshot()["arb_fills"] == 0
