"""Hermetic tests for headless discovery + Gecko OHLCV shaping."""

from __future__ import annotations

import time

import pytest

from decision import discover, ohlcv_remote, paper
from decision.config import WRAPPED_SOL_MINT
from decision.schema import SafetyReport, SafetyVerdict


USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MEME_MINT = "MemeMint1111111111111111111111111111111111"
POOL_A = "PoolAAA111111111111111111111111111111111"
POOL_B = "PoolBBB222222222222222222222222222222222"


def _dex_pair(
    *,
    mint: str = MEME_MINT,
    pool: str = POOL_A,
    symbol: str = "MEME",
    liq: float = 50_000.0,
    vol: float = 100_000.0,
    age_min: float = 120.0,
    quote: str = WRAPPED_SOL_MINT,
    buys: int = 40,
    sells: int = 30,
) -> dict:
    created_ms = (time.time() - age_min * 60.0) * 1000.0
    return {
        "chainId": "solana",
        "pairAddress": pool,
        "dexId": "raydium",
        "baseToken": {"address": mint, "symbol": symbol, "name": symbol},
        "quoteToken": {"address": quote, "symbol": "SOL", "name": "Wrapped SOL"},
        "liquidity": {"usd": liq},
        "volume": {"h24": vol},
        "pairCreatedAt": created_ms,
        "txns": {"h1": {"buys": buys, "sells": sells}},
    }


@pytest.fixture(autouse=True)
def _discover_knobs(monkeypatch):
    monkeypatch.setattr(discover, "MIN_LIQUIDITY_USD", 15_000.0)
    monkeypatch.setattr(discover, "MIN_VOLUME_24H_USD", 25_000.0)
    monkeypatch.setattr(discover, "MIN_AGE_MIN", 30.0)
    monkeypatch.setattr(discover, "MAX_AGE_DAYS", 14.0)
    monkeypatch.setattr(discover, "MAX_CANDIDATES", 8)
    monkeypatch.setattr(discover, "MAX_OBSERVE", 16)
    monkeypatch.setattr(discover, "SAFETY_CACHE_SEC", 600.0)
    monkeypatch.setattr(discover, "NO_EDGE_STREAK", 3)
    monkeypatch.setattr(discover, "NO_EDGE_COOLDOWN_SEC", 1800.0)
    monkeypatch.setattr(discover, "EXPLORE_MULTIPLIER", 4)
    discover._safety_ok.clear()
    discover._safety_bad.clear()
    discover._cool_until.clear()
    discover._no_edge_streak.clear()
    discover._cool_meta.clear()


def test_gecko_rows_to_candles_sorts_ascending():
    raw = [
        [1_700_000_120, 2, 3, 1, 2.5, 10],
        [1_700_000_060, 1, 2, 0.5, 2, 5],
        [1_700_000_000, 1, 1.5, 0.8, 1, 3],
    ]
    candles = ohlcv_remote.gecko_rows_to_candles(raw)
    assert [c["timestamp"] for c in candles] == [1_700_000_000, 1_700_000_060, 1_700_000_120]
    assert candles[-1]["close"] == 2.5


def test_build_live_token_marks_discover():
    candles = ohlcv_remote.gecko_rows_to_candles(
        [[1_700_000_000 + i * 60, 1, 1, 1, 1.0 + i * 0.01, 1] for i in range(5)]
    )
    tok = ohlcv_remote.build_live_token(
        name="MEME",
        mint=MEME_MINT,
        pool_address=POOL_A,
        candles=candles,
    )
    assert tok["discover"] is True
    assert tok["source"] == "geckoterminal"
    assert len(tok["timeframes"]["1"]) == 5
    assert tok["mint"] == MEME_MINT


