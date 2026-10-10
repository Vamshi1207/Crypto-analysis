"""Pump.fun bonding-curve math for paper quoting (no RPC, no trades).

Mirrors the constant-product formulas documented in the pump-fun-sdk
``bonding-curve-math`` reference (virtual reserves ``k = vTok × vSol``,
fee deduction before the swap, real-reserve caps, graduation when the
curve completes). Float math throughout: good to ~1e-9 relative, while
the chain uses integer lamports — never use this to build transactions.

Reserve inputs come from :func:`estimate_reserves`, a documented
approximation (Dex price + liquidity anchored to the known initial
curve), because this desk has no on-chain account reads. Anything that
needs exact reserves must fetch the curve account; until then every
consumer must treat outputs as estimates and fail soft on None.
"""

from __future__ import annotations

from typing import Optional, Tuple

# Worked example in the reference: 1.073e15 / 30e9 / 793.1e12, 1B supply.
INITIAL_VIRTUAL_TOKEN = 1.073e15
INITIAL_VIRTUAL_SOL = 30e9
INITIAL_REAL_TOKEN = 793.1e12

# Classic-curve graduation heuristic (real SOL in curve at completion).
# v3 curves differ — callers judging graduation must treat this as rough.
GRADUATION_SOL = 85.0

# Example fee tier from the reference (0.95% protocol + 0.30% creator).
# Tiers vary by market cap; always pass the applicable tier explicitly
# when known instead of relying on these defaults.
DEFAULT_PROTOCOL_BPS = 95
DEFAULT_CREATOR_BPS = 30


def _fee_mult(protocol_bps: int, creator_bps: int) -> float:
    return 10000.0 / (float(protocol_bps + creator_bps) + 10000.0)


def quote_buy_tokens(
    sol_in: float,
    v_tok: float,
    v_sol: float,
    real_tok: float,
    protocol_bps: int = DEFAULT_PROTOCOL_BPS,
    creator_bps: int = DEFAULT_CREATOR_BPS,
) -> float:
    """Tokens out for ``sol_in`` SOL (fees deducted first, capped at real)."""
    if sol_in <= 0 or v_tok <= 0 or v_sol <= 0 or real_tok <= 0:
        return 0.0
    net = sol_in * _fee_mult(protocol_bps, creator_bps)
    out = net * v_tok / (v_sol + net)
    return min(out, real_tok)


def quote_sell_sol(
    tok_in: float,
    v_tok: float,
    v_sol: float,
    protocol_bps: int = DEFAULT_PROTOCOL_BPS,
    creator_bps: int = DEFAULT_CREATOR_BPS,
) -> float:
    """SOL out for ``tok_in`` tokens (fees subtracted, floored at 0)."""
    if tok_in <= 0 or v_tok <= 0 or v_sol <= 0:
        return 0.0
    raw = tok_in * v_sol / (v_tok + tok_in)
    return max(0.0, raw * _fee_mult(protocol_bps, creator_bps))


def spot_price(v_tok: float, v_sol: float) -> float:
    """Marginal price (SOL per token) before fees."""
    if v_tok <= 0:
        return 0.0
    return v_sol / v_tok


def buy_impact_pct(
    sol_in: float,
    v_tok: float,
    v_sol: float,
    real_tok: float,
) -> Optional[float]:
    """Curve price movement for a buy of ``sol_in`` SOL, in percent.

    Measured ex-fee (pure constant-product movement): fees are modeled
    separately in the cost layer, and mixing them in would flag every
    trade for the ~1.25% fee drag regardless of depth.
    """
    px = spot_price(v_tok, v_sol)
    if px <= 0 or sol_in <= 0:
        return None
    out = quote_buy_tokens(sol_in, v_tok, v_sol, real_tok, 0, 0)
    if out <= 0:
        return None
    eff = sol_in / out
    return max(0.0, (eff - px) / px * 100.0)


