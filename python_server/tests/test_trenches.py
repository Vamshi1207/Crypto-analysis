"""Paper launch + cluster channel."""

from __future__ import annotations

import time

import pytest

from decision import paper
from decision import snipe_feed
from decision import trenches
from decision.config import WRAPPED_SOL_MINT


@pytest.fixture(autouse=True)
def _no_onchain_curve(monkeypatch):
    """Unit tests never hit Helius RPC; exact-path tests stub per case."""
    monkeypatch.setattr(trenches, "_curve_state", lambda mint: None)


def test_parse_swaps_counts_wallet_buy_and_sell():
    wallet = "Wallet111111111111111111111111111111111"
    meme = "MemeMint1111111111111111111111111111111"
    txs = [
        {
            "timestamp": 1_700_000_100,
            "signature": "sig1",
            "tokenTransfers": [
                {
                    "mint": meme,
                    "toUserAccount": wallet,
                    "fromUserAccount": "Pool",
                    "tokenAmount": 10,
                }
            ],
        },
        {
            "timestamp": 1_700_000_200,
            "signature": "sig2",
            "tokenTransfers": [
                {
                    "mint": meme,
                    "toUserAccount": "Pool",
                    "fromUserAccount": wallet,
                    "tokenAmount": 10,
                }
            ],
        },
        {
            "timestamp": 1_700_000_150,
            "tokenTransfers": [
                {
                    "mint": WRAPPED_SOL_MINT,
                    "toUserAccount": wallet,
                }
            ],
        },
    ]
    events = trenches.parse_swaps(txs, wallet=wallet)
    assert [(e["side"], e["mint"]) for e in events] == [("buy", meme), ("sell", meme)]


def test_cluster_score_needs_three_wallets_in_window():
    now = 1_000.0
    buyers = {"a": now - 10, "b": now - 20, "c": now - 200}
    assert trenches.cluster_score(buyers, now=now) == 2
    buyers["c"] = now - 5
    assert trenches.cluster_score(buyers, now=now) == 3


def _cluster_row(mint="Mint111111111111111111111111111111111"):
    return {
        "mint": mint,
        "pool": "Pool111111111111111111111111111111111",
        "symbol": "TEST",
        "liquidity_usd": 20000.0,
        "price_usd": 1e-5,
        "age_min": 3.0,
    }


def _chair(action="hold", veto=False):
    return {
        "veto": veto,
        "action": action,
        "claim": f"jev {action}",
        "reason": f"jev {action}",
        "backend": "jev",
        "confidence": 0.8,
        "veto_prob": 0.9 if veto else 0.1,
        "quality": 0.5,
        "probs": {},
        "latency_ms": 5,
        "model": "jev-latest",
    }


def _wire_chair(monkeypatch, review):
    from decision import jev as jev_mod

    monkeypatch.setattr(trenches, "JEV_CLUSTER_GATE", True)
    monkeypatch.setattr(jev_mod, "chair_review", lambda *a, **k: review)
    trenches._jev_blocked.clear()
    trenches._jev_proceed.clear()


def test_jev_gate_veto_blocks_cluster_open(monkeypatch):
    _wire_chair(monkeypatch, _chair(action="avoid", veto=True))
    row = _cluster_row()
    proceed, reason = trenches._jev_cluster_gate(row, ["a", "b", "c"], 6.0)
    assert proceed is False
    assert reason.startswith("jev_veto")


def test_jev_gate_avoid_blocks_cluster_open(monkeypatch):
    _wire_chair(monkeypatch, _chair(action="avoid"))
    proceed, reason = trenches._jev_cluster_gate(row := _cluster_row(), ["a", "b", "c"], 6.0)
    assert proceed is False
    assert reason.startswith("jev_avoid")


def test_jev_gate_hold_and_buy_proceed(monkeypatch):
    for action in ("hold", "buy"):
        _wire_chair(monkeypatch, _chair(action=action))
        proceed, reason = trenches._jev_cluster_gate(
            _cluster_row(mint=f"Mint{action}11111111111111111111111111"), ["a", "b", "c"], 6.0
        )
        assert proceed is True, reason


def test_jev_gate_offline_chair_proceeds(monkeypatch):
    _wire_chair(monkeypatch, None)
    proceed, reason = trenches._jev_cluster_gate(_cluster_row(), ["a", "b", "c"], 6.0)
    assert proceed is True
    assert reason == "jev_offline"


