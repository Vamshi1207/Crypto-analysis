"""TradingAgents-shaped desk for Pump creates — no LangGraph on the hot path.

TradingAgents (TauricResearch) is a daily LLM firm: technical / bull / bear /
risk / trader / portfolio manager, plus a memory of what closed. A Pump mint
is seconds old, so those roles read the live curve tape and a rolling peer
window instead of 10-Ks, MACD, and Yahoo bars.

Hard vetoes stay in ``pumpfun.snipe_veto``. This module ranks *this* mint
against other creates on the same tape and sizes from that score.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from decision.engines.sniper.pumpfun import snipe_lift_pct, snipe_size, snipe_veto

PEER_WINDOW = 80
WARMUP_N = 16
# Risk floor: 1-buyer blowoffs and empty curves never clear the desk.
MIN_UNIQUE = 3
MIN_REAL_SOL = 2.5
WARMUP_BUYERS = 4
WARMUP_LIFT_PCT = 8.0
WARMUP_MAX_LIFT_PCT = 35.0
WARMUP_SOL = 4.0
# Last session bought 50–104% rips (Soul, LUCK, DATBOI) and ate the creator dump.
# Lift is a *late* signal on Pump; cap it and score a sweet spot instead.
DEFAULT_MAX_LIFT_PCT = 40.0
LOSING_MAX_LIFT_PCT = 30.0
DEFAULT_PERCENTILE = 0.70
STARVE_LOOSEN = 0.55

_lock = threading.Lock()
_peers: deque[dict[str, float]] = deque(maxlen=PEER_WINDOW)
_last_buy_at: float = 0.0
_observes_since_buy: int = 0


@dataclass
class DeskReview:
    ok: bool
    why: str
    size_mult: float = 1.0
    composite: float = 0.0
    threshold: float = DEFAULT_PERCENTILE
    peers: int = 0
    opinions: list[str] = field(default_factory=list)


def clear() -> None:
    global _last_buy_at, _observes_since_buy
    with _lock:
        _peers.clear()
        _last_buy_at = 0.0
        _observes_since_buy = 0


def observe(
    *,
    unique_buyers: int,
    lift_pct: float,
    real_sol: float,
    age_sec: float,
) -> None:
    """Remember a create snapshot so the next mint is ranked against it."""
    global _observes_since_buy
    vel = float(unique_buyers) / max(float(age_sec), 1.0)
    with _lock:
        _peers.append(
            {
                "buyers": float(unique_buyers),
                "lift": float(lift_pct),
                "sol": float(real_sol),
                "vel": vel,
                "ts": time.time(),
            }
        )
        _observes_since_buy += 1


def note_fill() -> None:
    global _last_buy_at, _observes_since_buy
    with _lock:
        _last_buy_at = time.time()
        _observes_since_buy = 0


def status() -> dict[str, Any]:
    with _lock:
        n = len(_peers)
        idle = _observes_since_buy
        last = _last_buy_at
    return {
        "peers": n,
        "warmup": n < WARMUP_N,
        "observes_since_buy": idle,
        "threshold": round(_threshold(DEFAULT_PERCENTILE, idle), 4),
        "last_buy_at": last or None,
        "recent_ev_usd": _recent_ev(),
    }


def review(
    *,
    create_px: float,
    last_px: float,
    unique_buyers: int,
    buys: int,
    sells: int,
    real_sol: float,
    age_sec: float,
    watch_sec: float,
    curve_drop_pct: float,
    max_lift_pct: float = DEFAULT_MAX_LIFT_PCT,
    min_percentile: float = DEFAULT_PERCENTILE,
    dev_sold: bool = False,
    peak_real_sol: float = 0.0,
    smart_money: bool = False,
) -> DeskReview:
    """Risk veto, then demand vs peers. High lift is a dump setup, not an edge."""
    ev = _recent_ev()
    cap = max_lift_pct if max_lift_pct > 0 else DEFAULT_MAX_LIFT_PCT
    if ev is not None and ev < 0:
        cap = min(cap, LOSING_MAX_LIFT_PCT)
    min_uniq = MIN_UNIQUE
    if ev is not None and ev < 0:
        min_uniq = max(min_uniq, 4)

    veto = snipe_veto(
        create_px=create_px,
        last_px=last_px,
        buys=buys,
        sells=sells,
        real_sol=real_sol,
        age_sec=age_sec,
        watch_sec=watch_sec,
        max_lift_pct=cap,
        dev_sold=dev_sold,
        peak_real_sol=peak_real_sol,
        curve_drop_pct=curve_drop_pct,
    )
    if veto:
        return DeskReview(ok=False, why=veto, opinions=[f"risk:{veto}"])

    lift = snipe_lift_pct(create_px, last_px)
    vel = float(unique_buyers) / max(float(age_sec), 1.0)
    opinions = [
        _technical(unique_buyers, lift, real_sol, vel),
        _bull(unique_buyers, lift, real_sol, buys, sells, smart_money),
        _bear(unique_buyers, lift, real_sol, buys, sells, age_sec),
    ]

    if unique_buyers < min_uniq or real_sol < MIN_REAL_SOL:
        return DeskReview(
            ok=False,
            why="thin_tape",
            opinions=[o for o in opinions if o],
        )

    with _lock:
        peers = list(_peers)
        idle = _observes_since_buy
    n = len(peers)
    threshold = _threshold(min_percentile, idle)

    if n < WARMUP_N:
        ok = (
            unique_buyers >= WARMUP_BUYERS
            and WARMUP_LIFT_PCT <= lift <= WARMUP_MAX_LIFT_PCT
            and real_sol >= WARMUP_SOL
        )
        why = "desk_warmup" if ok else "waiting_peers"
        # Smaller clip once price has already run.
        mult = 1.0 if unique_buyers >= 5 and lift <= 20 else 0.7
        return DeskReview(
            ok=ok,
            why=why,
            size_mult=mult,
            composite=0.0,
            threshold=threshold,
            peers=n,
            opinions=[o for o in opinions if o],
        )

    p_buyers = _percentile(float(unique_buyers), [p["buyers"] for p in peers])
    p_sol = _percentile(float(real_sol), [p["sol"] for p in peers])
    p_vel = _percentile(vel, [p["vel"] for p in peers])
    sweet = _sweet_lift(lift)
    # Demand vs peers. Lift is a sweet-spot term, not "higher is better".
    composite = 0.35 * p_buyers + 0.30 * p_sol + 0.20 * p_vel + 0.15 * sweet
    ok = composite >= threshold and sweet > 0
    why = f"desk_p{int(composite * 100)}" if ok else "below_peers"
    if composite >= 0.85 and lift <= 20:
        mult = 1.0
    elif composite >= 0.70:
        mult = 0.75
    else:
        mult = 0.6
    if smart_money and lift <= 20:
        mult = min(1.0, mult + 0.1)
    return DeskReview(
        ok=ok,
        why=why,
        size_mult=mult,
        composite=round(composite, 4),
        threshold=round(threshold, 4),
        peers=n,
        opinions=[o for o in opinions if o],
    )


def size_for(base: float, review: DeskReview, *, lift_pct: float, unique_buyers: int, real_sol: float) -> float:
    raw = snipe_size(base, lift_pct=lift_pct, unique_buyers=unique_buyers, real_sol=real_sol)
    return round(max(0.0, raw * review.size_mult), 2)


def _threshold(lane_bar: float, idle: int) -> float:
    bar = lane_bar if lane_bar > 0 else DEFAULT_PERCENTILE
    # Do not raise the composite bar when losing — that only selected
    # even more extreme rips. Losing tightens max lift instead.
    if idle >= 40:
        return min(bar, STARVE_LOOSEN)
    return bar


def _percentile(value: float, others: list[float]) -> float:
    if not others:
        return 1.0
    below = sum(1 for x in others if x < value)
    return below / len(others)


def _sweet_lift(lift: float) -> float:
    """1.0 around +12–20%, 0 below +5% or at the 40% dump zone."""
    if lift < 5.0 or lift >= 40.0:
        return 0.0
    if lift <= 20.0:
        return min(1.0, (lift - 5.0) / 15.0)
    return max(0.0, (40.0 - lift) / 20.0)


def _technical(buyers: int, lift: float, sol: float, vel: float) -> str:
    return f"technical: {buyers} buyers, lift {lift:.1f}%, {sol:.1f} SOL, {vel:.2f}/s"


def _bull(buyers: int, lift: float, sol: float, buys: int, sells: int, smart: bool) -> str:
    if buyers >= 4 and 8.0 <= lift <= 25.0 and sol >= 6:
        extra = " smart_money" if smart else ""
        return f"bull: early crowded curve{extra}"
    if buys > sells * 2 and sol >= 4 and lift <= 25:
        return "bull: net inflow still early"
    return "bull: mixed"


def _bear(buyers: int, lift: float, sol: float, buys: int, sells: int, age: float) -> str:
    if lift >= 35:
        return "bear: already extended"
    if buyers < 4 and age > 12:
        return "bear: thin late tape"
    if sells >= buys and buys > 0:
        return "bear: sell pressure"
    return "bear: contained"


def _recent_ev() -> Optional[float]:
    try:
        from decision import labs, paper

        with paper.use_lane(labs.primary_lane_id()):
            closed = paper.snapshot().get("closed_recent") or []
    except Exception:
        return None
    pnls = [
        float(p.get("realized_pnl_usd") or 0.0)
        for p in closed
        if isinstance(p, dict) and p.get("entry_reason") == "snipe"
    ]
    if len(pnls) < 4:
        return None
    return round(sum(pnls) / len(pnls), 4)