def test_prefilter_drops_thin_and_brand_new():
    thin = discover.Candidate(
        mint=MEME_MINT,
        pool=POOL_A,
        symbol="THIN",
        name="Thin",
        liquidity_usd=1_000.0,
        volume_24h_usd=100_000.0,
        pair_created_at_ms=(time.time() - 120 * 60) * 1000,
        source="test",
    )
    fresh = discover.Candidate(
        mint=MEME_MINT,
        pool=POOL_B,
        symbol="NEW",
        name="New",
        liquidity_usd=50_000.0,
        volume_24h_usd=100_000.0,
        pair_created_at_ms=(time.time() - 5 * 60) * 1000,
        source="test",
    )
    ok = discover.Candidate(
        mint=MEME_MINT,
        pool="PoolOK",
        symbol="OK",
        name="Ok",
        liquidity_usd=50_000.0,
        volume_24h_usd=100_000.0,
        pair_created_at_ms=(time.time() - 120 * 60) * 1000,
        tx_h1=80,
        boost_amount=200,
        source="test",
    )
    kept = discover.prefilter([thin, fresh, ok])
    assert len(kept) == 1
    assert kept[0].symbol == "OK"
    assert kept[0].score > 0
    assert thin.reject_reason and "liquidity" in thin.reject_reason
    assert fresh.reject_reason and "age" in fresh.reject_reason


def test_prefilter_drops_stable_base():
    c = discover.Candidate(
        mint=WRAPPED_SOL_MINT,
        pool=POOL_A,
        symbol="SOL",
        name="Wrapped SOL",
        liquidity_usd=1_000_000.0,
        volume_24h_usd=1_000_000.0,
        pair_created_at_ms=(time.time() - 120 * 60) * 1000,
        source="test",
    )
    assert discover.prefilter([c]) == []
    assert c.reject_reason


def test_scan_candidates_merges_boost_and_gecko(monkeypatch):
    pair = _dex_pair()

    monkeypatch.setattr(
        discover,
        "fetch_dexscreener_boosts",
        lambda which="latest": [
            {
                "chainId": "solana",
                "tokenAddress": MEME_MINT,
                "amount": 50,
                "totalAmount": 500,
            }
        ],
    )
    monkeypatch.setattr(
        discover,
        "fetch_dexscreener_pairs",
        lambda mint: [pair] if mint == MEME_MINT else [],
    )
    monkeypatch.setattr(
        discover,
        "fetch_gecko_trending_pools",
        lambda page=1: [
            {
                "attributes": {
                    "address": POOL_A,
                    "name": "MEME / SOL",
                    "reserve_in_usd": "60000",
                    "volume_usd": {"h24": "200000"},
                    "transactions": {"h1": {"buys": 10, "sells": 10}},
                    "pool_created_at": "2026-08-20T00:00:00Z",
                },
                "relationships": {
                    "base_token": {"data": {"id": f"solana_{MEME_MINT}"}},
                    "quote_token": {"data": {"id": f"solana_{WRAPPED_SOL_MINT}"}},
                },
            }
        ],
    )

    cands = discover.scan_candidates()
    assert len(cands) == 1
    assert cands[0].pool == POOL_A
    assert cands[0].boost_amount == 500
    assert "boost" in cands[0].source or "gecko" in cands[0].source


def test_scan_and_hydrate_writes_token_buffer(monkeypatch):
    buf: dict = {}

    def set_token(addr, tok):
        buf[addr] = tok

    discover.configure(set_token=set_token, get_tokens=lambda: buf)

    cand = discover.Candidate(
        mint=MEME_MINT,
        pool=POOL_A,
        symbol="MEME",
        name="Meme",
        liquidity_usd=50_000.0,
        volume_24h_usd=100_000.0,
        pair_created_at_ms=(time.time() - 120 * 60) * 1000,
        boost_amount=100,
        tx_h1=50,
        source="test",
        score=5.0,
    )
    monkeypatch.setattr(discover, "scan_candidates", lambda: [cand])
    monkeypatch.setattr(
        discover,
        "check_token",
        lambda mint: SafetyReport(mint=mint, verdict=SafetyVerdict.SAFE, risk_score=5),
    )
    candles = [
        {
            "timestamp": 1_700_000_000 + i * 60,
            "open": 1.0,
            "high": 1.1,
            "low": 0.9,
            "close": 1.0 + i * 0.01,
            "volume": 10.0,
        }
        for i in range(64)
    ]
    monkeypatch.setattr(
        discover.ohlcv_remote,
        "fetch_pool_ohlcv",
        lambda pool, aggregate=1, limit=300: candles,
    )
    paper.set_kill_switch(False)

    result = discover.scan_once()
    assert result["status"] == "ok"
    assert POOL_A in buf
    assert buf[POOL_A]["discover"] is True
    assert len(buf[POOL_A]["timeframes"]["1"]) == 64
    assert result["watchlist"] == [POOL_A]