def test_jev_gate_block_cools_down_without_reasking(monkeypatch):
    from decision import jev as jev_mod

    calls = []
    monkeypatch.setattr(trenches, "JEV_CLUSTER_GATE", True)
    monkeypatch.setattr(
        jev_mod, "chair_review", lambda *a, **k: calls.append(1) or _chair(veto=True)
    )
    trenches._jev_blocked.clear()
    trenches._jev_proceed.clear()
    row = _cluster_row()
    assert trenches._jev_cluster_gate(row, ["a", "b", "c"], 6.0)[0] is False
    assert trenches._jev_cluster_gate(row, ["a", "b", "c"], 6.0) == (False, "jev_cooldown")
    assert len(calls) == 1


def test_jev_gate_state_carries_edge_and_safety(monkeypatch):
    """Gate0 report passes through as packet safety; edge = m5 − costs."""
    from types import SimpleNamespace

    from decision import costs as costs_mod
    from decision import jev as jev_mod

    seen = {}

    def fake_chair(card, packet, opinions):
        seen["card"] = card
        seen["packet"] = packet
        return _chair(action="hold")

    monkeypatch.setattr(trenches, "JEV_CLUSTER_GATE", True)
    monkeypatch.setattr(jev_mod, "chair_review", fake_chair)
    trenches._jev_blocked.clear()
    trenches._jev_proceed.clear()
    report = SimpleNamespace(
        verdict=SimpleNamespace(value="caution"),
        risk_score=30,
        summary=lambda: "caution (score 30)",
        sellability=SimpleNamespace(price_impact_pct=0.5),
    )
    ok, reason = trenches._jev_cluster_gate(_cluster_row(), ["a"] * 6, 6.0, report)
    assert ok is True, reason
    assert seen["packet"].safety is report
    expected = round(6.0 - costs_mod.round_trip_cost_pct(price_impact_pct=0.5), 2)
    assert seen["card"].cost_adjusted_edge_pct == expected


def test_list_launch_candidates_keeps_young_liquid_pools(monkeypatch):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(trenches, "LAUNCH_MAX_AGE_MIN", 12.0)
    monkeypatch.setattr(trenches, "LAUNCH_MIN_LIQ_USD", 3_000.0)
    created = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()

    def fake_pools(*, kind="new_pools", page=1):
        return [
            {
                "attributes": {
                    "address": "PoolYoung",
                    "name": "HOT/SOL",
                    "reserve_in_usd": "8000",
                    "base_token_price_usd": "0.001",
                    "pool_created_at": created,
                },
                "relationships": {
                    "base_token": {"data": {"id": "solana_MemeMint1111111111111111111111111111"}}
                },
            },
            {
                "attributes": {
                    "address": "PoolOld",
                    "name": "OLD/SOL",
                    "reserve_in_usd": "80000",
                    "base_token_price_usd": "1",
                    "pool_created_at": "2020-01-01T00:00:00+00:00",
                },
                "relationships": {
                    "base_token": {"data": {"id": "solana_OldMint111111111111111111111111111"}}
                },
            },
        ]

    monkeypatch.setattr(trenches, "fetch_gecko_pool_list", fake_pools)
    rows = trenches.list_launch_candidates()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "HOT"
    assert rows[0]["pool"] == "PoolYoung"


def test_pick_dex_price_prefers_the_entry_pool():
    pairs = [
        {"pairAddress": "Ghost", "priceUsd": "9.00", "liquidity": {"usd": 100}},
        {"pairAddress": "PoolYoung", "priceUsd": "0.001", "liquidity": {"usd": 8000}},
    ]
    assert trenches.pick_dex_price(pairs, pool="PoolYoung") == 0.001
    # Without a pool, use the most liquid pair — not the highest mid.
    assert trenches.pick_dex_price(pairs, pool=None) == 0.001


