"""Paper cross-pool arb channel."""

from __future__ import annotations

from decision import arb
from decision.config import WRAPPED_SOL_MINT


MEME = "MemeMintArb111111111111111111111111111111"
POOL_CHEAP = "CheapPool1111111111111111111111111111111"
POOL_RICH = "RichPool2222222222222222222222222222222"


def _pair(pool: str, price: float, liq: float, dex: str = "raydium") -> dict:
    return {
        "chainId": "solana",
        "pairAddress": pool,
        "dexId": dex,
        "priceUsd": str(price),
        "liquidity": {"usd": liq},
        "baseToken": {"address": MEME, "symbol": "MEME", "name": "Meme"},
        "quoteToken": {
            "address": WRAPPED_SOL_MINT,
            "symbol": "SOL",
            "name": "Wrapped SOL",
        },
    }


def test_find_opportunities_when_spread_clears_costs(monkeypatch):
    monkeypatch.setattr(arb, "MIN_EDGE_PCT", 0.5)
    monkeypatch.setattr(arb, "MIN_POOL_LIQ_USD", 5_000.0)
    monkeypatch.setattr(arb, "SIZE_USD", 40.0)
    # Force a known cost so the spread math is deterministic.
    monkeypatch.setattr(arb, "_arb_cost_pct", lambda: 3.0)

    monkeypatch.setattr(
        arb,
        "fetch_dexscreener_pairs",
        lambda mint: [
            _pair(POOL_CHEAP, 1.00, 50_000, "raydium"),
            _pair(POOL_RICH, 1.06, 40_000, "orca"),  # +6% gross → +3% net
        ],
    )
    opps = arb.find_opportunities(MEME, symbol="MEME")
    assert len(opps) == 1
    assert opps[0].buy.pair == POOL_CHEAP
    assert opps[0].sell.pair == POOL_RICH
    assert opps[0].gross_pct == 6.0
    assert opps[0].net_pct == 3.0
    assert opps[0].expected_pnl_usd == 1.2  # 3% of $40


def test_no_opportunity_when_spread_inside_costs(monkeypatch):
    monkeypatch.setattr(arb, "MIN_EDGE_PCT", 0.5)
    monkeypatch.setattr(arb, "MIN_POOL_LIQ_USD", 5_000.0)
    monkeypatch.setattr(arb, "_arb_cost_pct", lambda: 3.0)
    monkeypatch.setattr(
        arb,
        "fetch_dexscreener_pairs",
        lambda mint: [
            _pair(POOL_CHEAP, 1.00, 50_000),
            _pair(POOL_RICH, 1.02, 40_000),  # +2% < 3% cost
        ],
    )
    assert arb.find_opportunities(MEME) == []


def test_scan_paper_fills(monkeypatch, tmp_path):
    from decision import store as decision_store

    monkeypatch.setattr(decision_store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(arb, "MIN_EDGE_PCT", 0.5)
    monkeypatch.setattr(arb, "MIN_POOL_LIQ_USD", 5_000.0)
    monkeypatch.setattr(arb, "_arb_cost_pct", lambda: 1.0)
    monkeypatch.setattr(arb, "MAX_PER_TICK", 2)
    monkeypatch.setattr(arb, "REQUIRE_JUPITER", True)
    monkeypatch.setattr(
        arb,
        "_jupiter_verify",
        lambda opp: (True, {"verified": True, "impact_pct": 0.2, "adj_net_pct": opp.net_pct}),
    )

    buf = {
        "addr1": {"mint": MEME, "name": "MEME", "discover": True, "timeframes": {}},
    }
    arb.configure(get_tokens=lambda: buf)
    monkeypatch.setattr(
        arb,
        "fetch_dexscreener_pairs",
        lambda mint: [
            _pair(POOL_CHEAP, 1.0, 20_000),
            _pair(POOL_RICH, 1.05, 20_000),  # +5% gross → +4% net
        ],
    )
    result = arb.scan_once()
    assert result["status"] == "ok"
    assert result["fills_n"] == 1
    assert result["pnl_usd"] > 0
    papers = list(decision_store.read("paper"))
    assert any(r.get("event") == "paper_arb" for r in papers)


def test_jupiter_gate_skips_unroutable(monkeypatch, tmp_path):
    from decision import store as decision_store

    monkeypatch.setattr(decision_store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(arb, "MIN_EDGE_PCT", 0.5)
    monkeypatch.setattr(arb, "MIN_POOL_LIQ_USD", 5_000.0)
    monkeypatch.setattr(arb, "_arb_cost_pct", lambda: 1.0)
    monkeypatch.setattr(arb, "REQUIRE_JUPITER", True)
    monkeypatch.setattr(
        arb,
        "_jupiter_verify",
        lambda opp: (False, {"verified": False, "reason": "no_route"}),
    )
    buf = {"addr1": {"mint": MEME, "name": "MEME", "discover": True}}
    arb.configure(get_tokens=lambda: buf)
    monkeypatch.setattr(
        arb,
        "fetch_dexscreener_pairs",
        lambda mint: [
            _pair(POOL_CHEAP, 1.0, 20_000),
            _pair(POOL_RICH, 1.05, 20_000),
        ],
    )
    result = arb.scan_once()
    assert result["fills_n"] == 0
    assert result["skipped_n"] == 1
    assert list(decision_store.read("paper")) == []


def test_pick_tf_prefers_5s():
    from decision.swarm import _pick_tf

    token = {
        "timeframes": {
            "1": [{"close": 1.0}] * 20,
            "5S": [{"close": 1.0}] * 20,
        }
    }
    assert _pick_tf(token) == "5S"
    assert _pick_tf({"timeframes": {"1": [{"close": 1}] * 20}}) == "1"
