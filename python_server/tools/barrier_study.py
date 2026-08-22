"""Measure the paper scalp's barrier geometry against the historical corpus.

Replays the exact fill / exit model from `decision.paper` over archived OHLCV,
conditioned on the trailing window return the momentum specialist sees. Answers
two questions the live log is too small to settle:

  1. What does a $TARGET / $STOP pair actually imply as a *price* move, once
     entry slippage and round-trip fees are paid?
  2. Does forward performance depend on trailing momentum — i.e. is fading a
     blowoff empirically justified, or just intuition?

Run inside the container:

    python -m tools.barrier_study --timeframe 5S --tokens 12
"""

from __future__ import annotations

import argparse
import statistics
from dataclasses import dataclass
from typing import Optional

from decision import costs, dataset

ENTRY_SLIP_PCT = costs.DEFAULT_ENTRY_SLIP_PCT
EXIT_COST_FRAC = costs.exit_cost_pct() / 100.0

MAX_HOLD_SEC = 600.0
TF_SECONDS = {"5S": 5, "15S": 15, "30S": 30, "1": 60, "3": 180, "5": 300}

# Buckets of trailing window return (%), matching the momentum specialist.
BUCKETS = [
    ("crash   < -20", -1e18, -20.0),
    ("down -20..-3", -20.0, -3.0),
    ("flat  -3..+3", -3.0, 3.0),
    ("trend  +3..+20", 3.0, 20.0),
    ("extend +20..+50", 20.0, 50.0),
    ("blowoff +50..200", 50.0, 200.0),
    ("parabolic >200", 200.0, 1e18),
]


@dataclass
class Trade:
    trailing_pct: float
    pnl_usd: float
    outcome: str  # target | stop | timeout
    bars_held: int


def _pnl(mark: float, fill: float, size: float) -> float:
    """Exactly decision.paper: qty*mark - size - exit_fee - exit_slip."""
    return size * (mark / fill) - size - size * EXIT_COST_FRAC


def _run_fixed(
    rows: list[dict[str, float]], i: int, fill: float, *,
    size: float, target: float, stop: float, max_bars: int,
) -> tuple[str, float, int]:
    """Current behaviour: exit at the first mark that has crossed the target.

    Banks `target` exactly. Real fills overshoot when a bar gaps past the level,
    but that is tick granularity, not something the strategy is reaching for.
    """
    for k in range(1, max_bars + 1):
        bar = rows[i + k]
        # Conservative: if a bar spans both barriers, assume the stop hit.
        if _pnl(bar["low"], fill, size) <= -stop:
            return "stop", -stop, k
        if _pnl(bar["high"], fill, size) >= target:
            return "target", target, k
    return "timeout", _pnl(rows[i + max_bars]["close"], fill, size), max_bars


def _run_ratchet(
    rows: list[dict[str, float]], i: int, fill: float, *,
    size: float, target: float, stop: float, max_bars: int, trail_frac: float,
) -> tuple[str, float, int]:
    """Lock in `target`, then let the winner run behind a trailing giveback.

    Once peak profit reaches the target the floor never drops below it, so a
    winner still banks at least `target`. Above that, the exit trails the peak
    by `trail_frac`, letting a fat right tail keep running.

    Conservative within a bar: the low is checked against the floor before the
    high updates the peak, so an up-spike that retraces in the same bar is not
    credited.
    """
    peak = 0.0
    locked = False
    for k in range(1, max_bars + 1):
        bar = rows[i + k]
        low_pnl = _pnl(bar["low"], fill, size)
        floor = max(target, peak * (1.0 - trail_frac)) if locked else -stop
        if low_pnl <= floor:
            return ("trail" if locked else "stop"), floor, k
        high_pnl = _pnl(bar["high"], fill, size)
        if high_pnl > peak:
            peak = high_pnl
        if not locked and peak >= target:
            locked = True
    return "timeout", _pnl(rows[i + max_bars]["close"], fill, size), max_bars


def simulate(
    rows: list[dict[str, float]],
    *,
    size: float,
    target: float,
    stop: float,
    max_bars: int,
    window: int,
    step: int,
    exit_mode: str = "fixed",
    trail_frac: float = 0.35,
) -> list[Trade]:
    trades: list[Trade] = []
    i = window
    n = len(rows)
    while i + max_bars < n:
        price = rows[i]["close"]
        past = rows[i - window + 1]["close"]
        if price <= 0 or past <= 0:
            i += step
            continue
        trailing = (price / past - 1.0) * 100.0
        fill = price * (1.0 + ENTRY_SLIP_PCT / 100.0)

        if exit_mode == "ratchet":
            outcome, pnl, held = _run_ratchet(
                rows, i, fill, size=size, target=target, stop=stop,
                max_bars=max_bars, trail_frac=trail_frac,
            )
        else:
            outcome, pnl, held = _run_fixed(
                rows, i, fill, size=size, target=target, stop=stop, max_bars=max_bars
            )

        trades.append(Trade(trailing, pnl, outcome, held))
        i += step
    return trades