def test_dex_m5_pct_reads_chosen_pair(monkeypatch):
    pairs = [
        {
            "pairAddress": "PoolYoung",
            "priceUsd": "0.001",
            "liquidity": {"usd": 8000},
            "priceChange": {"m5": 12.5},
        }
    ]
    monkeypatch.setattr(trenches, "fetch_dexscreener_pairs", lambda mint: pairs)
    assert trenches.dex_m5_pct("Mint", pool="PoolYoung") == 12.5
    monkeypatch.setattr(
        trenches,
        "fetch_dexscreener_pairs",
        lambda mint: [{"pairAddress": "X", "priceUsd": "1", "liquidity": {"usd": 1}}],
    )
    assert trenches.dex_m5_pct("Mint") is None


def test_confirm_entry_price_skips_gecko_dex_gap(monkeypatch):
    monkeypatch.setattr(trenches, "MAX_PRICE_DIVERGE_PCT", 8.0)
    monkeypatch.setattr(trenches, "dex_price_usd", lambda mint, pool=None: 0.0004)
    price, why = trenches.confirm_entry_price(
        mint="Mint", pool="Pool", gecko_usd=0.001
    )
    assert price is None
    assert "price_diverge" in why

    monkeypatch.setattr(trenches, "dex_price_usd", lambda mint, pool=None: 0.00102)
    price, why = trenches.confirm_entry_price(
        mint="Mint", pool="Pool", gecko_usd=0.001
    )
    assert price == 0.00102
    assert why == "ok"


def test_list_sniper_candidates_keeps_only_seconds_old_pools(monkeypatch):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(trenches, "SNIPER_MAX_AGE_SEC", 90.0)
    monkeypatch.setattr(trenches, "SNIPER_MIN_LIQ_USD", 1_500.0)
    fresh = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
    stale = (datetime.now(timezone.utc) - timedelta(minutes=8)).isoformat()

    def fake_pools(*, kind="new_pools", page=1):
        return [
            {
                "attributes": {
                    "address": "PoolFresh",
                    "name": "NEW/SOL",
                    "reserve_in_usd": "4000",
                    "base_token_price_usd": "0.0002",
                    "pool_created_at": fresh,
                },
                "relationships": {
                    "base_token": {"data": {"id": "solana_NewMint111111111111111111111111111"}}
                },
            },
            {
                "attributes": {
                    "address": "PoolLate",
                    "name": "OLD/SOL",
                    "reserve_in_usd": "40000",
                    "base_token_price_usd": "0.01",
                    "pool_created_at": stale,
                },
                "relationships": {
                    "base_token": {"data": {"id": "solana_OldSnipe11111111111111111111111111"}}
                },
            },
        ]

    monkeypatch.setattr(trenches, "fetch_gecko_pool_list", fake_pools)
    rows = trenches.list_sniper_candidates()
    assert len(rows) == 1
    assert rows[0]["pool"] == "PoolFresh"
    assert rows[0]["age_sec"] < 90


def test_snipe_hard_block_rejects_freeze(monkeypatch):
    monkeypatch.setattr(
        trenches,
        "fetch_mint_account",
        lambda mint: {"freezeAuthority": "BadGuy", "extensions": []},
    )
    ok, reason = trenches._snipe_hard_block("Mint")
    assert ok is False
    assert "freeze" in reason


def test_scan_sniper_watches_create_instead_of_buying(monkeypatch):
    trenches.reset_counters()
    paper.reset(starting_cash_usd=1000.0)
    create_px = 1e-6
    monkeypatch.setattr(trenches, "_sol_usd", lambda: 150.0)
    monkeypatch.setattr(trenches, "initial_price_usd", lambda sol: create_px)
    monkeypatch.setattr(
        snipe_feed,
        "drain",
        lambda limit=32: [
            {
                "mint": "MintWatch111111111111111111111111111",
                "symbol": "WAIT",
                "creator": "DevWatch11111111111111111111111111",
                "bonding_curve": "CurveWatch",
                "seen_at": time.time(),
            }
        ],
    )
    monkeypatch.setattr(snipe_feed, "tapes", lambda sol_usd=150: {})
    seen, opens, skipped, expired, hits = trenches._scan_sniper()
    assert opens == 0
    assert hits == []
    assert "MintWatch111111111111111111111111111" in trenches._watches
    assert paper.snapshot()["open_count"] == 0
    trenches.reset_counters()


