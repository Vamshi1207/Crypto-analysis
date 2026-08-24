"""Pump.fun create-log parser and creator spray filter."""

from __future__ import annotations

import base64

from decision import pumpfun


def test_parse_create_logs_reads_anchor_event():
    mint = bytes(range(32))
    curve = bytes(range(32, 64))
    user = bytes(range(64, 96))
    raw = pumpfun.encode_create_event(
        name="HOT",
        symbol="HOT",
        uri="https://x",
        mint=mint,
        bonding_curve=curve,
        creator=user,
    )
    logs = [
        "Program log: Instruction: Create",
        f"Program data: {base64.b64encode(raw).decode()}",
    ]
    parsed = pumpfun.parse_create_logs(logs)
    assert parsed is not None
    assert parsed["symbol"] == "HOT"
    assert parsed["mint"] == pumpfun.b58encode(mint)
    assert parsed["creator"] == pumpfun.b58encode(user)


def test_parse_create_logs_ignores_non_create():
    assert pumpfun.parse_create_logs(["Program log: Instruction: Buy"]) is None


def test_initial_price_is_tiny_positive():
    px = pumpfun.initial_price_usd(150.0)
    assert 1e-8 < px < 1e-3


def test_creator_book_trips_after_three_in_window():
    book = pumpfun.CreatorBook(max_per_window=3, window_sec=100.0)
    now = 1_000.0
    book.note("Dev", now=now)
    book.note("Dev", now=now + 1)
    assert book.too_hot("Dev", now=now + 2) is False
    book.note("Dev", now=now + 2)
    assert book.too_hot("Dev", now=now + 3) is True
    assert book.too_hot("Dev", now=now + 200) is False


def test_parse_trade_logs_reads_anchor_event():
    mint = bytes(range(32))
    user = bytes(range(64, 96))
    raw = pumpfun.encode_trade_event(
        mint=mint,
        user=user,
        is_buy=True,
        virt_sol=32_000_000_000,
        virt_token=1_000_000_000_000_000,
        real_sol=2_000_000_000,
    )
    logs = [
        "Program log: Instruction: Buy",
        f"Program data: {base64.b64encode(raw).decode()}",
    ]
    trades = pumpfun.parse_trade_logs(logs)
    assert len(trades) == 1
    assert trades[0]["is_buy"] is True
    assert trades[0]["mint"] == pumpfun.b58encode(mint)
    assert trades[0]["real_sol_ui"] == 2.0
    px = pumpfun.curve_price_usd(
        trades[0]["virt_sol"], trades[0]["virt_token"], 150.0
    )
    assert px > pumpfun.initial_price_usd(150.0)


def test_snipe_entry_waits_for_tape_then_buys_on_lift():
    create = 1e-6
    ok, why = pumpfun.snipe_entry(
        create_px=create, last_px=create, unique_buyers=0,
        buys=0, sells=0, real_sol=0.0, age_sec=2.0,
    )
    assert ok is False
    assert why == "waiting_tape"

    ok, why = pumpfun.snipe_entry(
        create_px=create, last_px=create * 1.08, unique_buyers=2,
        buys=3, sells=0, real_sol=0.4, age_sec=8.0,
    )
    assert ok is False
    assert why == "waiting_tape"

    ok, why = pumpfun.snipe_entry(
        create_px=create, last_px=create * 1.08, unique_buyers=2,
        buys=3, sells=0, real_sol=3.0, age_sec=8.0,
    )
    assert ok is True
    assert why == "tape_lift"

    ok, why = pumpfun.snipe_entry(
        create_px=create, last_px=create * 1.20, unique_buyers=1,
        buys=1, sells=0, real_sol=3.0, age_sec=4.0,
    )
    assert ok is True
    assert why == "curve_demand"

    ok, why = pumpfun.snipe_entry(
        create_px=create, last_px=create * 1.08, unique_buyers=2,
        buys=2, sells=4, real_sol=3.0, age_sec=8.0,
    )
    assert ok is False
    assert why == "net_selling"

    ok, why = pumpfun.snipe_entry(
        create_px=create, last_px=create * 1.08, unique_buyers=2,
        buys=3, sells=0, real_sol=3.0, age_sec=80.0, watch_sec=45.0,
    )
    assert ok is False
    assert why == "watch_expired"

    ok, why = pumpfun.snipe_entry(
        create_px=create, last_px=create * 1.20, unique_buyers=3,
        buys=4, sells=1, real_sol=4.0, age_sec=8.0, dev_sold=True,
    )
    assert ok is False
    assert why == "dev_sold"

    ok, why = pumpfun.snipe_entry(
        create_px=create, last_px=create * 1.20, unique_buyers=3,
        buys=4, sells=1, real_sol=2.0, age_sec=8.0, peak_real_sol=6.0,
    )
    assert ok is False
    assert why == "curve_dump"


def test_dev_sell_and_curve_giveback():
    assert pumpfun.is_dev_sell(creator="Dev", user="Dev", is_buy=False) is True
    assert pumpfun.is_dev_sell(creator="Dev", user="Dev", is_buy=True) is False
    assert pumpfun.is_dev_sell(creator="Dev", user="Other", is_buy=False) is False
    assert pumpfun.curve_gave_back(6.0, 2.8) is True
    assert pumpfun.curve_gave_back(6.0, 4.0) is False
    assert pumpfun.curve_gave_back(1.0, 0.4) is False


def test_snipe_size_scales_with_tape():
    assert pumpfun.snipe_size(40.0, lift_pct=6.0, unique_buyers=2, real_sol=3.0) == 20.0
    assert pumpfun.snipe_size(40.0, lift_pct=15.0, unique_buyers=3, real_sol=4.0) == 28.0
    assert pumpfun.snipe_size(40.0, lift_pct=30.0, unique_buyers=5, real_sol=8.0) == 40.0
