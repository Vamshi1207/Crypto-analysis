"""Single source of truth for round-trip execution cost.

Gate 2 sizes expected edge against these numbers and `decision.paper` charges
them on fills. They must not drift apart. When the gate is cheaper than the
executor, every approved buy is measured against a cost the fill will not
honour: it clears the gate, then cannot reach its profit target and drifts into
the stop instead.

Empirically (tools/barrier_study.py, ~10k entries on the archived corpus) a
barrier scalp returns roughly *minus the round-trip cost* per trade regardless
of entry signal or barrier placement — the standard result for a driftless
random walk with two absorbing barriers. So this number is not a detail: it is
the entire hurdle a forecast has to clear to be worth trading.
"""

from __future__ import annotations

from typing import Optional

from decision.config import _env_float

# Swap fees, charged on both legs.
ENTRY_FEE_PCT = _env_float("EXEC_ENTRY_FEE_PCT", 0.3)
EXIT_FEE_PCT = _env_float("EXEC_EXIT_FEE_PCT", 0.3)
# Slippage. Entry is measured from Jupiter when available; exit is assumed.
DEFAULT_ENTRY_SLIP_PCT = _env_float("EXEC_ENTRY_SLIP_PCT", 1.5)
EXIT_SLIP_PCT = _env_float("EXEC_EXIT_SLIP_PCT", 1.0)
# Solana priority fee, paid once per leg pair.
PRIORITY_TIP_PCT = _env_float("EXEC_PRIORITY_TIP_PCT", 0.1)


def entry_slip_pct(
    *,
    price_impact_pct: Optional[float] = None,
    slippage_bps: Optional[float] = None,
) -> float:
    """Best available estimate of entry slippage, in percent.

    Prefers a measured Jupiter price impact, falls back to a quoted slippage
    budget, then to the memecoin default.
    """
    if price_impact_pct is not None:
        return float(price_impact_pct)
    if slippage_bps is not None:
        return float(slippage_bps) / 100.0
    return DEFAULT_ENTRY_SLIP_PCT


def exit_cost_pct() -> float:
    """Cost charged when closing a position, in percent of notional."""
    return EXIT_FEE_PCT + EXIT_SLIP_PCT


def round_trip_cost_pct(
    *,
    price_impact_pct: Optional[float] = None,
    slippage_bps: Optional[float] = None,
) -> float:
    """All-in cost of opening and closing one position, in percent of notional."""
    entry = entry_slip_pct(price_impact_pct=price_impact_pct, slippage_bps=slippage_bps)
    return round(entry + ENTRY_FEE_PCT + exit_cost_pct() + PRIORITY_TIP_PCT, 4)


def barrier_moves(
    *,
    size_usd: float,
    target_usd: float,
    stop_usd: float,
    entry_slip_pct_: Optional[float] = None,
) -> dict[str, float]:
    """Price moves the dollar barriers actually require, in percent.

    `decision.paper` marks a position as
    ``pnl = size * (mark / fill - 1 - exit_cost)``, so a dollar target is really
    a price move of ``exit_cost + target/size`` above the *fill* — and the fill
    already sits entry-slippage above the quote.
    """
    if size_usd <= 0:
        raise ValueError("size_usd must be positive")
    exit_frac = exit_cost_pct() / 100.0
    slip = DEFAULT_ENTRY_SLIP_PCT if entry_slip_pct_ is None else entry_slip_pct_
    entry_mult = 1.0 + slip / 100.0

    target_from_fill = exit_frac + target_usd / size_usd
    stop_from_fill = exit_frac - stop_usd / size_usd
    return {
        "target_from_fill_pct": target_from_fill * 100.0,
        "stop_from_fill_pct": stop_from_fill * 100.0,
        "target_from_quote_pct": (entry_mult * (1.0 + target_from_fill) - 1.0) * 100.0,
        "stop_from_quote_pct": (entry_mult * (1.0 + stop_from_fill) - 1.0) * 100.0,
    }


def required_move_pct(
    *,
    size_usd: float,
    target_usd: float,
    price_impact_pct: Optional[float] = None,
) -> float:
    """Percent move needed to bank ``target_usd`` on ``size_usd``, all-in.

    This is the number that decides whether a strategy is possible at all:
    ``target/size`` plus round-trip cost. A $1 target on $40 at 3.2% cost needs
    5.7%, which almost nothing delivers in minutes; the same $1 on $200 at 1.7%
    needs 2.2%, which memecoins print constantly. Sizing is not a detail here.
    """
    if size_usd <= 0:
        raise ValueError("size_usd must be positive")
    cost = round_trip_cost_pct(price_impact_pct=price_impact_pct)
    return round(cost + (target_usd / size_usd) * 100.0, 4)


def degenerate_stop_reason(
    *,
    size_usd: float,
    target_usd: float,
    stop_usd: float,
    entry_slip_pct_: Optional[float] = None,
) -> Optional[str]:
    """Explain why these barriers are unopenable, or None if they are sane.

    A stop smaller than the exit cost on this size sits *above* the fill, so the
    position books a loss the moment it opens no matter which way price goes.
    """
    moves = barrier_moves(
        size_usd=size_usd,
        target_usd=target_usd,
        stop_usd=stop_usd,
        entry_slip_pct_=entry_slip_pct_,
    )
    if moves["stop_from_fill_pct"] >= 0.0:
        return (
            f"stop ${stop_usd:.2f} on ${size_usd:.2f} sits {moves['stop_from_fill_pct']:+.2f}% "
            f"above the fill — inside the {exit_cost_pct():.2f}% exit cost, so the position "
            "stops out immediately"
        )
    if moves["target_from_fill_pct"] <= 0.0:
        return f"target ${target_usd:.2f} on ${size_usd:.2f} is already met at the fill"
    return None