def test_scan_sniper_buys_only_after_tape_lifts(monkeypatch, tmp_path):
    from decision import store as decision_store

    monkeypatch.setattr(decision_store, "STORE_DIR", tmp_path / "store")
    trenches.reset_counters()
    paper.reset(starting_cash_usd=1000.0)
    _wire_chair(monkeypatch, _chair(action="hold"))
    create_px = 1e-6
    mint = "MintHot11111111111111111111111111111"
    monkeypatch.setattr(trenches, "_sol_usd", lambda: 150.0)
    monkeypatch.setattr(trenches, "initial_price_usd", lambda sol: create_px)
    monkeypatch.setattr(trenches, "SNIPER_MIN_LIQ_USD", 400.0)
    monkeypatch.setattr(trenches, "SNIPER_MIN_BUYERS", 2)
    monkeypatch.setattr(trenches, "SNIPER_MIN_LIFT_PCT", 5.0)
    monkeypatch.setattr(trenches, "SNIPER_MIN_REAL_SOL", 2.5)
    monkeypatch.setattr(
        snipe_feed,
        "drain",
        lambda limit=32: [
            {
                "mint": mint,
                "symbol": "HOT",
                "creator": "DevHot111111111111111111111111111",
                "bonding_curve": "CurveHot",
                "seen_at": time.time(),
            }
        ],
    )
    monkeypatch.setattr(
        snipe_feed,
        "tapes",
        lambda sol_usd=150: {
            mint: {
                "last_px": create_px * 1.08,
                "unique_buyers": 2,
                "buys": 3,
                "sells": 0,
                "real_sol": 3.0,
            }
        },
    )
    seen, opens, skipped, expired, hits = trenches._scan_sniper()
    assert opens == 1
    assert hits[0]["entry_why"] == "tape_lift"
    assert hits[0]["buyers"] == 2
    snap = paper.snapshot()
    assert snap["open_count"] == 1
    pos = snap["open"][0]
    assert pos["entry_reason"] == "snipe"
    assert pos["stop_loss_usd"] == trenches.SNIPER_STOP_USD
    assert pos["size_usd"] < trenches.SNIPER_SIZE_USD
    trenches.reset_counters()
    paper.reset()


def test_scan_sniper_skips_thin_tape(monkeypatch):
    trenches.reset_counters()
    paper.reset(starting_cash_usd=1000.0)
    create_px = 1e-6
    mint = "MintThin111111111111111111111111111"
    monkeypatch.setattr(trenches, "_sol_usd", lambda: 150.0)
    monkeypatch.setattr(trenches, "initial_price_usd", lambda sol: create_px)
    monkeypatch.setattr(trenches, "SNIPER_MIN_LIQ_USD", 400.0)
    monkeypatch.setattr(
        snipe_feed,
        "drain",
        lambda limit=32: [
            {
                "mint": mint,
                "symbol": "THIN",
                "creator": "DevThin11111111111111111111111111",
                "bonding_curve": "CurveThin",
                "seen_at": time.time(),
            }
        ],
    )
    monkeypatch.setattr(
        snipe_feed,
        "tapes",
        lambda sol_usd=150: {
            mint: {
                "last_px": create_px * 1.08,
                "unique_buyers": 2,
                "buys": 3,
                "sells": 0,
                "real_sol": 0.6,
            }
        },
    )
    seen, opens, skipped, expired, hits = trenches._scan_sniper()
    assert opens == 0
    assert hits == []
    assert mint in trenches._watches
    trenches.reset_counters()
    paper.reset()


def test_scan_sniper_skips_after_dev_sell(monkeypatch):
    trenches.reset_counters()
    paper.reset(starting_cash_usd=1000.0)
    create_px = 1e-6
    mint = "MintRug11111111111111111111111111111"
    monkeypatch.setattr(trenches, "_sol_usd", lambda: 150.0)
    monkeypatch.setattr(trenches, "initial_price_usd", lambda sol: create_px)
    monkeypatch.setattr(
        snipe_feed,
        "drain",
        lambda limit=32: [
            {
                "mint": mint,
                "symbol": "RUG",
                "creator": "DevRug111111111111111111111111111",
                "bonding_curve": "CurveRug",
                "seen_at": time.time(),
            }
        ],
    )
    monkeypatch.setattr(
        snipe_feed,
        "tapes",
        lambda sol_usd=150: {
            mint: {
                "last_px": create_px * 1.20,
                "unique_buyers": 4,
                "buys": 5,
                "sells": 1,
                "real_sol": 4.0,
                "peak_real_sol": 4.0,
                "dev_sold": True,
            }
        },
    )
    seen, opens, skipped, expired, hits = trenches._scan_sniper()
    assert opens == 0
    assert expired >= 1
    assert mint not in trenches._watches
    trenches.reset_counters()


