"""Isolated paper books so experiments do not share cash or fills."""

from __future__ import annotations

import time

from decision import labs
from decision import paper
from decision import snipe_feed
from decision import trenches


def test_lane_books_do_not_share_cash():
    paper.reset(starting_cash_usd=1000.0)
    with paper.use_lane("snipe_wide"):
        opened = paper.execute_signal(
            address="PoolA",
            mint="MintA11111111111111111111111111111",
            name="WIDE",
            mark_price=1.0,
            size_usd=20.0,
            strategy="snipe",
            max_hold_sec=45.0,
        )
        assert opened["status"] == "opened"
        wide_cash = paper.snapshot()["cash_usd"]
    with paper.use_lane("snipe_tight"):
        tight = paper.snapshot()
        assert tight["open_count"] == 0
        assert tight["cash_usd"] == 1000.0
    assert wide_cash < 1000.0
    board = paper.lane_board()
    by_id = {row["id"]: row for row in board["lanes"]}
    assert by_id["snipe_wide"]["open_count"] == 1
    assert by_id["snipe_tight"]["cash_usd"] == 1000.0


def test_wide_and_tight_can_both_take_the_same_create(monkeypatch, tmp_path):
    from decision import store as decision_store

    monkeypatch.setattr(decision_store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(labs, "ENABLED", True)
    labs.clear_seen()
    trenches.reset_counters()
    paper.reset(starting_cash_usd=1000.0)
    create_px = 1e-6
    mint = "MintBoth111111111111111111111111111"
    monkeypatch.setattr(trenches, "_sol_usd", lambda: 150.0)
    monkeypatch.setattr(trenches, "initial_price_usd", lambda sol: create_px)
    monkeypatch.setattr(
        snipe_feed,
        "drain",
        lambda limit=32: [
            {
                "mint": mint,
                "symbol": "BOTH",
                "creator": "DevBoth11111111111111111111111111",
                "bonding_curve": "CurveBoth",
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
                "unique_buyers": 5,
                "buys": 8,
                "sells": 0,
                "real_sol": 12.0,
            }
        },
    )
    seen, opens, skipped, expired, hits = trenches._scan_sniper()
    lanes = {h["lane"] for h in hits}
    assert "snipe_wide" in lanes
    assert "snipe_tight" in lanes
    assert opens >= 2
    with paper.use_lane("snipe_wide"):
        assert paper.snapshot()["open_count"] == 1
    with paper.use_lane("snipe_tight"):
        assert paper.snapshot()["open_count"] == 1
    with paper.use_lane("snipe_rip"):
        assert paper.snapshot()["open_count"] == 1
    trenches.reset_counters()
    paper.reset()


def test_thin_tape_only_fills_the_wide_book(monkeypatch, tmp_path):
    from decision import store as decision_store

    monkeypatch.setattr(decision_store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(labs, "ENABLED", True)
    labs.clear_seen()
    trenches.reset_counters()
    paper.reset(starting_cash_usd=1000.0)
    create_px = 1e-6
    mint = "MintThinLabs1111111111111111111111"
    monkeypatch.setattr(trenches, "_sol_usd", lambda: 150.0)
    monkeypatch.setattr(trenches, "initial_price_usd", lambda sol: create_px)
    monkeypatch.setattr(
        snipe_feed,
        "drain",
        lambda limit=32: [
            {
                "mint": mint,
                "symbol": "THIN",
                "creator": "DevThinLabs111111111111111111111",
                "bonding_curve": "CurveThinLabs",
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
    assert [h["lane"] for h in hits] == ["snipe_wide"]
    with paper.use_lane("snipe_wide"):
        assert paper.snapshot()["open_count"] == 1
    with paper.use_lane("snipe_tight"):
        assert paper.snapshot()["open_count"] == 0
        assert paper.snapshot()["cash_usd"] == 1000.0
    trenches.reset_counters()
    paper.reset()
