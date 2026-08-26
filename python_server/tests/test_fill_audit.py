"""Paper close vs 1m tape / on-chain swap times. No network."""

from __future__ import annotations

from datetime import datetime, timezone

from decision.fill_audit import compare_close


def _pos(**kwargs):
    opened = datetime(2026, 8, 26, 19, 0, tzinfo=timezone.utc).isoformat()
    closed = datetime(2026, 8, 26, 19, 5, tzinfo=timezone.utc).isoformat()
    base = {
        "id": "abc",
        "address": "Pool111",
        "mint": "Mint111111111111111111111111111111111",
        "name": "TEST",
        "entry_price": 1.0,
        "exit_price": 1.02,
        "size_usd": 80.0,
        "qty": 80.0,
        "realized_pnl_usd": 1.0,
        "opened_at": opened,
        "closed_at": closed,
        "entry_reason": "cluster",
        "close_reason": "max_hold 300s",
    }
    base.update(kwargs)
    return base


def _bar(ts, o, h, l, c):
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": 1.0}


def test_aligned_when_tape_matches_paper_exit():
    t0 = datetime(2026, 8, 26, 19, 0, tzinfo=timezone.utc).timestamp()
    candles = [_bar(t0 + 60, 1.0, 1.03, 0.99, 1.02)]
    out = compare_close(_pos(), candles=candles, now=t0 + 400)
    assert out["verdict"] == "aligned"
    assert out["fast_enough"] is True
    assert out["bars"] == 1


def test_ghost_entry_when_tape_never_traded_at_fill():
    t0 = datetime(2026, 8, 26, 19, 0, tzinfo=timezone.utc).timestamp()
    # Paper filled at 1.00; 1m high never reached 0.92.
    candles = [_bar(t0 + 30, 0.80, 0.85, 0.70, 0.82)]
    out = compare_close(_pos(entry_price=1.0), candles=candles, now=t0 + 400)
    assert out["ghost_entry"] is True
    assert out["verdict"] == "ghost_entry"
    assert out["fast_enough"] is False


def test_barrier_kinder_when_chain_low_worse_than_booked_stop():
    t0 = datetime(2026, 8, 26, 19, 0, tzinfo=timezone.utc).timestamp()
    candles = [_bar(t0 + 30, 1.0, 1.01, 0.50, 0.55)]
    out = compare_close(
        _pos(
            realized_pnl_usd=-1.6,
            exit_price=0.98,
            close_reason="stop_loss $-1.60 (barrier; mark would be $-40.00)",
        ),
        candles=candles,
        now=t0 + 400,
    )
    assert out["barrier_kinder"] is True
    assert out["verdict"] == "barrier_kinder"
    assert out["chain_low_pnl_usd"] < -1.6


def test_too_slow_when_first_onchain_swap_lags_a_dump():
    t0 = datetime(2026, 8, 26, 19, 0, tzinfo=timezone.utc).timestamp()
    candles = [_bar(t0 + 20, 1.0, 1.0, 0.80, 0.85)]
    swaps = [{"ts": t0 + 12.0, "signature": "sig"}]
    out = compare_close(
        _pos(entry_price=1.0),
        candles=candles,
        chain_swaps=swaps,
        now=t0 + 400,
    )
    assert out["first_swap_lag_sec"] == 12.0
    assert out["verdict"] == "too_slow"
    assert out["fast_enough"] is False


def test_no_tape_when_candles_and_dex_missing():
    out = compare_close(_pos(), candles=[], dex=None, chain_swaps=None)
    assert out["verdict"] == "no_tape"
    assert out["fast_enough"] is False