def test_snipe_feed_marks_dev_sell_and_peak_sol():
    snipe_feed.clear_tape()
    mint = "MintTape111111111111111111111111111"
    creator = "DevTape111111111111111111111111111"
    snipe_feed._note_create(
        {"mint": mint, "creator": creator, "symbol": "TAPE", "bonding_curve": "Curve"}
    )
    snipe_feed._note_trade(
        {
            "mint": mint,
            "user": "Buyer11111111111111111111111111111",
            "is_buy": True,
            "real_sol_ui": 6.0,
            "virt_sol": 36_000_000_000,
            "virt_token": 1_000_000_000_000_000,
        }
    )
    snipe_feed._note_trade(
        {
            "mint": mint,
            "user": creator,
            "is_buy": False,
            "real_sol_ui": 2.4,
            "virt_sol": 32_000_000_000,
            "virt_token": 1_050_000_000_000_000,
        }
    )
    tape = snipe_feed.tapes(sol_usd=150.0)[mint]
    assert tape["dev_sold"] is True
    assert tape["peak_real_sol"] == 6.0
    assert tape["real_sol"] == 2.4
    snipe_feed.clear_tape()


def test_tape_force_reason_flags_dev_and_curve_dump():
    assert trenches._tape_force_reason({"dev_sold": True}) == "dev_sell"
    assert trenches._tape_force_reason(
        {"dev_sold": False, "peak_real_sol": 8.0, "real_sol": 3.0}
    ) == "curve_dump"
    assert trenches._tape_force_reason(
        {"dev_sold": False, "peak_real_sol": 8.0, "real_sol": 7.0}
    ) is None


def test_execute_signal_and_cluster_sell(tmp_path, monkeypatch):
    from decision import store as decision_store

    monkeypatch.setattr(decision_store, "STORE_DIR", tmp_path / "store")
    paper.reset(starting_cash_usd=1000.0)
    opened = paper.execute_signal(
        address="PoolA",
        mint="MintA",
        name="HOT",
        mark_price=1.0,
        size_usd=80.0,
        strategy="cluster",
        max_hold_sec=180.0,
    )
    assert opened["status"] == "opened"
    snap = paper.snapshot()
    assert snap["open_count"] == 1
    assert snap["cash_usd"] < 1000.0
    closed = paper.close_by_mint(mint="MintA", mark_price=1.1, reason="cluster_sell abc")
    assert len(closed) == 1
    assert paper.snapshot()["open_count"] == 0
    assert paper.snapshot()["closed_count"] == 1


def _snipe_row():
    return {
        "mint": "SnipeMint11111111111111111111111111111",
        "pool": "Pool111111111111111111111111111111111",
        "symbol": "SNIPE",
        "liquidity_usd": 5000.0,
        "price_usd": 1e-6,
        "age_sec": 20.0,
        "buyers_unique": 6,
        "buys": 10,
        "sells": 1,
        "real_sol": 12.0,
        "peak_real_sol": 12.0,
        "lift_pct": 18.0,
        "size_usd": 20.0,
    }


def test_jev_gate_covers_launch_strategy(monkeypatch):
    _wire_chair(monkeypatch, _chair(action="avoid", veto=True))
    row = _cluster_row()
    proceed, reason = trenches._jev_cluster_gate(
        row, ["a", "b"], None, None, strategy="launch"
    )
    assert proceed is False
    assert reason.startswith("jev_veto")


def test_jev_gate_covers_snipe_tape(monkeypatch):
    _wire_chair(monkeypatch, _chair(action="avoid", veto=True))
    proceed, reason = trenches._jev_cluster_gate(
        _snipe_row(), [], None, None, strategy="snipe"
    )
    assert proceed is False
    assert reason.startswith("jev_veto")


