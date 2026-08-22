"""Axiom pair-stats → 5m orderflow for Gate 3."""

from __future__ import annotations

from decision.packet import _orderflow_from_sources, _sum_stat_buckets, packet_from_candles
from decision.schema import MarketSnapshot
from decision.vibe import _orderflow_specialist
from decision.schema import (
    Action,
    DecisionCard,
    Direction,
    ForecastEnsemble,
    MarketPacket,
    ReturnBand,
    RiskBlock,
)


def _buckets():
    # 5 one-minute buckets; last ones dominate sells (like CATE 21b/89s pattern).
    return [
        {"createdAt": f"2026-08-22T16:0{i}:00Z", "buyCount": 2, "sellCount": 10,
         "buyVolumeSol": 1.0, "sellVolumeSol": 3.0, "priceSol": 0.1}
        for i in range(5)
    ]


def test_sum_stat_buckets_last_5m():
    out = _sum_stat_buckets(_buckets(), last_n=5)
    assert out["buys_5m"] == 10
    assert out["sells_5m"] == 50
    assert out["buy_sell_ratio_5m"] == 0.2
    assert out["stats_buckets_5m"] == 5


def test_orderflow_prefers_axiom_5m_over_dexscreener_h1():
    snap = MarketSnapshot(buys_h1=120, sells_h1=40, volume_h1=1000.0)
    of = _orderflow_from_sources(snap, _buckets())
    assert of["source"] == "axiom_pair_stats_5m"
    assert of["buys_5m"] == 10
    assert of["sells_5m"] == 50
    assert of["buy_sell_ratio"] == 0.2
    # h1 still present as secondary
    assert of["buys_h1"] == 120


def test_orderflow_falls_back_to_h1_without_stats():
    snap = MarketSnapshot(buys_h1=120, sells_h1=40)
    of = _orderflow_from_sources(snap, None)
    assert of["source"] == "dexscreener_h1"
    assert of["buy_sell_ratio"] == 3.0


def test_packet_from_live_token_uses_stats():
    token = {
        "name": "CATE",
        "timeframes": {
            "5S": [
                {"timestamp": i, "open": 1, "high": 1, "low": 1, "close": 1.0, "volume": 1}
                for i in range(20)
            ]
        },
        "stats": _buckets(),
    }
    pkt = packet_from_candles(
        address="PoolCATE",
        name="CATE",
        timeframes=token["timeframes"],
        axiom_stats=token["stats"],
    )
    assert pkt.orderflow["source"] == "axiom_pair_stats_5m"
    assert pkt.orderflow["sells_5m"] == 50


def _card() -> DecisionCard:
    rb = ReturnBand(p10=-2, p50=3, p90=8)
    return DecisionCard(
        token={"address": "x", "mint": "m", "name": "T"},
        horizon_bars=6,
        timeframe="5S",
        action=Action.HOLD,
        action_confidence=0.5,
        confidence_basis="test",
        direction=Direction.SIDEWAYS,
        expected_return_pct=rb,
        cost_adjusted_edge_pct=0.0,
        position={"max_size_usd": 0, "size_basis": "test"},
        risk=RiskBlock(risk_pass=False),
        forecast=ForecastEnsemble(
            horizon_bars=6,
            timeframe="5S",
            raw=rb,
            calibrated=rb,
            agreement=0.5,
        ),
    )


def test_orderflow_specialist_uses_5m_sell_pressure():
    pkt = MarketPacket(
        address="x",
        orderflow={
            "source": "axiom_pair_stats_5m",
            "buys_5m": 21,
            "sells_5m": 89,
            "buy_sell_ratio": round(21 / 89, 3),
        },
    )
    op = _orderflow_specialist(_card(), pkt)
    assert op.vote == "avoid"
    assert "5m" in op.claim


def test_orderflow_specialist_buys_on_5m_imbalance():
    pkt = MarketPacket(
        address="x",
        orderflow={
            "source": "axiom_pair_stats_5m",
            "buys_5m": 40,
            "sells_5m": 10,
            "buy_sell_ratio": 4.0,
        },
    )
    op = _orderflow_specialist(_card(), pkt)
    assert op.vote == "buy"
    assert "5m" in op.claim