def _fmt_bucket(label: str, trades: list[Trade]) -> str:
    if not trades:
        return f"{label:>16}  {'—':>6}"
    n = len(trades)
    wins = sum(1 for t in trades if t.outcome in ("target", "trail"))
    stops = sum(1 for t in trades if t.outcome == "stop")
    pnls = [t.pnl_usd for t in trades]
    total = sum(pnls)
    return (
        f"{label:>16}  {n:>6}  {wins / n:>6.1%}  {stops / n:>6.1%}  "
        f"{statistics.mean(pnls):>+8.3f}  {statistics.median(pnls):>+8.3f}  {total:>+10.2f}"
    )


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--timeframe", default="5S")
    ap.add_argument("--tokens", type=int, default=12)
    ap.add_argument("--size", type=float, default=36.12)
    ap.add_argument("--target", type=float, default=1.0)
    ap.add_argument("--stop", type=float, default=1.5)
    ap.add_argument("--window", type=int, default=12, help="trailing bars for momentum")
    ap.add_argument("--step", type=int, default=25, help="bars between entries")
    ap.add_argument("--tail", type=int, default=30_000, help="bars loaded per token")
    ap.add_argument(
        "--exit-mode",
        default="fixed",
        choices=["fixed", "ratchet", "both"],
        help="fixed = bank the target; ratchet = lock the target then trail the peak",
    )
    ap.add_argument(
        "--trail-frac",
        type=float,
        default=0.35,
        help="ratchet only: fraction of peak profit given back before exiting",
    )
    args = ap.parse_args(argv)

    secs = TF_SECONDS.get(args.timeframe, 60)
    max_bars = max(1, int(MAX_HOLD_SEC // secs))

    geo = costs.barrier_moves(
        size_usd=args.size, target_usd=args.target, stop_usd=args.stop
    )
    print(f"timeframe={args.timeframe}  size=${args.size:.2f}  "
          f"target=${args.target:.2f}  stop=${args.stop:.2f}  max_hold={max_bars} bars")
    print()
    print("--- barrier geometry ---")
    print(f"  to bank ${args.target:.2f}: price must rise {geo['target_from_quote_pct']:+.2f}% "
          f"from the quote ({geo['target_from_fill_pct']:+.2f}% from the fill)")
    print(f"  to lose ${args.stop:.2f}: price need only fall {geo['stop_from_quote_pct']:+.2f}% "
          f"from the quote ({geo['stop_from_fill_pct']:+.2f}% from the fill)")
    ratio = abs(geo["target_from_fill_pct"] / geo["stop_from_fill_pct"])
    print(f"  distance ratio (target:stop) = {ratio:.2f}:1 against you, "
          f"payoff = 1:{args.stop / args.target:.2f} against you")
    degenerate = costs.degenerate_stop_reason(
        size_usd=args.size, target_usd=args.target, stop_usd=args.stop
    )
    if degenerate:
        print(f"  DEGENERATE: {degenerate}")
    rt_cost = costs.round_trip_cost_pct()
    print(f"  round-trip cost = {rt_cost:.2f}% of size = "
          f"${args.size * rt_cost / 100:.2f} per completed trade")
    print()

    tokens = sorted(dataset.list_tokens(), key=lambda t: t.size_bytes)[: args.tokens]
    modes = ["fixed", "ratchet"] if args.exit_mode == "both" else [args.exit_mode]
    results: dict[str, list[Trade]] = {m: [] for m in modes}
    used = 0
    for token in tokens:
        try:
            _, candles = dataset.load_timeframe(
                token.candles_path, args.timeframe, tail=args.tail
            )
        except Exception:
            continue
        rows = dataset.ohlcv_rows(candles)
        if len(rows) < args.window + max_bars + args.step:
            continue
        for mode in modes:
            results[mode].extend(
                simulate(
                    rows,
                    size=args.size,
                    target=args.target,
                    stop=args.stop,
                    max_bars=max_bars,
                    window=args.window,
                    step=args.step,
                    exit_mode=mode,
                    trail_frac=args.trail_frac,
                )
            )
        used += 1

    if not any(results.values()):
        print("no trades simulated — corpus empty or timeframe missing")
        return 1

    for mode in modes:
        all_trades = results[mode]
        detail = f" (trail {args.trail_frac:.0%} of peak)" if mode == "ratchet" else ""
        print(f"--- exit_mode={mode}{detail}: {len(all_trades):,} entries "
              f"across {used} tokens ---")
        print(f"{'trailing window':>16}  {'n':>6}  {'win%':>6}  {'stop%':>6}  "
              f"{'mean$':>8}  {'med$':>8}  {'total$':>10}")
        for label, lo, hi in BUCKETS:
            bucket = [t for t in all_trades if lo <= t.trailing_pct < hi]
            print(_fmt_bucket(label, bucket))
        print("-" * 72)
        print(_fmt_bucket("ALL", all_trades))
        wins = [t.pnl_usd for t in all_trades if t.outcome in ("target", "trail")]
        mean_all = statistics.mean(t.pnl_usd for t in all_trades)
        print(f"  mean pnl ${mean_all:+.3f} per entry", end="")
        if wins:
            print(f"   |   winners n={len(wins)} "
                  f"mean ${statistics.mean(wins):+.2f} best ${max(wins):+.2f}")
        else:
            print()
        print()

    print(f"expected round-trip cost: ${-args.size * rt_cost / 100:+.3f}")
    print("(a driftless barrier walk returns minus the round-trip cost; a mean")
    print(" materially above that is the only evidence of real forecast edge)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