def test_jev_gate_snipe_hold_proceeds(monkeypatch):
    _wire_chair(monkeypatch, _chair(action="hold"))
    proceed, _ = trenches._jev_cluster_gate(
        _snipe_row(), [], None, None, strategy="snipe"
    )
    assert proceed is True


def test_snipe_proceed_is_shared_across_lanes(monkeypatch):
    from decision import jev as jev_mod

    calls = []
    monkeypatch.setattr(trenches, "JEV_SNIPER_GATE", True)
    monkeypatch.setattr(
        jev_mod, "chair_review", lambda *a, **k: calls.append(1) or _chair(action="hold")
    )
    trenches._jev_blocked.clear()
    trenches._jev_proceed.clear()
    row = _snipe_row()
    first = trenches._jev_cluster_gate(row, [], None, None, strategy="snipe")
    second = trenches._jev_cluster_gate(row, [], None, None, strategy="snipe")
    assert first == (True, "jev_hold")
    assert second == (True, "jev_hold")
    assert len(calls) == 1


def test_snipe_proceed_reasks_after_watch(monkeypatch):
    from decision import jev as jev_mod

    calls = []
    clock = {"now": 1_000.0}
    monkeypatch.setattr(trenches, "JEV_SNIPER_GATE", True)
    monkeypatch.setattr(trenches, "JEV_SNIPER_PROCEED_SEC", 10.0)
    monkeypatch.setattr(trenches.time, "time", lambda: clock["now"])
    monkeypatch.setattr(
        jev_mod, "chair_review", lambda *a, **k: calls.append(1) or _chair(action="buy")
    )
    trenches._jev_blocked.clear()
    trenches._jev_proceed.clear()
    row = _snipe_row()
    assert trenches._jev_cluster_gate(row, [], None, None, strategy="snipe")[0] is True
    clock["now"] = 1_011.0
    assert trenches._jev_cluster_gate(row, [], None, None, strategy="snipe") == (True, "jev_buy")
    assert len(calls) == 2


def test_snipe_offline_is_not_cached(monkeypatch):
    from decision import jev as jev_mod

    calls = []
    monkeypatch.setattr(trenches, "JEV_SNIPER_GATE", True)
    monkeypatch.setattr(jev_mod, "chair_review", lambda *a, **k: calls.append(1) or None)
    trenches._jev_blocked.clear()
    trenches._jev_proceed.clear()
    row = _snipe_row()
    assert trenches._jev_cluster_gate(row, [], None, None, strategy="snipe") == (True, "jev_offline")
    assert trenches._jev_cluster_gate(row, [], None, None, strategy="snipe") == (True, "jev_offline")
    assert len(calls) == 2


def test_cluster_proceed_still_reasks(monkeypatch):
    from decision import jev as jev_mod

    calls = []
    monkeypatch.setattr(trenches, "JEV_CLUSTER_GATE", True)
    monkeypatch.setattr(
        jev_mod, "chair_review", lambda *a, **k: calls.append(1) or _chair(action="hold")
    )
    trenches._jev_blocked.clear()
    trenches._jev_proceed.clear()
    row = _cluster_row()
    assert trenches._jev_cluster_gate(row, ["a", "b", "c"], 6.0)[0] is True
    assert trenches._jev_cluster_gate(row, ["a", "b", "c"], 6.0)[0] is True
    assert len(calls) == 2


def test_jev_gate_flag_off_per_strategy(monkeypatch):
    from decision import jev as jev_mod

    def boom(*a, **k):
        raise AssertionError("chair must not be called")

    monkeypatch.setattr(jev_mod, "chair_review", boom)
    monkeypatch.setattr(trenches, "JEV_SNIPER_GATE", False)
    trenches._jev_blocked.clear()
    trenches._jev_proceed.clear()
    proceed, reason = trenches._jev_cluster_gate(
        _snipe_row(), [], None, None, strategy="snipe"
    )
    assert proceed is True
    assert reason == "jev_gate_off"


def _safe_report(blocking=False, verdict="ok"):
    from types import SimpleNamespace

    return SimpleNamespace(
        blocking=blocking,
        verdict=SimpleNamespace(value=verdict),
        risk_score=90 if blocking else 5,
        summary=lambda: f"{verdict} (score {90 if blocking else 5})",
        sellability=SimpleNamespace(price_impact_pct=0.2),
    )