def test_gate0_danger_skips_hydrate(monkeypatch):
    buf: dict = {}
    discover.configure(set_token=lambda a, t: buf.__setitem__(a, t), get_tokens=lambda: buf)

    cand = discover.Candidate(
        mint=MEME_MINT,
        pool=POOL_A,
        symbol="RUG",
        name="Rug",
        liquidity_usd=50_000.0,
        volume_24h_usd=100_000.0,
        pair_created_at_ms=(time.time() - 120 * 60) * 1000,
        source="test",
        score=5.0,
    )
    monkeypatch.setattr(discover, "scan_candidates", lambda: [cand])
    monkeypatch.setattr(
        discover,
        "check_token",
        lambda mint: SafetyReport(mint=mint, verdict=SafetyVerdict.DANGER, risk_score=90),
    )
    called = {"ohlcv": False}

    def boom(*_a, **_k):
        called["ohlcv"] = True
        raise AssertionError("should not fetch ohlcv")

    monkeypatch.setattr(discover.ohlcv_remote, "fetch_pool_ohlcv", boom)
    result = discover.scan_once()
    assert result["status"] == "ok"
    assert buf == {}
    assert called["ohlcv"] is False
    assert any("gate0" in (r.get("reason") or "") for r in result.get("rejected") or [])


def test_stub_candles_and_observe_lite(monkeypatch):
    candles = ohlcv_remote.stub_candles_from_price(1.23, bars=16, step_sec=60)
    assert len(candles) == 16
    assert candles[-1]["close"] == 1.23

    buf: dict = {}
    discover.configure(set_token=lambda a, t: buf.__setitem__(a, t), get_tokens=lambda: buf)
    monkeypatch.setattr(discover, "MAX_CANDIDATES", 1)
    monkeypatch.setattr(discover, "MAX_OBSERVE", 3)

    trade = discover.Candidate(
        mint=MEME_MINT,
        pool=POOL_A,
        symbol="TRADE",
        name="Trade",
        liquidity_usd=50_000,
        volume_24h_usd=100_000,
        pair_created_at_ms=(time.time() - 120 * 60) * 1000,
        score=100,
        source="test",
        price_usd=1.0,
    )
    obs = discover.Candidate(
        mint="ObsMint3333333333333333333333333333333",
        pool=POOL_B,
        symbol="OBS",
        name="Obs",
        liquidity_usd=40_000,
        volume_24h_usd=80_000,
        pair_created_at_ms=(time.time() - 120 * 60) * 1000,
        score=10,
        source="test",
        price_usd=0.5,
    )
    monkeypatch.setattr(discover, "scan_candidates", lambda: [trade, obs])
    monkeypatch.setattr(
        discover,
        "check_token",
        lambda mint: SafetyReport(mint=mint, verdict=SafetyVerdict.SAFE, risk_score=5),
    )
    real_candles = [
        {
            "timestamp": 1_700_000_000 + i * 60,
            "open": 1.0,
            "high": 1.1,
            "low": 0.9,
            "close": 1.0,
            "volume": 1.0,
        }
        for i in range(32)
    ]
    monkeypatch.setattr(
        discover.ohlcv_remote,
        "fetch_pool_ohlcv",
        lambda pool, aggregate=1, limit=300: real_candles,
    )
    result = discover.scan_once()
    assert result["trade_n"] == 1
    assert result["observe_n"] == 1
    assert buf[POOL_A]["tradeable"] is True
    assert buf[POOL_B]["tradeable"] is False
    assert buf[POOL_B].get("observe_lite") is True

    from decision.schema import (
        Action,
        DecideMode,
        DecisionCard,
        Direction,
        ForecastEnsemble,
        ReturnBand,
        RiskBlock,
    )

    monkeypatch.setattr(discover, "NO_EDGE_STREAK", 3)
    monkeypatch.setattr(discover, "NO_EDGE_COOLDOWN_SEC", 3600.0)

    buf: dict = {
        POOL_A: {
            "discover": True,
            "mint": MEME_MINT,
            "name": "MEME",
            "timeframes": {"1": []},
        }
    }
    discover.configure(set_token=lambda a, t: buf.__setitem__(a, t), get_tokens=lambda: buf)

    rb = ReturnBand(p10=-5, p50=-1, p90=3)

    def _hold_card():
        return DecisionCard(
            token={
                "address": POOL_A,
                "mint": MEME_MINT,
                "name": "MEME",
                "source": "geckoterminal",
            },
            horizon_bars=6,
            timeframe="1",
            action=Action.HOLD,
            action_confidence=0.5,
            confidence_basis="test",
            direction=Direction.DOWN,
            expected_return_pct=rb,
            cost_adjusted_edge_pct=-4.0,
            position={"max_size_usd": 40, "size_basis": "test"},
            risk=RiskBlock(risk_pass=True),
            forecast=ForecastEnsemble(
                horizon_bars=6,
                timeframe="1",
                raw=rb,
                calibrated=rb,
                agreement=0.8,
                models=[],
                residual_count=10,
                backend="stat",
            ),
            gates_failed=["gate2"],
            mode=DecideMode.FAST,
        )

    for _ in range(2):
        discover.note_decision(_hold_card())
        assert not discover._is_cooling(MEME_MINT)
        assert POOL_A in buf

    discover.note_decision(_hold_card())
    assert discover._is_cooling(MEME_MINT)
    assert POOL_A in buf
    assert buf[POOL_A].get("tradeable") is False
    assert buf[POOL_A].get("cooled") is True
    assert discover.status()["cooling_count"] >= 1


