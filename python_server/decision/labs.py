"""Parallel paper experiments. Same tape, isolated books, different rules.

Not a search for one perfect setup. Each lane keeps its own $1000 so a
spray-and-scratch sniper cannot hide what the 40% rip book is doing.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any, Optional

from decision import paper
from decision.pumpfun import snipe_lift_pct
from decision import snipe_desk

ENABLED = os.getenv("LABS_ENABLED", "1").strip() == "1"


@dataclass(frozen=True)
class SnipeLane:
    id: str
    label: str
    thesis: str
    min_buyers: int
    min_lift_pct: float
    fast_lift_pct: float
    min_real_sol: float
    min_liq_usd: float
    size_usd: float
    stop_usd: float
    target_usd: float
    take_profit_pct: float
    max_hold_sec: float
    dead_after_sec: float
    abs_hold_sec: float
    bank_at_target: bool = True
    desk_percentile: float = 0.70
    max_lift_pct: float = 40.0


# Same Pump tape, two books. Logs: sniper scratches are -EV once ghost
# rips are stripped. Cluster-style hold (300s, trail, no dead_tape) was
# +$2.30/trade across 266 fills. That is the money book.
SNIPE_LANES: tuple[SnipeLane, ...] = (
    SnipeLane(
        id="hold",
        label="Hold Book",
        thesis="Early crowded Pump tape, hold 5m, trail — replica of the +EV cluster book",
        min_buyers=3,
        min_lift_pct=8.0,
        fast_lift_pct=12.0,
        min_real_sol=4.0,
        min_liq_usd=600.0,
        size_usd=40.0,
        stop_usd=1.60,
        target_usd=2.0,
        take_profit_pct=0.0,
        max_hold_sec=300.0,
        dead_after_sec=0.0,
        abs_hold_sec=300.0,
        bank_at_target=False,
        desk_percentile=0.65,
        max_lift_pct=40.0,
    ),
    SnipeLane(
        id="snipe",
        label="Scratch Book",
        thesis="Same tape, faster exit — comparison only",
        min_buyers=2,
        min_lift_pct=5.0,
        fast_lift_pct=12.0,
        min_real_sol=2.5,
        min_liq_usd=400.0,
        size_usd=20.0,
        stop_usd=0.80,
        target_usd=2.0,
        take_profit_pct=40.0,
        max_hold_sec=60.0,
        dead_after_sec=8.0,
        abs_hold_sec=120.0,
    ),
)

ARB_LANE = "arb"
SCALP_LANE = "scalp"
CLUSTER_LANE = "cluster"
CHANNEL_LANES = (
    {"id": CLUSTER_LANE, "family": "cluster", "label": "Smart-money cluster", "thesis": "Gecko new pools with ≥3 unique buyers, 5m hold"},
    {"id": ARB_LANE, "family": "arb", "label": "Jupiter round-trip", "thesis": "SOL→mint→SOL quote, isolated cash"},
    {"id": SCALP_LANE, "family": "scalp", "label": "Forecast scalp", "thesis": "discover + swarm decide, isolated cash"},
)

_seen: dict[str, set[str]] = {}


def all_lane_ids() -> list[str]:
    ids = [lane.id for lane in SNIPE_LANES]
    ids.extend(row["id"] for row in CHANNEL_LANES)
    ids.append(paper.DEFAULT_LANE)
    return ids


def lane_meta() -> list[dict[str, Any]]:
    rows = [
        {
            "id": lane.id,
            "family": "snipe",
            "label": lane.label,
            "thesis": lane.thesis,
            "params": {k: v for k, v in asdict(lane).items() if k not in {"id", "label", "thesis"}},
        }
        for lane in SNIPE_LANES
    ]
    rows.extend(CHANNEL_LANES)
    rows.append(
        {
            "id": paper.DEFAULT_LANE,
            "family": "main",
            "label": "Legacy main",
            "thesis": "Unchanged single book (tests / leftover)",
        }
    )
    return rows


def snipe_lanes() -> tuple[SnipeLane, ...]:
    if ENABLED:
        return SNIPE_LANES
    return ()


def primary_lane_id() -> str:
    """Dashboard /portfolio book: the live leader, else the hold book.

    Open lots beat closed PnL so a busy cluster book is not hidden behind a
    scratch sniper that already booked a couple of dollars.
    """
    if not ENABLED:
        return paper.DEFAULT_LANE
    preferred = SNIPE_LANES[0].id if SNIPE_LANES else paper.DEFAULT_LANE
    board = paper.lane_board()
    active = [
        row
        for row in (board.get("lanes") or [])
        if row.get("id") != paper.DEFAULT_LANE
        and ((row.get("closed_count") or 0) or (row.get("open_count") or 0))
    ]
    if not active:
        return preferred
    active.sort(
        key=lambda row: (
            int(row.get("open_count") or 0),
            int(row.get("closed_count") or 0),
            float(row.get("realized_pnl_usd") or 0.0),
        ),
        reverse=True,
    )
    return str(active[0]["id"])


def already_seen(lane_id: str, mint: str) -> bool:
    return mint in _seen.get(lane_id, set())


def note_seen(lane_id: str, mint: str) -> None:
    _seen.setdefault(lane_id, set()).add(mint)


def all_snipe_lanes_seen(mint: str) -> bool:
    lanes = snipe_lanes()
    if not lanes:
        return False
    return all(mint in _seen.get(lane.id, set()) for lane in lanes)


def clear_seen() -> None:
    _seen.clear()


def trading_lane(family: str) -> str:
    """Book id for arb/scalp fills. Isolated when labs is on, else main."""
    if not ENABLED:
        return paper.DEFAULT_LANE
    if family in {"arb"}:
        return ARB_LANE
    if family in {"scalp"}:
        return SCALP_LANE
    if family in {"cluster", "launch"}:
        return CLUSTER_LANE
    return paper.DEFAULT_LANE


def consider_snipe(
    *,
    lane: SnipeLane,
    watch: dict[str, Any],
    tape: dict[str, Any],
    age_sec: float,
    sol_usd: float,
    watch_sec: float,
    curve_drop_pct: float,
    max_per_tick_already: int,
    max_per_tick: int,
    open_fn,
) -> Optional[dict[str, Any]]:
    """One lane looks at one create. Returns a hit row when it paper-buys."""
    mint = str(watch.get("mint") or "")
    if not mint or already_seen(lane.id, mint):
        return None
    if max_per_tick_already >= max_per_tick:
        return None
    create_px = float(watch.get("create_px") or 0.0)
    last_px = float(tape.get("last_px") or 0.0) or create_px
    unique = int(tape.get("unique_buyers") or 0)
    buys = int(tape.get("buys") or 0)
    sells = int(tape.get("sells") or 0)
    real_sol = float(tape.get("real_sol") or 0.0)
    peak_real = float(tape.get("peak_real_sol") or 0.0)
    lift = snipe_lift_pct(create_px, last_px)
    desk = snipe_desk.review(
        create_px=create_px,
        last_px=last_px,
        unique_buyers=unique,
        buys=buys,
        sells=sells,
        real_sol=real_sol,
        age_sec=age_sec,
        watch_sec=watch_sec,
        curve_drop_pct=curve_drop_pct,
        min_percentile=lane.desk_percentile,
        max_lift_pct=lane.max_lift_pct,
        dev_sold=bool(tape.get("dev_sold")),
        peak_real_sol=peak_real,
        smart_money=bool(tape.get("smart_money")),
    )
    if not desk.ok:
        return None
    liq_usd = real_sol * sol_usd
    if liq_usd < lane.min_liq_usd:
        return None
    size = snipe_desk.size_for(
        lane.size_usd,
        desk,
        lift_pct=lift,
        unique_buyers=unique,
        real_sol=real_sol,
    )
    row = {
        "mint": mint,
        "pool": watch.get("pool") or mint,
        "symbol": watch.get("symbol") or mint[:6],
        "liquidity_usd": liq_usd,
        "price_usd": last_px,
        "age_min": round(age_sec / 60.0, 2),
        "age_sec": round(age_sec, 1),
        "seen_at": watch.get("seen_at"),
        "source": "pump_create",
        "creator": watch.get("creator"),
    }
    with paper.use_lane(lane.id):
        result = open_fn(
            row,
            strategy="snipe",
            size=size,
            hold=lane.max_hold_sec,
            confirm_dex=False,
            bank_at_target=lane.bank_at_target,
            target_usd=lane.target_usd,
            take_profit_pct=lane.take_profit_pct,
            dead_after_sec=lane.dead_after_sec,
            abs_hold_sec=lane.abs_hold_sec,
            stop_loss_usd=lane.stop_usd,
        )
    if result.get("status") != "opened":
        return None
    note_seen(lane.id, mint)
    snipe_desk.note_fill()
    return {
        **row,
        "strategy": "snipe",
        "lane": lane.id,
        "buyers": unique,
        "lift_pct": round(lift, 2),
        "entry_why": desk.why,
        "size_usd": size,
        "desk": {
            "composite": desk.composite,
            "threshold": desk.threshold,
            "peers": desk.peers,
            "opinions": desk.opinions,
        },
    }


def status() -> dict[str, Any]:
    return {
        "enabled": ENABLED,
        "board": paper.lane_board(),
        "snipe_lanes": [asdict(lane) for lane in SNIPE_LANES],
        "seen": {lid: len(mints) for lid, mints in _seen.items()},
        "desk": snipe_desk.status(),
    }