def _wire_gate0(monkeypatch, report=None):
    monkeypatch.setattr(
        trenches, "check_token", lambda mint: report or _safe_report()
    )


def test_snipe_gated_open_blocks_on_veto(monkeypatch):
    _wire_chair(monkeypatch, _chair(action="avoid", veto=True))
    _wire_gate0(monkeypatch)
    row = dict(_snipe_row())
    out = trenches._snipe_gated_open(
        row, _tape={"lift_pct": 18.0}, strategy="snipe", size=20.0, hold=45.0
    )
    assert out["status"] == "skipped"
    assert "jev_veto" in out["reason"]


def test_snipe_gated_open_blocks_on_gate0(monkeypatch):
    from decision import jev as jev_mod

    def boom(*a, **k):
        raise AssertionError("chair must not be reached past a Gate0 block")

    monkeypatch.setattr(jev_mod, "chair_review", boom)
    _wire_gate0(monkeypatch, _safe_report(blocking=True, verdict="danger"))
    out = trenches._snipe_gated_open(
        dict(_snipe_row()), _tape={}, strategy="snipe", size=20.0, hold=45.0
    )
    assert out["status"] == "skipped"
    assert out["reason"].startswith("snipe_gate0_")


def test_snipe_gated_open_passes_through_on_hold(monkeypatch, tmp_path):
    from decision import store as decision_store

    monkeypatch.setattr(decision_store, "STORE_DIR", tmp_path / "store")
    paper.reset(starting_cash_usd=1000.0)
    _wire_chair(monkeypatch, _chair(action="hold"))
    _wire_gate0(monkeypatch)
    monkeypatch.setattr(trenches, "confirm_entry_price", lambda mint, pool=None, gecko_usd=0: (1e-6, "ok"))
    row = dict(_snipe_row())
    out = trenches._snipe_gated_open(
        row, _tape={"lift_pct": 18.0}, strategy="snipe", size=20.0, hold=45.0,
        confirm_dex=False,
    )
    assert out["status"] == "opened"


def test_m5_bounds_keep_momentum_reject_blowoff(monkeypatch):
    monkeypatch.setattr(trenches, "CLUSTER_MIN_M5_PCT", 5.0)
    monkeypatch.setattr(trenches, "CLUSTER_MAX_M5_PCT", 150.0)
    assert trenches._m5_ok(30.0) == (True, "")
    assert trenches._m5_ok(None) == (True, "")
    ok, why = trenches._m5_ok(2.0)
    assert ok is False and why.startswith("m5_low")
    ok, why = trenches._m5_ok(772.0)
    assert ok is False and why.startswith("m5_high")


def test_m5_bounds_off_disables_both(monkeypatch):
    monkeypatch.setattr(trenches, "CLUSTER_MIN_M5_PCT", 0.0)
    monkeypatch.setattr(trenches, "CLUSTER_MAX_M5_PCT", 0.0)
    assert trenches._m5_ok(0.01)[0] is True
    assert trenches._m5_ok(9999.0)[0] is True


def test_copy_leaders_counts_windowed_buys(monkeypatch):
    import time as _time

    monkeypatch.setattr(trenches, "COPY_ENABLED", True)
    monkeypatch.setattr(trenches, "COPY_WINDOW_SEC", 120.0)
    now = _time.time()
    monkeypatch.setattr(
        trenches, "_buys", {"M1": {"w1": now - 10, "w2": now - 5000}}
    )
    assert trenches._copy_leaders("M1", now=now) == ["w1"]
    assert trenches._copy_leaders("MISSING", now=now) == []