def test_scan_skips_cooled_mints_for_next_names(monkeypatch):
    """Top-scored mint on cooldown → trade hydrates the next; cool stays observe."""
    buf: dict = {}
    discover.configure(set_token=lambda a, t: buf.__setitem__(a, t), get_tokens=lambda: buf)
    monkeypatch.setattr(discover, "MAX_CANDIDATES", 1)
    monkeypatch.setattr(discover, "MAX_OBSERVE", 8)
    monkeypatch.setattr(discover, "NO_EDGE_COOLDOWN_SEC", 3600.0)

    cool = discover.Candidate(
        mint="CoolMint1111111111111111111111111111111",
        pool="CoolPool",
        symbol="COOL",
        name="Cool",
        liquidity_usd=50_000,
        volume_24h_usd=200_000,
        pair_created_at_ms=(time.time() - 120 * 60) * 1000,
        score=999,
        source="test",
        price_usd=1.0,
    )
    nxt = discover.Candidate(
        mint="NextMint2222222222222222222222222222222",
        pool="NextPool",
        symbol="NEXT",
        name="Next",
        liquidity_usd=40_000,
        volume_24h_usd=150_000,
        pair_created_at_ms=(time.time() - 120 * 60) * 1000,
        score=50,
        source="test",
        price_usd=1.0,
    )
    discover._park_mint(cool.mint, symbol="COOL", reason="gate2_no_edge", edge=-5.0)

    monkeypatch.setattr(discover, "scan_candidates", lambda: [cool, nxt])
    monkeypatch.setattr(
        discover,
        "check_token",
        lambda mint: SafetyReport(mint=mint, verdict=SafetyVerdict.SAFE, risk_score=5),
    )
    candles = [
        {
            "timestamp": 1_700_000_000 + i * 60,
            "open": 1.0,
            "high": 1.1,
            "low": 0.9,
            "close": 1.0,
            "volume": 1.0,
        }
        for i in range(32)
    ]
    monkeypatch.setattr(
        discover.ohlcv_remote,
        "fetch_pool_ohlcv",
        lambda pool, aggregate=1, limit=300: candles,
    )

    result = discover.scan_once()
    assert result["status"] == "ok"
    assert "NextPool" in result["watchlist"]
    assert "COOL" in (result.get("cooled_skipped") or [])
    assert "NextPool" in buf
    assert buf["NextPool"].get("tradeable") is True
    # Cooled mint stays hydrated for dashboard volume as observe.
    assert "CoolPool" in buf
    assert buf["CoolPool"].get("tradeable") is False
    assert result.get("trade_n") == 1
    assert result.get("observe_n") >= 1
