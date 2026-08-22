"""Paper cross-pool arb channel — conservative sizing / stress / cooldown."""

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


def _patch_basic(monkeypatch, *, cost: float = 3.0):
    monkeypatch.setattr(arb, "MIN_EDGE_PCT", 0.5)
    monkeypatch.setattr(arb, "MIN_POOL_LIQ_USD", 5_000.0)
    monkeypatch.setattr(arb, "SIZE_USD", 40.0)
    monkeypatch.setattr(arb, "MIN_SIZE_USD", 10.0)
    monkeypatch.setattr(arb, "MAX_POOL_FRAC", 0.01)
    monkeypatch.setattr(arb, "STRESS_GAP_FRAC", 0.5)
    monkeypatch.setattr(arb, "REQUIRE_CROSS_DEX", True)
    monkeypatch.setattr(arb, "_arb_cost_pct", lambda: cost)


def test_find_opportunities_when_spread_clears_stress(monkeypatch):
    _patch_basic(monkeypatch, cost=3.0)
    # +10% gross → net 7%, stress 2% — both clear 0.5% min edge
    monkeypatch.setattr(
        arb,
        "fetch_dexscreener_pairs",
        lambda mint: [
            _pair(POOL_CHEAP, 1.00, 50_000, "raydium"),
            _pair(POOL_RICH, 1.10, 40_000, "orca"),
        ],
    )
    opps = arb.find_opportunities(MEME, symbol="MEME")
    assert len(opps) == 1
    assert opps[0].buy.pair == POOL_CHEAP
    assert opps[0].sell.pair == POOL_RICH
    assert opps[0].gross_pct == 10.0
    assert opps[0].net_pct == 7.0
    assert opps[0].stress_net_pct == 2.0
    assert opps[0].size_usd == 40.0
    assert opps[0].stress_pnl_usd == 0.8


def test_stress_skips_when_half_gap_inside_costs(monkeypatch):
    _patch_basic(monkeypatch, cost=3.0)
    # +6% gross → net 3% clears, stress 0% does not clear 0.5%
    monkeypatch.setattr(
        arb,
        "fetch_dexscreener_pairs",
        lambda mint: [
            _pair(POOL_CHEAP, 1.00, 50_000, "raydium"),
            _pair(POOL_RICH, 1.06, 40_000, "orca"),
        ],
    )
    assert arb.find_opportunities(MEME) == []


def test_same_dex_skipped(monkeypatch):
    _patch_basic(monkeypatch, cost=1.0)
    monkeypatch.setattr(
        arb,
        "fetch_dexscreener_pairs",
        lambda mint: [
            _pair(POOL_CHEAP, 1.00, 50_000, "raydium"),
            _pair(POOL_RICH, 1.12, 40_000, "raydium"),
        ],
    )
    assert arb.find_opportunities(MEME) == []


def test_size_capped_by_thin_pool(monkeypatch):
    _patch_basic(monkeypatch, cost=1.0)
    monkeypatch.setattr(arb, "MAX_POOL_FRAC", 0.002)  # 0.2% of 10k = $20
    monkeypatch.setattr(
        arb,
        "fetch_dexscreener_pairs",
        lambda mint: [
            _pair(POOL_CHEAP, 1.00, 10_000, "raydium"),
            _pair(POOL_RICH, 1.12, 80_000, "orca"),
        ],
    )
    opps = arb.find_opportunities(MEME)
    assert len(opps) == 1
    assert opps[0].size_usd == 20.0