def test_copy_opens_on_leader_buy(monkeypatch, tmp_path):
    import time as _time

    from decision import jev as jev_mod
    from decision import labs as labs_mod
    from decision import store as decision_store
    from types import SimpleNamespace

    monkeypatch.setattr(decision_store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(labs_mod, "ENABLED", True)
    monkeypatch.setattr(trenches, "COPY_ENABLED", True)
    monkeypatch.setattr(trenches, "COPY_MIN_LEADERS", 1)
    monkeypatch.setattr(trenches, "SNIPER_ENABLED", False)
    monkeypatch.setattr(
        jev_mod, "chair_review",
        lambda *a, **k: {
            "veto": False, "action": "hold", "claim": "jev hold",
            "reason": "jev hold", "backend": "jev", "confidence": 0.8,
            "veto_prob": 0.1, "quality": 0.5, "probs": {}, "latency_ms": 5,
            "model": "jev-latest",
        },
    )
    monkeypatch.setattr(
        trenches, "check_token",
        lambda mint: SimpleNamespace(
            blocking=False, verdict=SimpleNamespace(value="ok"),
            risk_score=5, summary=lambda: "ok",
            sellability=SimpleNamespace(price_impact_pct=0.2),
        ),
    )
    monkeypatch.setattr(trenches, "dex_m5_pct", lambda mint, pool=None: 10.0)
    monkeypatch.setattr(
        trenches, "confirm_entry_price", lambda mint, pool=None, gecko_usd=0: (1e-5, "ok")
    )
    mint = "CopyMint11111111111111111111111111111"
    row = dict(_cluster_row(mint))
    monkeypatch.setattr(trenches, "list_launch_candidates", lambda: [row])
    monkeypatch.setattr(trenches, "unique_buyers_recent", lambda *a, **k: [])
    trenches.reset_counters()
    monkeypatch.setattr(trenches, "_buys", {mint: {"leader1": _time.time()}})
    paper.reset(starting_cash_usd=1000.0)
    result = trenches._scan(do_cluster=True)
    assert result["copy_fires"] == 1
    assert result["copy_opens"] == 1
    with paper.use_lane("copy"):
        assert paper.snapshot()["open_count"] == 1


def test_copy_skips_without_leaders(monkeypatch):
    monkeypatch.setattr(trenches, "COPY_ENABLED", True)
    monkeypatch.setattr(trenches, "COPY_MIN_LEADERS", 1)
    monkeypatch.setattr(trenches, "_buys", {})
    assert trenches._copy_leaders("M1") == []


def test_toxic_flow_needs_sample_and_supermajority(monkeypatch):
    monkeypatch.setattr(trenches, "TOX_MIN_SWAPS", 6)
    monkeypatch.setattr(trenches, "TOX_MAX_SELL_FRAC", 0.6)
    assert trenches.toxic_flow(1, 4) == (False, "")
    assert trenches.toxic_flow(2, 8)[0] is True
    assert trenches.toxic_flow(8, 2)[0] is False


def test_flow_toxicity_counts_sides(monkeypatch):
    now = time.time()
    evs = (
        [{"side": "buy", "mint": "M", "wallet": f"b{i}", "ts": now - 5} for i in range(8)]
        + [{"side": "sell", "mint": "M", "wallet": "s1", "ts": now - 5} for _ in range(2)]
    )
    monkeypatch.setattr(trenches, "_mint_swaps", lambda mint: evs)
    tox = trenches.flow_toxicity("M", window_s=120.0)
    assert tox["swaps"] == 10 and tox["sells"] == 2
    assert tox["toxic"] is False


def test_open_refuses_completed_curve(monkeypatch, tmp_path):
    from decision import store as decision_store

    monkeypatch.setattr(decision_store, "STORE_DIR", tmp_path / "store")
    paper.reset(starting_cash_usd=1000.0)
    monkeypatch.setattr(
        trenches, "_curve_state",
        lambda mint: {"complete": True, "sol_quoted": True, "v_tok": 0,
                      "v_sol": 0, "real_tok": 0, "real_sol": 0},
    )
    out = trenches._open(
        dict(_cluster_row()), strategy="cluster", size=40.0, hold=300.0,
        confirm_dex=False,
    )
    assert out["status"] == "skipped"
    assert out["reason"] == "curve_complete"


def test_open_uses_exact_impact(monkeypatch, tmp_path):
    from decision import store as decision_store

    monkeypatch.setattr(decision_store, "STORE_DIR", tmp_path / "store")
    paper.reset(starting_cash_usd=1000.0)
    monkeypatch.setattr(
        trenches, "_curve_state",
        lambda mint: {"complete": False, "sol_quoted": True,
                      "v_tok": 1.073e15, "v_sol": 30e9,
                      "real_tok": 793.1e12, "real_sol": 0.0},
    )
    out = trenches._open(
        dict(_cluster_row()), strategy="cluster", size=40.0, hold=300.0,
        confirm_dex=False,
    )
    assert out["status"] in ("opened", "pending")
