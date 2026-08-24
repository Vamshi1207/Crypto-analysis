"""Paper launch + cluster channel."""

from __future__ import annotations

import time

from decision import paper
from decision import snipe_feed
from decision import trenches
from decision.config import WRAPPED_SOL_MINT


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