def test_scan_books_conservative_pnl(monkeypatch, tmp_path):
    from decision import store as decision_store

    monkeypatch.setattr(decision_store, "STORE_DIR", tmp_path / "store")
    _patch_basic(monkeypatch, cost=1.0)
    monkeypatch.setattr(arb, "MAX_PER_TICK", 2)
    monkeypatch.setattr(arb, "REQUIRE_JUPITER", True)
    monkeypatch.setattr(arb, "MINT_COOLDOWN_SEC", 0.0)
    monkeypatch.setattr(arb, "MAX_FILLS_PER_MINT_DAY", 5)
    arb.reset_counters()

    def fake_jup(opp):
        booked = opp.stress_net_pct - 0.2
        return True, {
            "verified": True,
            "impact_pct": 0.1,
            "booked_net_pct": booked,
            "optimistic_net_pct": opp.net_pct,
        }

    monkeypatch.setattr(arb, "_jupiter_verify", fake_jup)

    buf = {
        "addr1": {"mint": MEME, "name": "MEME", "discover": True, "timeframes": {}},
    }
    arb.configure(get_tokens=lambda: buf)
    monkeypatch.setattr(
        arb,
        "fetch_dexscreener_pairs",
        lambda mint: [
            _pair(POOL_CHEAP, 1.0, 50_000, "raydium"),
            _pair(POOL_RICH, 1.12, 50_000, "orca"),  # +12% → stress 5% - cost 1% = 5%
        ],
    )
    result = arb.scan_once()
    assert result["status"] == "ok"
    assert result["fills_n"] == 1
    papers = list(decision_store.read("paper"))
    fill = next(r for r in papers if r.get("event") == "paper_arb")
    # Booked = stress 5% - 0.2 = 4.8% of $40 = 1.92
    assert fill["realized_pnl_usd"] == 1.92
    assert fill["optimistic_pnl_usd"] > fill["realized_pnl_usd"]
    assert fill["mode"] == "paper_atomic_sim_conservative"


def test_cooldown_optional_when_enabled_blocks_second_fill(monkeypatch, tmp_path):
    from decision import store as decision_store

    monkeypatch.setattr(decision_store, "STORE_DIR", tmp_path / "store")
    _patch_basic(monkeypatch, cost=1.0)
    monkeypatch.setattr(arb, "REQUIRE_JUPITER", True)
    monkeypatch.setattr(arb, "MINT_COOLDOWN_SEC", 600.0)
    monkeypatch.setattr(arb, "MAX_FILLS_PER_MINT_DAY", 5)
    arb.reset_counters()
    monkeypatch.setattr(
        arb,
        "_jupiter_verify",
        lambda opp: (True, {"verified": True, "booked_net_pct": opp.stress_net_pct}),
    )
    buf = {"addr1": {"mint": MEME, "name": "MEME"}}
    arb.configure(get_tokens=lambda: buf)
    monkeypatch.setattr(
        arb,
        "fetch_dexscreener_pairs",
        lambda mint: [
            _pair(POOL_CHEAP, 1.0, 50_000, "raydium"),
            _pair(POOL_RICH, 1.12, 50_000, "orca"),
        ],
    )
    first = arb.scan_once()
    second = arb.scan_once()
    assert first["fills_n"] == 1
    assert second["fills_n"] == 0
    assert second["skipped_cooldown_n"] == 1


def test_default_no_cooldown_allows_refill_when_analysis_clears(monkeypatch, tmp_path):
    from decision import store as decision_store

    monkeypatch.setattr(decision_store, "STORE_DIR", tmp_path / "store")
    _patch_basic(monkeypatch, cost=1.0)
    monkeypatch.setattr(arb, "REQUIRE_JUPITER", True)
    monkeypatch.setattr(arb, "MINT_COOLDOWN_SEC", 0.0)
    monkeypatch.setattr(arb, "MAX_FILLS_PER_MINT_DAY", 0)
    arb.reset_counters()
    monkeypatch.setattr(
        arb,
        "_jupiter_verify",
        lambda opp: (True, {"verified": True, "booked_net_pct": opp.stress_net_pct}),
    )
    buf = {"addr1": {"mint": MEME, "name": "MEME"}}
    arb.configure(get_tokens=lambda: buf)
    monkeypatch.setattr(
        arb,
        "fetch_dexscreener_pairs",
        lambda mint: [
            _pair(POOL_CHEAP, 1.0, 50_000, "raydium"),
            _pair(POOL_RICH, 1.12, 50_000, "orca"),
        ],
    )
    first = arb.scan_once()
    second = arb.scan_once()
    assert first["fills_n"] == 1
    assert second["fills_n"] == 1
    assert second["skipped_cooldown_n"] == 0


def test_jupiter_gate_skips_unroutable(monkeypatch, tmp_path):
    from decision import store as decision_store

    monkeypatch.setattr(decision_store, "STORE_DIR", tmp_path / "store")
    _patch_basic(monkeypatch, cost=1.0)
    monkeypatch.setattr(arb, "REQUIRE_JUPITER", True)
    arb.reset_counters()
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
            _pair(POOL_CHEAP, 1.0, 50_000, "raydium"),
            _pair(POOL_RICH, 1.12, 50_000, "orca"),
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
