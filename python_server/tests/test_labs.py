"""Isolated paper books so experiments do not share cash or fills."""

from __future__ import annotations

import time

from decision import labs
from decision import paper
from decision import snipe_feed
from decision import trenches


def test_primary_lane_is_the_hold_book_when_idle(monkeypatch):
    monkeypatch.setattr(labs, "ENABLED", True)
    assert labs.primary_lane_id() == "hold"


def test_primary_lane_prefers_open_lots_over_closed_pnl(monkeypatch):
    monkeypatch.setattr(labs, "ENABLED", True)
    monkeypatch.setattr(
        paper,
        "lane_board",
        lambda: {
            "lanes": [
                {
                    "id": "snipe",
                    "open_count": 0,
                    "closed_count": 5,
                    "realized_pnl_usd": 10.0,
                },
                {
                    "id": "cluster",
                    "open_count": 2,
                    "closed_count": 0,
                    "realized_pnl_usd": 0.0,
                },
                {
                    "id": "hold",
                    "open_count": 0,
                    "closed_count": 0,
                    "realized_pnl_usd": 0.0,
                },
                {
                    "id": "main",
                    "open_count": 1,
                    "closed_count": 0,
                    "realized_pnl_usd": 0.0,
                },
            ]
        },
    )
    assert labs.primary_lane_id() == "cluster"


def test_lane_books_do_not_share_cash():
    paper.reset(starting_cash_usd=1000.0)
    with paper.use_lane("snipe"):
        opened = paper.execute_signal(
            address="PoolA",
            mint="MintA11111111111111111111111111111",
            name="SNIPE",
            mark_price=1.0,
            size_usd=20.0,
            strategy="snipe",
            max_hold_sec=45.0,
        )
        assert opened["status"] == "opened"
        snipe_cash = paper.snapshot()["cash_usd"]
    with paper.use_lane("arb"):
        arb = paper.snapshot()
        assert arb["open_count"] == 0
        assert arb["cash_usd"] == 1000.0
    assert snipe_cash < 1000.0
    board = paper.lane_board()
    by_id = {row["id"]: row for row in board["lanes"]}
    assert by_id["snipe"]["open_count"] == 1
    assert by_id["snipe"]["cash_usd"] == snipe_cash
    assert by_id["arb"]["cash_usd"] == 1000.0


def test_strong_tape_opens_the_snipe_book(monkeypatch, tmp_path):
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
    assert lanes == {"hold", "snipe"}
    assert opens == 2
    with paper.use_lane("hold"):
        assert paper.snapshot()["open_count"] == 1
    with paper.use_lane("snipe"):
        assert paper.snapshot()["open_count"] == 1
    with paper.use_lane("arb"):
        assert paper.snapshot()["open_count"] == 0
        assert paper.snapshot()["cash_usd"] == 1000.0
    trenches.reset_counters()
    paper.reset()


def test_thin_tape_does_not_fill_the_snipe_book(monkeypatch, tmp_path):
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
    assert hits == []
    assert opens == 0
    with paper.use_lane("snipe"):
        assert paper.snapshot()["open_count"] == 0
        assert paper.snapshot()["cash_usd"] == 1000.0
    trenches.reset_counters()
    paper.reset()
