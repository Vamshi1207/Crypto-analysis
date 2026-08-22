"""Scalp-mode helpers: short TF selection and $1 profit edge math."""

from __future__ import annotations

from decision.packet import (
    SCALP_SIZE_USD,
    TARGET_PROFIT_USD,
    expected_profit_usd,
    min_edge_pct_for_target,
    pick_decision_timeframe,
)
from decision.schema import MarketPacket, TimeframePacket


def _tf(n: int) -> TimeframePacket:
    return TimeframePacket(timeframe="x", candle_count=n, ohlcv_tail=[])


def test_pick_prefers_5s_when_1m_is_thin():
    pkt = MarketPacket(
        address="x",
        timeframes={
            "1": _tf(3),
            "5S": _tf(40),
            "15S": _tf(20),
        },
    )
    assert pick_decision_timeframe(pkt, preferred="1") == "5S"


def test_pick_keeps_preferred_when_enough_bars():
    pkt = MarketPacket(
        address="x",
        timeframes={"1": _tf(80), "5S": _tf(200)},
    )
    assert pick_decision_timeframe(pkt, preferred="1") == "1"


def test_dollar_edge_for_one_dollar_target():
    # $40 size → need 2.5% net edge for $1
    need = min_edge_pct_for_target(size_usd=40.0, target_usd=1.0)
    assert need == 2.5
    assert expected_profit_usd(2.5, size_usd=40.0) == 1.0
    assert expected_profit_usd(1.0, size_usd=SCALP_SIZE_USD) < TARGET_PROFIT_USD
