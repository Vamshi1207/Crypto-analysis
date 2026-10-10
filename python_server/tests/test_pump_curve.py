"""Pump bonding-curve math vectors (reference worked example)."""

from __future__ import annotations

from decision import pump_curve


def test_doc_vector_buy_tenth_sol():
    # Reference: 0.1 SOL pre-fee buys ~3,564,784,053,156 raw units.
    out = pump_curve.quote_buy_tokens(
        0.1e9,
        pump_curve.INITIAL_VIRTUAL_TOKEN,
        pump_curve.INITIAL_VIRTUAL_SOL,
        pump_curve.INITIAL_REAL_TOKEN,
        protocol_bps=0,
        creator_bps=0,
    )
    assert out == 3564784053156.0 or abs(out - 3564784053156.0) / 3564784053156.0 < 1e-6


def test_fees_reduce_output():
    plain = pump_curve.quote_buy_tokens(
        0.1e9, pump_curve.INITIAL_VIRTUAL_TOKEN, pump_curve.INITIAL_VIRTUAL_SOL,
        pump_curve.INITIAL_REAL_TOKEN, protocol_bps=0, creator_bps=0,
    )
    taxed = pump_curve.quote_buy_tokens(
        0.1e9, pump_curve.INITIAL_VIRTUAL_TOKEN, pump_curve.INITIAL_VIRTUAL_SOL,
        pump_curve.INITIAL_REAL_TOKEN,
    )
    assert 0 < taxed < plain


def test_round_trip_loses_to_fees():
    v_tok, v_sol, real = (
        pump_curve.INITIAL_VIRTUAL_TOKEN,
        pump_curve.INITIAL_VIRTUAL_SOL,
        pump_curve.INITIAL_REAL_TOKEN,
    )
    got = pump_curve.quote_buy_tokens(1e9, v_tok, v_sol, real)
    back = pump_curve.quote_sell_sol(got, v_tok - got, v_sol + 1e9 * 10000 / 10125)
    assert back < 1e9


def test_impact_grows_with_size():
    v_tok, v_sol, real = (
        pump_curve.INITIAL_VIRTUAL_TOKEN,
        pump_curve.INITIAL_VIRTUAL_SOL,
        pump_curve.INITIAL_REAL_TOKEN,
    )
    small = pump_curve.buy_impact_pct(0.01e9, v_tok, v_sol, real)
    big = pump_curve.buy_impact_pct(5e9, v_tok, v_sol, real)
    assert small is not None and big is not None
    assert 0 <= small < 0.5
    assert big > small


def test_zero_reserves_quote_zero():
    assert pump_curve.quote_buy_tokens(1e9, 0.0, 30e9, 100.0) == 0.0
    assert pump_curve.quote_sell_sol(100.0, 0.0, 30e9) == 0.0
    assert pump_curve.buy_impact_pct(1e9, 0.0, 30e9, 100.0) is None


def test_guard_blocks_dust_pool_allows_normal():
    ok, _, impact = pump_curve.guard_buy(
        size_usd=40.0, price_usd=1e-5, liquidity_usd=20000.0, sol_usd=150.0
    )
    assert ok is True and impact is not None and impact < 5.0
    ok, reason, _ = pump_curve.guard_buy(
        size_usd=40.0, price_usd=1e-5, liquidity_usd=20.0, sol_usd=150.0,
        max_impact_pct=10.0,
    )
    assert ok is False and reason.startswith("curve_thin")


def test_guard_skips_without_basis():
    ok, reason, impact = pump_curve.guard_buy(
        size_usd=40.0, price_usd=0.0, liquidity_usd=None, sol_usd=150.0
    )
    assert ok is True and impact is None