def market_cap_sol(v_tok: float, v_sol: float, supply: float) -> float:
    """Curve market cap in SOL: vSol × supply / vTok."""
    if v_tok <= 0:
        return 0.0
    return v_sol * supply / v_tok


def graduation_progress(real_sol: float, threshold: float = GRADUATION_SOL) -> float:
    """0..1 progress toward completion from real SOL in the curve."""
    if threshold <= 0:
        return 0.0
    return max(0.0, min(1.0, real_sol / threshold))


def estimate_reserves(
    price_sol: float, real_sol: float
) -> Optional[Tuple[float, float, float]]:
    """Anchor virtual reserves from Dex observables (APPROXIMATION).

    Uses the known initial curve (30 SOL / 1.073e15) plus net SOL in as
    the virtual SOL balance, holding ``k`` fixed (ignores fee drift and
    v3 schedule differences). Returns ``(v_tok, v_sol, real_tok_est)``
    or None when inputs are unusable. ``real_tok_est`` assumes the
    classic 793.1M-per-1000M supply shape and is the weakest leg —
    callers needing exact caps must read the curve account.
    """
    if price_sol is None or real_sol is None:
        return None
    try:
        price_sol = float(price_sol)
        real_sol = float(real_sol)
    except (TypeError, ValueError):
        return None
    if price_sol <= 0 or real_sol < 0:
        return None
    v_sol = INITIAL_VIRTUAL_SOL + real_sol
    k = INITIAL_VIRTUAL_TOKEN * INITIAL_VIRTUAL_SOL
    v_tok = k / v_sol if v_sol > 0 else 0.0
    # Real tokens remaining, anchored so price matches at estimate time.
    real_tok = v_tok - real_sol / price_sol if price_sol > 0 else 0.0
    real_tok = max(0.0, min(real_tok, INITIAL_REAL_TOKEN))
    return v_tok, v_sol, real_tok


def guard_buy(
    *,
    size_usd: float,
    price_usd: float,
    liquidity_usd: Optional[float],
    sol_usd: float,
    max_impact_pct: float = 10.0,
    max_liq_frac: float = 0.05,
) -> Tuple[bool, str, Optional[float]]:
    """Pre-trade curve check for a buy of ``size_usd``.

    Two independent refusals: our size moves the curve more than
    ``max_impact_pct`` (we would be the exit liquidity), or our size
    exceeds ``max_liq_frac`` of real pool liquidity (there is nothing
    to sell back into). Returns ``(ok, reason, impact_pct)``.
    Missing/unusable inputs skip (other screens still apply).
    Never raises.
    """
    try:
        if size_usd <= 0 or price_usd <= 0 or sol_usd <= 0:
            return True, "curve_skip_no_basis", None
        if liquidity_usd is None or float(liquidity_usd) <= 0:
            return True, "curve_skip_no_liquidity", None
        liq_frac = float(size_usd) / float(liquidity_usd)
        if liq_frac > max_liq_frac:
            return False, f"curve_thin size {liq_frac:.0%} of pool liq", None
        # Dex total reserve value ≈ both sides; real SOL ≈ half.
        real_sol = float(liquidity_usd) / (2.0 * float(sol_usd))
        price_sol = float(price_usd) / float(sol_usd)
        est = estimate_reserves(price_sol, real_sol)
        if est is None:
            return True, "curve_skip_unestimable", None
        v_tok, v_sol, real_tok = est
        sol_in = float(size_usd) / float(sol_usd)
        impact = buy_impact_pct(sol_in, v_tok, v_sol, real_tok)
        if impact is None:
            return True, "curve_skip_no_quote", None
        if impact > max_impact_pct:
            return False, f"curve_impact {impact:.1f}%>{max_impact_pct:g}%", round(impact, 2)
        return True, "curve_ok", round(impact, 2)
    except Exception:  # noqa: BLE001 — guard never blocks on its own errors
        return True, "curve_skip_error", None
