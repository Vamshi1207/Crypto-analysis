"""Phase 4 — paper AutoHedge: buy/sell simulation with portfolio limits.

Never touches a wallet. Fills are modeled from DecisionCard costs + optional
Jupiter impact. Target: take ~$1 net profit (DECIDE_TARGET_PROFIT_USD) then exit.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from decision import costs
from decision import exec_sim
from decision import pipeline_log
from decision import session as session_scope
from decision import store as decision_store
from decision.config import _env_float, _env_int, paper_risk_on
from decision.packet import SCALP_SIZE_USD, TARGET_PROFIT_USD
from decision.schema import Action, DecisionCard

LIVE_TRADING = os.getenv("LIVE_TRADING", "0") == "1"
MAX_OPEN_POSITIONS = _env_int("PAPER_MAX_OPEN", 5)
MAX_NOTIONAL_USD = _env_float("PAPER_MAX_NOTIONAL_USD", 200.0)
STOP_LOSS_USD = _env_float("PAPER_STOP_LOSS_USD", 1.5)
# When > 0 the stop scales with position size instead of being a flat dollar
# amount. A fixed $1.50 stop on a larger clip is inside normal memecoin noise,
# so every position would stop out before its thesis had room to play out.
STOP_LOSS_PCT = _env_float("PAPER_STOP_LOSS_PCT", 0.0)
MAX_HOLD_SEC = _env_float("PAPER_MAX_HOLD_SEC", 600.0)
# Trailing take-profit. Closing every winner at exactly +$1 caps the runners
# that pay for the losers, which is the whole edge in momentum scalping.
TRAIL_ENABLED = os.getenv("PAPER_TRAIL_ENABLED", "0").strip() == "1"
# Profit that arms the trail (defaults to the normal target).
TRAIL_ARM_USD = _env_float("PAPER_TRAIL_ARM_USD", 0.0)
# Percent of size we let a winner give back from its peak before banking.
TRAIL_GIVEBACK_PCT = _env_float("PAPER_TRAIL_GIVEBACK_PCT", 1.0)
# Also give back at least this fraction of the peak (0.35 = keep 65% of the run).
TRAIL_GIVEBACK_OF_PEAK = _env_float("PAPER_TRAIL_GIVEBACK_OF_PEAK", 0.35)
# Ignore a single mark that jumps more than this % of size vs the last accepted
# PnL — Dex mids on new memecoins print 50%+ ghosts that used to arm the trail
# and then "take profit" at a loss when the next tick is real.
TRAIL_MAX_JUMP_PCT = _env_float("PAPER_TRAIL_MAX_JUMP_PCT", 15.0)
# First seconds after a fill: a mark that is this far from entry is a different
# venue, not a dump. Used to be an instant -$1.60 stop on every launch.
STOP_GRACE_SEC = _env_float("PAPER_STOP_GRACE_SEC", 12.0)
ENTRY_CONFIRM_PCT = _env_float("PAPER_ENTRY_CONFIRM_PCT", 8.0)
KILL_SWITCH = os.getenv("PAPER_KILL_SWITCH", "0") == "1"
# Quiet period after a close before the same token may be re-entered.
# 0 = off — if Gate 2 still sees edge, paper may re-buy immediately.
REENTRY_COOLDOWN_SEC = _env_float("PAPER_REENTRY_COOLDOWN_SEC", 0.0)
# How stop exits are filled when price gaps through the barrier.
#   barrier — exit at the price that realizes exactly -stop_loss_usd (limit-stop ideal)
#   mark    — exit at the jumped mark (live-like; can blow past the dollar stop)
STOP_FILL_MODE = os.getenv("PAPER_STOP_FILL_MODE", "barrier").strip().lower()
# Absolute floor even in mark mode: never book worse than this fraction of size.
MAX_LOSS_PCT = _env_float("PAPER_MAX_LOSS_PCT", 8.0)
# Concentration: 0 = unlimited (analysis / portfolio cash decide). Loss cap still applies.
MAX_TRADES_PER_MINT_DAY = _env_int("PAPER_MAX_TRADES_PER_MINT_DAY", 0)
MAX_NOTIONAL_PER_MINT_DAY = _env_float("PAPER_MAX_NOTIONAL_PER_MINT_DAY", 0.0)
MAX_DAILY_LOSS_PER_MINT = _env_float("PAPER_MAX_DAILY_LOSS_PER_MINT", 5.0)
# If decide still clears edge while a lot is open, open another lot (pyramid).
# Each lot keeps its own take-profit / stop. Still bound by cash + max open/notional.
ALLOW_ADD_ON = os.getenv("PAPER_ALLOW_ADD_ON", "1").strip() == "1"
MAX_OPEN_LOTS_PER_ADDRESS = _env_int("PAPER_MAX_OPEN_LOTS_PER_ADDRESS", 2)
BLOCK_ADD_ON_IF_OPEN_LOSING = os.getenv("PAPER_BLOCK_ADD_ON_IF_OPEN_LOSING", "1").strip() == "1"
BLOCK_REPEAT_MINT_AFTER_STOP = os.getenv("PAPER_BLOCK_REPEAT_MINT_AFTER_STOP", "1").strip() == "1"
# JEV exit monitor on open lots (0 = off; stops/trails own exits alone).
# The chair may force-exit a loser early. Each lot is re-asked at most
# every JEV_EXIT_RECHECK_SEC once held past JEV_EXIT_MIN_HOLD_SEC — plus
# immediately when pnl slides JEV_EXIT_PNL_DROP_USD since the last review,
# so rugs get judged at the next mark instead of the next minute.
JEV_EXIT_MONITOR = os.getenv("JEV_EXIT_MONITOR", "1").strip() == "1"
JEV_EXIT_RECHECK_SEC = _env_float("JEV_EXIT_RECHECK_SEC", 60.0)
JEV_EXIT_MIN_HOLD_SEC = _env_float("JEV_EXIT_MIN_HOLD_SEC", 10.0)
JEV_EXIT_PNL_DROP_USD = _env_float("JEV_EXIT_PNL_DROP_USD", 2.0)
_jev_exit_checked: dict[str, tuple[float, float]] = {}


@dataclass
class PaperFill:
    side: str  # buy | sell
    price: float
    size_usd: float
    fee_usd: float
    slippage_pct: float
    ts: str


@dataclass
class PaperPosition:
    id: str
    address: str
    mint: Optional[str]
    name: Optional[str]
    entry_price: float
    size_usd: float
    qty: float
    opened_at: str
    target_profit_usd: float
    stop_loss_usd: float
    status: str = "open"  # open | closed
    exit_price: Optional[float] = None
    realized_pnl_usd: Optional[float] = None
    closed_at: Optional[str] = None
    close_reason: Optional[str] = None
    fills: list[dict[str, Any]] = field(default_factory=list)
    # Trailing state: best mark PnL seen and whether the trail is live.
    peak_pnl_usd: float = 0.0
    trail_armed: bool = False
    last_mark_pnl_usd: float = 0.0
    entry_reason: Optional[str] = None
    max_hold_sec: Optional[float] = None
    # Sniper: $1 arms the trail on a live tape. Instant clip only if not situational.
    bank_at_target: bool = False
    take_profit_pct: float = 0.0
    # Situation hold: scratch a dead tape early; only clock-stop if stalled.
    dead_after_sec: Optional[float] = None
    abs_hold_sec: Optional[float] = None
    # Forecast snapshot at entry — feeds the live calibration loop.
    predicted_p10: Optional[float] = None
    predicted_p50: Optional[float] = None
    predicted_p90: Optional[float] = None
    timeframe: Optional[str] = None
    horizon_bars: int = 0
    # On-chain latency sim: mid we decided on, and deferred exit state.
    signal_entry_price: Optional[float] = None
    pending_exit_at: Optional[float] = None
    pending_exit_reason: Optional[str] = None
    pending_exit_signal_price: Optional[float] = None


@dataclass
class PortfolioState:
    cash_usd: float = 1_000.0
    realized_pnl_usd: float = 0.0
    arb_fills: int = 0
    arb_realized_pnl_usd: float = 0.0
    arb_notional_usd: float = 0.0
    open: list[PaperPosition] = field(default_factory=list)
    closed: list[PaperPosition] = field(default_factory=list)
    kill_switch: bool = False
    live_trading_blocked: bool = True


_lock = threading.Lock()
DEFAULT_LANE = "main"
_active_lane: ContextVar[str] = ContextVar("paper_lane", default=DEFAULT_LANE)
_books: dict[str, PortfolioState] = {}


def _new_book() -> PortfolioState:
    return PortfolioState(kill_switch=KILL_SWITCH, live_trading_blocked=not LIVE_TRADING)


def _cur() -> PortfolioState:
    lane = _active_lane.get() or DEFAULT_LANE
    book = _books.get(lane)
    if book is None:
        book = _new_book()
        _books[lane] = book
    return book


class _BookProxy:
    """Attribute access goes to the active lane's book (default: main)."""

    def __getattr__(self, name: str) -> Any:
        return getattr(_cur(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(_cur(), name, value)


_state = _BookProxy()


@contextmanager
def use_lane(lane: str) -> Iterator[str]:
    """Run paper fills against an isolated book. Each lane has its own $1000."""
    key = (lane or DEFAULT_LANE).strip() or DEFAULT_LANE
    token = _active_lane.set(key)
    _cur()  # create the book lazily
    try:
        yield key
    finally:
        _active_lane.reset(token)


def active_lane() -> str:
    return _active_lane.get() or DEFAULT_LANE


def known_lanes() -> list[str]:
    """Declared labs books, plus any leftover book that actually traded."""
    try:
        from decision import labs

        ids = set(labs.all_lane_ids())
    except Exception:
        ids = {DEFAULT_LANE}
    with _lock:
        for lid, book in _books.items():
            if lid in ids:
                continue
            if book.open or book.closed or book.arb_fills:
                ids.add(lid)
    return sorted(ids)


def snapshot() -> dict[str, Any]:
    with _lock:
        scalp_realized = _state.realized_pnl_usd - _state.arb_realized_pnl_usd
        sniper_closed = [p for p in _state.closed if p.entry_reason == "snipe"]
        sniper_open = [p for p in _state.open if p.entry_reason == "snipe"]
        sniper_realized = sum(float(p.realized_pnl_usd or 0.0) for p in sniper_closed)
        return {
            "lane": active_lane(),
            "cash_usd": _state.cash_usd,
            "realized_pnl_usd": _state.realized_pnl_usd,
            "arb_fills": _state.arb_fills,
            "arb_realized_pnl_usd": round(_state.arb_realized_pnl_usd, 4),
            "arb_notional_usd": round(_state.arb_notional_usd, 4),
            "scalp_realized_pnl_usd": round(scalp_realized, 4),
            "open_count": len(_state.open),
            "closed_count": len(_state.closed),
            "kill_switch": _state.kill_switch or KILL_SWITCH,
            "live_trading": LIVE_TRADING,
            "open": [asdict(p) for p in _state.open],
            "closed_recent": [asdict(p) for p in _state.closed[-20:]],
            "sniper": {
                "open_count": len(sniper_open),
                "closed_count": len(sniper_closed),
                "realized_pnl_usd": round(sniper_realized, 4),
                "closed_recent": [asdict(p) for p in sniper_closed[-12:]],
            },
            "limits": {
                "max_open": MAX_OPEN_POSITIONS,
                "max_notional_usd": MAX_NOTIONAL_USD,
                "scalp_size_usd": SCALP_SIZE_USD,
                "target_profit_usd": TARGET_PROFIT_USD,
                "stop_loss_usd": STOP_LOSS_USD,
                "reentry_cooldown_sec": REENTRY_COOLDOWN_SEC,
                "stop_fill_mode": STOP_FILL_MODE,
                "max_loss_pct": MAX_LOSS_PCT,
                "max_trades_per_mint_day": MAX_TRADES_PER_MINT_DAY,
                "max_notional_per_mint_day": MAX_NOTIONAL_PER_MINT_DAY,
                "max_daily_loss_per_mint": MAX_DAILY_LOSS_PER_MINT,
                "allow_add_on": ALLOW_ADD_ON,
                "max_open_lots_per_address": MAX_OPEN_LOTS_PER_ADDRESS,
                "block_add_on_if_open_losing": BLOCK_ADD_ON_IF_OPEN_LOSING,
                "block_repeat_mint_after_stop": BLOCK_REPEAT_MINT_AFTER_STOP,
                "stop_loss_pct": STOP_LOSS_PCT,
                "trail_enabled": TRAIL_ENABLED,
                "trail_arm_usd": TRAIL_ARM_USD or TARGET_PROFIT_USD,
                "trail_giveback_pct": TRAIL_GIVEBACK_PCT,
                "trail_giveback_of_peak": TRAIL_GIVEBACK_OF_PEAK,
                "trail_max_jump_pct": TRAIL_MAX_JUMP_PCT,
                "stop_grace_sec": STOP_GRACE_SEC,
                "entry_confirm_pct": ENTRY_CONFIRM_PCT,
                "exec_sim": exec_sim.status(),
                "required_move_pct": round(
                    costs.required_move_pct(
                        size_usd=SCALP_SIZE_USD, target_usd=TARGET_PROFIT_USD
                    ),
                    4,
                ),
            },
            "pending_buys": exec_sim.status().get("pending_buys"),
        }


def lane_board() -> dict[str, Any]:
    """Side-by-side PnL for every isolated paper book."""
    try:
        from decision import labs

        meta = {row["id"]: row for row in labs.lane_meta()}
    except Exception:
        meta = {}
    lanes: list[dict[str, Any]] = []
    for lid in known_lanes():
        with use_lane(lid):
            book = _cur()
            pnls = [float(p.realized_pnl_usd or 0.0) for p in book.closed]
            wins = sum(1 for x in pnls if x > 0)
            losses = sum(1 for x in pnls if x < 0)
            n = len(pnls)
            info = meta.get(lid) or {"id": lid, "family": lid, "label": lid, "thesis": ""}
            lanes.append(
                {
                    **info,
                    "cash_usd": round(book.cash_usd, 4),
                    "realized_pnl_usd": round(book.realized_pnl_usd, 4),
                    "open_count": len(book.open),
                    "closed_count": n,
                    "wins": wins,
                    "losses": losses,
                    "flats": n - wins - losses,
                    "win_rate": round(wins / n, 4) if n else None,
                    "avg_pnl_usd": round(sum(pnls) / n, 4) if n else None,
                    "arb_fills": book.arb_fills,
                    "arb_realized_pnl_usd": round(book.arb_realized_pnl_usd, 4),
                }
            )
    ranked = sorted(lanes, key=lambda r: (r.get("realized_pnl_usd") or 0.0), reverse=True)
    return {
        "lanes": ranked,
        "leader": ranked[0]["id"] if ranked else None,
        "n": len(ranked),
    }


def set_kill_switch(enabled: bool) -> dict[str, Any]:
    flag = bool(enabled)
    with _lock:
        for book in _books.values():
            book.kill_switch = flag
        _cur().kill_switch = flag
    return snapshot()


def reset(*, starting_cash_usd: float = 1_000.0) -> dict[str, Any]:
    """Wipe every lane's open/closed book and restore starting cash."""
    cash = float(starting_cash_usd)
    with _lock:
        ids = set(_books) | {DEFAULT_LANE}
        try:
            from decision import labs

            ids |= set(labs.all_lane_ids())
        except Exception:
            pass
        for lid in ids:
            book = _new_book()
            book.cash_usd = cash
            _books[lid] = book
    exec_sim.reset()
    pipeline_log.emit(
        "paper",
        "reset",
        level="warning",
        cash_usd=starting_cash_usd,
        lanes=sorted(ids),
    )
    return snapshot()


def observe_mark(mint: str, price: float) -> None:
    """Feed a venue mid into the latency buffer (call on every Dex/pricefeed tick)."""
    if exec_sim.ENABLED:
        exec_sim.observe(mint, price)


def drain_pending_buys(
    *, fallback_price_fn: Optional[Any] = None
) -> list[dict[str, Any]]:
    """Materialize buys whose on-chain latency window has elapsed."""
    if not exec_sim.ENABLED:
        return []
    filled: list[dict[str, Any]] = []
    for pending in exec_sim.due_buys():
        fb = None
        if fallback_price_fn is not None:
            try:
                fb = fallback_price_fn(pending.mint, pending.address)
            except Exception:  # noqa: BLE001
                fb = None
        if fb and fb > 0:
            exec_sim.observe(pending.mint, float(fb))
        resolved = exec_sim.resolve_buy(pending, fallback_price=fb)
        with use_lane(pending.lane):
            result = _materialize_pending_buy(pending, resolved)
        filled.append(result)
    return filled


def drain_pending_exits(
    *, fallback_price_fn: Optional[Any] = None
) -> list[dict[str, Any]]:
    """Resolve deferred sells on the latency timer (not the next trenches tick).

    For each open lot past ``pending_exit_at + LATENCY``:
      1. Poll Dex for a land mid (fallback)
      2. Fill at the worst mid in ``[signal, signal+LATENCY]``
      3. Book the close

    Also polls Dex once for *waiting* pending exits so the 1.5s window has
    real marks even when pricefeed is sparse.
    """
    if not exec_sim.ENABLED:
        return []
    now = time.time()
    due: list[tuple[str, PaperPosition]] = []
    waiting: list[tuple[str, PaperPosition]] = []
    with _lock:
        for lane in list(_books.keys()):
            book = _books[lane]
            for pos in book.open:
                if pos.status != "open" or pos.pending_exit_at is None:
                    continue
                if now >= float(pos.pending_exit_at) + exec_sim.LATENCY_SEC:
                    due.append((lane, pos))
                else:
                    waiting.append((lane, pos))

    # Seed the land window while we wait.
    for lane, pos in waiting:
        if not pos.mint or fallback_price_fn is None:
            continue
        try:
            fb = fallback_price_fn(str(pos.mint), str(pos.address))
        except Exception:  # noqa: BLE001
            fb = None
        if fb and fb > 0:
            exec_sim.observe(str(pos.mint), float(fb), ts=now)

    closed: list[dict[str, Any]] = []
    for lane, pos in due:
        fb = None
        if fallback_price_fn is not None and pos.mint:
            try:
                fb = fallback_price_fn(str(pos.mint), str(pos.address))
            except Exception:  # noqa: BLE001
                fb = None
        if fb and fb > 0 and pos.mint:
            exec_sim.observe(str(pos.mint), float(fb), ts=now)
        with use_lane(lane):
            row = _resolve_due_pending_exit(pos.id, fallback_price=fb, now=now)
            if row:
                closed.append(row)
    return closed


def tick_exec_sim(*, fallback_price_fn: Optional[Any] = None) -> dict[str, Any]:
    """Drain due buys and sells. Safe to call from a fast timer thread."""
    if not exec_sim.ENABLED:
        return {"buys": [], "sells": []}
    buys = drain_pending_buys(fallback_price_fn=fallback_price_fn)
    sells = drain_pending_exits(fallback_price_fn=fallback_price_fn)
    return {"buys": buys, "sells": sells}


_resolver_thread: Optional[threading.Thread] = None
_resolver_stop = threading.Event()
RESOLVER_TICK_SEC = _env_float("EXEC_SIM_RESOLVER_TICK_SEC", 0.25)


def start_exec_resolver() -> dict[str, Any]:
    """Background timer: resolve pending buys/sells at LATENCY_SEC with Dex polls."""
    global _resolver_thread
    if not exec_sim.ENABLED:
        return {"running": False, "reason": "EXEC_SIM_ENABLED=0"}
    with _lock:
        if _resolver_thread and _resolver_thread.is_alive():
            return {"running": True, "tick_sec": RESOLVER_TICK_SEC}
        _resolver_stop.clear()
        _resolver_thread = threading.Thread(
            target=_exec_resolver_loop, name="exec-sim-resolver", daemon=True
        )
        _resolver_thread.start()
    pipeline_log.emit(
        "exec_sim",
        "resolver_start",
        latency_sec=exec_sim.LATENCY_SEC,
        tick_sec=RESOLVER_TICK_SEC,
    )
    return {"running": True, "tick_sec": RESOLVER_TICK_SEC}


def stop_exec_resolver() -> None:
    _resolver_stop.set()


def _default_dex_fallback(mint: str, pool: str = "") -> Optional[float]:
    try:
        from decision import trenches

        return trenches.dex_price_usd(mint, pool=pool or None)
    except Exception:  # noqa: BLE001
        return None


def _exec_resolver_loop() -> None:
    while not _resolver_stop.wait(RESOLVER_TICK_SEC):
        try:
            tick_exec_sim(fallback_price_fn=_default_dex_fallback)
        except Exception as exc:  # noqa: BLE001
            pipeline_log.emit(
                "exec_sim",
                "resolver_error",
                level="warning",
                reason=type(exc).__name__,
            )


def _resolve_due_pending_exit(
    position_id: str,
    *,
    fallback_price: Optional[float],
    now: float,
) -> Optional[dict[str, Any]]:
    """Book one due pending exit under the active lane lock."""
    with _lock:
        pos = next((p for p in _state.open if p.id == position_id and p.status == "open"), None)
        if pos is None or pos.pending_exit_at is None:
            return None
        if now < float(pos.pending_exit_at) + exec_sim.LATENCY_SEC:
            return None

        exit_fee_frac = costs.EXIT_FEE_PCT / 100.0
        exit_slip_frac = costs.EXIT_SLIP_PCT / 100.0
        exit_cost_frac = exit_fee_frac + exit_slip_frac
        signal = float(pos.pending_exit_signal_price or pos.entry_price)
        resolved = exec_sim.resolve_sell(
            mint=str(pos.mint or ""),
            signal_price=signal,
            signal_at=float(pos.pending_exit_at),
            fallback_price=fallback_price,
            now=now,
        )
        exit_price = float(resolved["fill_price_raw"])
        pnl = pos.qty * exit_price - pos.size_usd - pos.size_usd * exit_cost_frac
        if "stop_loss" in str(pos.pending_exit_reason or ""):
            exit_price, pnl, stop_reason = _resolve_stop_exit(
                pos, exit_price, pnl, exit_cost_frac
            )
            reason = (
                f"{pos.pending_exit_reason} → {stop_reason} "
                f"exec_lag {resolved['latency_sec']}s "
                f"adverse {resolved['adverse_pct']}%"
            )
        else:
            reason = (
                f"{pos.pending_exit_reason} "
                f"exec_lag {resolved['latency_sec']}s "
                f"adverse {resolved['adverse_pct']}% "
                f"fill=${exit_price:.8g} (signal ${pos.pending_exit_signal_price})"
            )
        return _book_close_locked(pos, exit_price=exit_price, pnl=pnl, reason=reason, now=now)


def _book_close_locked(
    pos: PaperPosition,
    *,
    exit_price: float,
    pnl: float,
    reason: str,
    now: float,
) -> dict[str, Any]:
    """Finalize a close. Caller must hold ``_lock`` and ``pos`` must be in ``_state.open``."""
    exit_fee_frac = costs.EXIT_FEE_PCT / 100.0
    exit_slip_frac = costs.EXIT_SLIP_PCT / 100.0
    exit_fee = pos.size_usd * exit_fee_frac
    exit_slip = pos.size_usd * exit_slip_frac
    proceeds = pos.qty * exit_price - exit_fee - exit_slip
    _state.cash_usd += proceeds
    _state.realized_pnl_usd += pnl
    pos.status = "closed"
    pos.exit_price = exit_price
    pos.realized_pnl_usd = round(pnl, 4)
    pos.closed_at = datetime.now(timezone.utc).isoformat()
    pos.close_reason = reason
    pos.pending_exit_at = None
    pos.fills.append(
        asdict(
            PaperFill(
                side="sell",
                price=exit_price,
                size_usd=proceeds,
                fee_usd=exit_fee,
                slippage_pct=costs.EXIT_SLIP_PCT,
                ts=pos.closed_at,
            )
        )
    )
    _state.closed.append(pos)
    _state.open = [p for p in _state.open if p.id != pos.id]
    record = {"event": "close", "position": asdict(pos)}
    try:
        decision_store.append("paper", record)
        from decision import scoreboard

        scoreboard.record_outcome(
            address=pos.address,
            mint=pos.mint,
            timeframe=pos.timeframe or "live",
            horizon_bars=pos.horizon_bars,
            predicted_p50=float(pos.predicted_p50 or 0.0),
            predicted_p10=float(
                pos.predicted_p10
                if pos.predicted_p10 is not None
                else -pos.stop_loss_usd / max(pos.size_usd, 1e-9) * 100.0
            ),
            predicted_p90=float(
                pos.predicted_p90
                if pos.predicted_p90 is not None
                else pos.target_profit_usd / max(pos.size_usd, 1e-9) * 100.0
            ),
            entry_price=pos.entry_price,
            exit_price=exit_price,
            action="paper_scalp",
        )
    except OSError:
        pass
    except Exception:  # noqa: BLE001
        pass
    opened_ts = _parse_ts(pos.opened_at)
    pipeline_log.emit(
        "paper",
        "close",
        address=pos.address,
        mint=pos.mint,
        symbol=pos.name,
        position_id=pos.id,
        realized_pnl_usd=pos.realized_pnl_usd,
        reason=reason,
        exit_price=exit_price,
        entry_price=pos.entry_price,
        held_sec=round(now - opened_ts, 1) if opened_ts else None,
    )
    _queue_fill_audit(pos)
    return asdict(pos)


def execute_signal(
    *,
    address: str,
    mint: str,
    name: str,
    mark_price: float,
    size_usd: float,
    strategy: str,
    max_hold_sec: Optional[float] = None,
    extra: Optional[dict[str, Any]] = None,
    target_profit_usd: Optional[float] = None,
    bank_at_target: bool = False,
    stop_loss_usd: Optional[float] = None,
    take_profit_pct: float = 0.0,
    dead_after_sec: Optional[float] = None,
    abs_hold_sec: Optional[float] = None,
) -> dict[str, Any]:
    """Open a paper lot from a launch/cluster signal (no DecisionCard).

    When ``EXEC_SIM_ENABLED=1``, cash is reserved at the signal mid and the lot
    only opens after ``EXEC_SIM_LATENCY_SEC``, filled at the worst mid observed
    in that window (plus modeled entry slip) — matching submit→land adverse
    selection on Solana.
    """
    if LIVE_TRADING:
        return {"status": "refused", "reason": "LIVE_TRADING=1 is blocked in this build"}
    if _state.kill_switch or KILL_SWITCH:
        return {"status": "refused", "reason": "kill switch on"}
    if mark_price <= 0 or size_usd <= 0:
        return {"status": "refused", "reason": "bad price or size"}

    slip_pct = costs.entry_slip_pct()
    stop_usd = float(stop_loss_usd) if stop_loss_usd is not None else stop_loss_for_size(size_usd)
    target_usd = float(target_profit_usd) if target_profit_usd is not None else TARGET_PROFIT_USD
    fee_usd = size_usd * costs.ENTRY_FEE_PCT / 100.0
    notional = size_usd + fee_usd

    with _lock:
        already = [p for p in _state.open if p.address == address and p.status == "open"]
        if already and not ALLOW_ADD_ON:
            return {"status": "skipped", "reason": "already open for address"}
        if len(already) >= MAX_OPEN_LOTS_PER_ADDRESS:
            return {"status": "skipped", "reason": f"max {MAX_OPEN_LOTS_PER_ADDRESS} lots"}
        if len(_state.open) >= MAX_OPEN_POSITIONS:
            return {"status": "refused", "reason": "max open positions"}
        open_notional = sum(p.size_usd for p in _state.open)
        if open_notional + size_usd > MAX_NOTIONAL_USD:
            return {"status": "refused", "reason": "max notional"}
        if _state.cash_usd < notional:
            return {"status": "refused", "reason": "insufficient paper cash"}

        if exec_sim.ENABLED:
            _state.cash_usd -= notional
            pending = exec_sim.queue_buy(
                lane=active_lane(),
                address=address,
                mint=mint,
                name=name,
                signal_price=mark_price,
                size_usd=size_usd,
                strategy=strategy,
                reserved_usd=notional,
                max_hold_sec=max_hold_sec,
                extra=extra or {},
                target_profit_usd=target_usd,
                bank_at_target=bank_at_target,
                stop_loss_usd=stop_usd,
                take_profit_pct=take_profit_pct,
                dead_after_sec=dead_after_sec,
                abs_hold_sec=abs_hold_sec,
            )
            pipeline_log.emit(
                "paper",
                "buy_pending",
                address=address,
                mint=mint,
                symbol=name,
                size_usd=size_usd,
                strategy=strategy,
                signal_price=mark_price,
                latency_sec=exec_sim.LATENCY_SEC,
                lane=active_lane(),
            )
            return {
                "status": "pending",
                "pending_id": pending.id,
                "signal_price": mark_price,
                "fill_at_in_sec": exec_sim.LATENCY_SEC,
                "cash_usd": round(_state.cash_usd, 4),
            }

        fill_price = mark_price * (1.0 + slip_pct / 100.0)
        qty = size_usd / fill_price
        _state.cash_usd -= notional
        pos = PaperPosition(
            id=str(uuid.uuid4())[:8],
            address=address,
            mint=mint,
            name=name,
            entry_price=fill_price,
            size_usd=size_usd,
            qty=qty,
            opened_at=datetime.now(timezone.utc).isoformat(),
            target_profit_usd=target_usd,
            stop_loss_usd=stop_usd,
            entry_reason=strategy,
            max_hold_sec=max_hold_sec,
            bank_at_target=bool(bank_at_target),
            take_profit_pct=float(take_profit_pct or 0.0),
            dead_after_sec=float(dead_after_sec) if dead_after_sec else None,
            abs_hold_sec=float(abs_hold_sec) if abs_hold_sec else None,
            signal_entry_price=mark_price,
            fills=[
                asdict(
                    PaperFill(
                        side="buy",
                        price=fill_price,
                        size_usd=size_usd,
                        fee_usd=fee_usd,
                        slippage_pct=slip_pct,
                        ts=datetime.now(timezone.utc).isoformat(),
                    )
                )
            ],
            timeframe=strategy,
            horizon_bars=0,
        )
        _state.open.append(pos)
        record = {
            "event": "open",
            "strategy": strategy,
            "lane": active_lane(),
            "position": asdict(pos),
            "extra": extra or {},
        }
        try:
            decision_store.append("paper", record)
        except OSError:
            pass
        pipeline_log.emit(
            "paper",
            "open",
            address=address,
            mint=mint,
            symbol=name,
            size_usd=size_usd,
            strategy=strategy,
            lane=active_lane(),
        )
        return {"status": "opened", "position": asdict(pos), "cash_usd": round(_state.cash_usd, 4)}


def _materialize_pending_buy(
    pending: exec_sim.PendingBuy, resolved: dict[str, Any]
) -> dict[str, Any]:
    """Turn a reserved pending buy into an open lot at the adverse fill."""
    slip_pct = costs.entry_slip_pct()
    raw = float(resolved["fill_price_raw"])
    fill_price = raw * (1.0 + slip_pct / 100.0)
    fee_usd = pending.size_usd * costs.ENTRY_FEE_PCT / 100.0
    qty = pending.size_usd / fill_price if fill_price > 0 else 0.0
    extra = dict(pending.extra or {})
    extra.update(
        {
            "exec_sim": {
                "signal_price": resolved["signal_price"],
                "fill_price_raw": raw,
                "fill_price": fill_price,
                "adverse_pct": resolved["adverse_pct"],
                "latency_sec": resolved["latency_sec"],
                "marks_n": resolved["marks_n"],
            }
        }
    )
    with _lock:
        pos = PaperPosition(
            id=str(uuid.uuid4())[:8],
            address=pending.address,
            mint=pending.mint,
            name=pending.name,
            entry_price=fill_price,
            size_usd=pending.size_usd,
            qty=qty,
            opened_at=datetime.now(timezone.utc).isoformat(),
            target_profit_usd=float(
                pending.target_profit_usd
                if pending.target_profit_usd is not None
                else TARGET_PROFIT_USD
            ),
            stop_loss_usd=float(
                pending.stop_loss_usd
                if pending.stop_loss_usd is not None
                else stop_loss_for_size(pending.size_usd)
            ),
            entry_reason=pending.strategy,
            max_hold_sec=pending.max_hold_sec,
            bank_at_target=bool(pending.bank_at_target),
            take_profit_pct=float(pending.take_profit_pct or 0.0),
            dead_after_sec=float(pending.dead_after_sec)
            if pending.dead_after_sec
            else None,
            abs_hold_sec=float(pending.abs_hold_sec) if pending.abs_hold_sec else None,
            signal_entry_price=float(pending.signal_price),
            fills=[
                asdict(
                    PaperFill(
                        side="buy",
                        price=fill_price,
                        size_usd=pending.size_usd,
                        fee_usd=fee_usd,
                        slippage_pct=slip_pct,
                        ts=datetime.now(timezone.utc).isoformat(),
                    )
                )
            ],
            timeframe=pending.strategy,
            horizon_bars=0,
        )
        _state.open.append(pos)
        record = {
            "event": "open",
            "strategy": pending.strategy,
            "lane": active_lane(),
            "position": asdict(pos),
            "extra": extra,
        }
        try:
            decision_store.append("paper", record)
        except OSError:
            pass
        pipeline_log.emit(
            "paper",
            "open",
            address=pending.address,
            mint=pending.mint,
            symbol=pending.name,
            size_usd=pending.size_usd,
            strategy=pending.strategy,
            lane=active_lane(),
            signal_price=pending.signal_price,
            fill_price=fill_price,
            adverse_pct=resolved["adverse_pct"],
            latency_sec=resolved["latency_sec"],
        )
        return {
            "status": "opened",
            "position": asdict(pos),
            "cash_usd": round(_state.cash_usd, 4),
            "exec_sim": resolved,
        }

def close_by_mint(*, mint: str, mark_price: float, reason: str) -> list[dict[str, Any]]:
    """Force-close every open lot on ``mint`` (cluster-sell / creator dump)."""
    if mark_price <= 0:
        return []
    mint = (mint or "").strip()
    if not mint:
        return []
    closed: list[dict[str, Any]] = []
    now = time.time()
    with _lock:
        still: list[PaperPosition] = []
        for pos in _state.open:
            if (pos.mint or "") != mint or pos.status != "open":
                still.append(pos)
                continue
            exit_fee_frac = costs.EXIT_FEE_PCT / 100.0
            exit_slip_frac = costs.EXIT_SLIP_PCT / 100.0
            pnl = pos.qty * mark_price - pos.size_usd - pos.size_usd * (
                exit_fee_frac + exit_slip_frac
            )
            proceeds = pos.qty * mark_price - pos.size_usd * (exit_fee_frac + exit_slip_frac)
            opened = _parse_ts(pos.opened_at)
            held = now - opened if opened else 0.0
            _state.cash_usd += proceeds
            _state.realized_pnl_usd += pnl
            pos.status = "closed"
            pos.exit_price = mark_price
            pos.realized_pnl_usd = round(pnl, 4)
            pos.closed_at = datetime.now(timezone.utc).isoformat()
            pos.close_reason = reason
            _state.closed.append(pos)
            try:
                decision_store.append("paper", {"event": "close", "position": asdict(pos)})
            except OSError:
                pass
            pipeline_log.emit(
                "paper",
                "close",
                address=pos.address,
                mint=pos.mint,
                symbol=pos.name,
                position_id=pos.id,
                realized_pnl_usd=pos.realized_pnl_usd,
                reason=reason,
                held_sec=round(held, 1),
            )
            closed.append(asdict(pos))
            _queue_fill_audit(pos)
        _state.open = still
    return closed


def execute_arb_fill(
    *,
    size_usd: float,
    pnl_usd: float,
    mint: str,
    symbol: str,
    record: dict[str, Any],
) -> dict[str, Any]:
    """Book an atomic Jupiter round-trip against the shared paper bankroll."""
    if LIVE_TRADING:
        return {"status": "refused", "reason": "LIVE_TRADING=1 is blocked in this build"}
    if _state.kill_switch or KILL_SWITCH:
        return {"status": "refused", "reason": "kill switch on"}
    size = float(size_usd)
    pnl = float(pnl_usd)
    if size <= 0:
        return {"status": "refused", "reason": "arb size must be positive"}

    with _lock:
        if _state.cash_usd < size:
            return {
                "status": "refused",
                "reason": f"insufficient paper cash (${_state.cash_usd:.2f} < ${size:.2f})",
            }
        # Atomic round-trip: deploy notional, return principal + Jupiter PnL.
        _state.cash_usd -= size
        _state.cash_usd += size + pnl
        _state.realized_pnl_usd += pnl
        _state.arb_realized_pnl_usd += pnl
        _state.arb_notional_usd += size
        _state.arb_fills += 1
        cash_after = round(_state.cash_usd, 4)
        arb_fills = _state.arb_fills

    log_record = dict(record)
    log_record["portfolio_cash_usd"] = cash_after
    try:
        decision_store.append("paper", log_record)
    except OSError:
        pass
    pipeline_log.emit(
        "paper",
        "arb_fill",
        mint=mint,
        symbol=symbol,
        size_usd=size,
        realized_pnl_usd=round(pnl, 4),
        cash_usd=cash_after,
        arb_fills=arb_fills,
    )
    return {
        "status": "filled",
        "cash_usd": cash_after,
        "realized_pnl_usd": round(pnl, 4),
        "arb_fills": arb_fills,
    }


def execute_decision(
    card: DecisionCard,
    *,
    mark_price: Optional[float] = None,
) -> dict[str, Any]:
    """Paper-execute a DecisionCard. Refuses unless action=buy and risk_pass."""
    tok = card.token or {}
    address = tok.get("address") or ""
    mint = tok.get("mint")
    name = tok.get("name")
    result = _execute_decision_inner(card, mark_price=mark_price)
    status = result.get("status")
    level = "info" if status == "opened" else ("warning" if status == "refused" else "info")
    pipeline_log.emit(
        "paper",
        status or "unknown",
        level=level,
        address=address,
        mint=mint,
        symbol=name,
        reason=result.get("reason"),
        action=getattr(card.action, "value", None),
        edge=card.cost_adjusted_edge_pct,
        size_usd=(result.get("position") or {}).get("size_usd")
        if isinstance(result.get("position"), dict)
        else None,
        position_id=(result.get("position") or {}).get("id")
        if isinstance(result.get("position"), dict)
        else None,
    )
    if status == "opened":
        pipeline_log.emit(
            "gate4",
            "pass",
            address=address,
            mint=mint,
            symbol=name,
            position_id=(result.get("position") or {}).get("id"),
        )
    elif status == "refused" and result.get("reason"):
        # Portfolio / concentration / kill — Gate 4 territory.
        reason = str(result["reason"])
        if any(
            key in reason
            for key in (
                "max open",
                "max notional",
                "insufficient",
                "mint daily",
                "kill switch",
                "LIVE_TRADING",
                "risk_pass",
            )
        ):
            pipeline_log.emit(
                "gate4",
                "fail",
                level="warning",
                address=address,
                mint=mint,
                symbol=name,
                reason=reason,
            )
    return result


def _execute_decision_inner(
    card: DecisionCard,
    *,
    mark_price: Optional[float] = None,
) -> dict[str, Any]:
    """Paper-execute a DecisionCard. Refuses unless action=buy and risk_pass."""
    if LIVE_TRADING:
        return {"status": "refused", "reason": "LIVE_TRADING=1 is blocked in this build"}
    if _state.kill_switch or KILL_SWITCH:
        return {"status": "refused", "reason": "kill switch on"}
    if card.action is not Action.BUY:
        return {"status": "skipped", "reason": f"action={card.action.value}"}
    if not card.risk.risk_pass:
        return {"status": "refused", "reason": "risk_pass=false", "veto": card.risk.veto_reasons}

    price = mark_price or (card.price_targets.entry if card.price_targets else None)
    if price is None or price <= 0:
        return {"status": "refused", "reason": "no entry price"}

    address = (card.token or {}).get("address") or ""
    mint = (card.token or {}).get("mint")
    name = (card.token or {}).get("name")
    # `max_size_usd or SCALP_SIZE_USD` used to sit here, which silently traded
    # full size whenever the card said zero — 0.0 is falsy, so an explicit
    # "cannot size this" (unmeasured liquidity, risk veto, desk override) became
    # a $40 fill and the guard below was unreachable. Honour the card's number.
    planned = card.position.max_size_usd if card.position else None
    size = min(SCALP_SIZE_USD, float(planned)) if planned is not None else 0.0
    if size <= 0:
        basis = (card.position.size_basis if card.position else None) or "unset"
        return {
            "status": "refused",
            "reason": f"card sized this position at $0.00 ({basis})",
        }

    # Model entry slippage from safety impact when present.
    impact = None
    if isinstance(card.safety, dict):
        sell = card.safety.get("sellability") or {}
        impact = sell.get("price_impact_pct")
    slip_pct = costs.entry_slip_pct(price_impact_pct=impact)

    stop_usd = stop_loss_for_size(size)

    # Barriers that cannot be reached in the intended direction are a config
    # error, not a trade: refuse rather than open a guaranteed loss.
    degenerate = costs.degenerate_stop_reason(
        size_usd=size,
        target_usd=TARGET_PROFIT_USD,
        stop_usd=stop_usd,
        entry_slip_pct_=slip_pct,
    )
    if degenerate:
        return {"status": "refused", "reason": degenerate}

    # Gate 2 sized the edge against nominal SCALP_SIZE_USD, but confidence and
    # liquidity caps can shrink the fill — and a fixed dollar target needs a
    # *larger* percentage move at smaller size. Re-check against the real size.
    moves = costs.barrier_moves(
        size_usd=size,
        target_usd=TARGET_PROFIT_USD,
        stop_usd=stop_usd,
        entry_slip_pct_=slip_pct,
    )
    needed = moves["target_from_quote_pct"]
    median = card.expected_return_pct.p50 if card.expected_return_pct else None
    notes = getattr(card, "notes", None) or {}
    momentum_entry = bool(notes.get("momentum_entry"))
    # A momentum entry is taken on tape, not on the forecast median, so the
    # median-vs-target check would veto exactly the setups it exists to catch.
    # Paper risk-on: Gate 2 already cleared; don't re-veto on a second hurdle
    # that exists to keep live size honest.
    if (
        median is not None
        and median < needed
        and not momentum_entry
        and not paper_risk_on()
    ):
        return {
            "status": "skipped",
            "reason": (
                f"median {median:+.2f}% cannot reach the {needed:+.2f}% move that banks "
                f"${TARGET_PROFIT_USD:.2f} on ${size:.2f}"
            ),
        }

    fill_price = price * (1.0 + slip_pct / 100.0)
    fee_usd = size * costs.ENTRY_FEE_PCT / 100.0
    notional = size + fee_usd
    qty = size / fill_price

    with _lock:
        already = [
            p for p in _state.open if p.address == address and p.status == "open"
        ]
        if already and not ALLOW_ADD_ON:
            return {"status": "skipped", "reason": "already open for address"}
        entry_block = _entry_block_reason(
            address=address,
            mint=mint,
            mark_price=price,
            open_lots=already,
            add_on=len(already) > 0,
        )
        if entry_block:
            return {"status": "skipped", "reason": entry_block}
        cooling = _cooldown_remaining(address)
        if cooling > 0:
            return {
                "status": "skipped",
                "reason": f"re-entry cooldown {cooling:.0f}s remaining",
            }
        conc = _concentration_block(mint=mint, address=address, size=size)
        if conc:
            return {"status": "refused", "reason": conc}
        if len(_state.open) >= MAX_OPEN_POSITIONS:
            return {"status": "refused", "reason": "max open positions"}
        open_notional = sum(p.size_usd for p in _state.open)
        if open_notional + size > MAX_NOTIONAL_USD:
            return {"status": "refused", "reason": "max notional"}
        if _state.cash_usd < notional:
            return {"status": "refused", "reason": "insufficient paper cash"}

        band = card.expected_return_pct
        _state.cash_usd -= notional
        pos = PaperPosition(
            id=str(uuid.uuid4())[:8],
            address=address,
            mint=mint,
            name=name,
            entry_price=fill_price,
            size_usd=size,
            qty=qty,
            opened_at=datetime.now(timezone.utc).isoformat(),
            target_profit_usd=TARGET_PROFIT_USD,
            stop_loss_usd=stop_usd,
            entry_reason="momentum" if momentum_entry else "forecast",
            fills=[
                asdict(
                    PaperFill(
                        side="buy",
                        price=fill_price,
                        size_usd=size,
                        fee_usd=fee_usd,
                        slippage_pct=slip_pct,
                        ts=datetime.now(timezone.utc).isoformat(),
                    )
                )
            ],
            predicted_p10=band.p10 if band else None,
            predicted_p50=band.p50 if band else None,
            predicted_p90=band.p90 if band else None,
            timeframe=card.timeframe,
            horizon_bars=int(card.horizon_bars or 0),
        )
        _state.open.append(pos)
        add_on = len(already) > 0
        record = {
            "event": "open",
            "add_on": add_on,
            "open_lots_for_address": len(already) + 1,
            "position": asdict(pos),
            "card_summary": card.summary(),
        }
        try:
            decision_store.append("paper", record)
        except OSError:
            pass
        pipeline_log.emit(
            "paper",
            "add_on" if add_on else "open",
            address=address,
            mint=mint,
            size_usd=size,
            lots=len(already) + 1,
        )
        return {
            "status": "opened",
            "add_on": add_on,
            "position": asdict(pos),
        }


def mark_and_maybe_exit(
    *,
    address: str,
    mark_price: float,
    force_reason: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Update open positions for address; close on stop, trail, or a tape force.

    Stop fills honour ``PAPER_STOP_FILL_MODE``. Default ``barrier`` exits at the
    price that realizes exactly ``-stop_loss_usd``, so a gap through the level
    does not invent a -$9 loss on a $1.50 stop. ``mark`` keeps live-like gaps
    but still clamps to ``PAPER_MAX_LOSS_PCT`` of size as a circuit breaker.

    With ``EXEC_SIM_ENABLED=1``, an exit signal does not sell at this tick's mid.
    We wait ``EXEC_SIM_LATENCY_SEC``, keep recording mids, then fill at the
    *lowest* mid in that window (worst sell) — submit→land adverse selection.
    """
    if mark_price <= 0:
        return []
    closed: list[dict[str, Any]] = []
    now = time.time()
    with _lock:
        still_open: list[PaperPosition] = []
        for pos in _state.open:
            if pos.address != address or pos.status != "open":
                still_open.append(pos)
                continue

            if exec_sim.ENABLED and pos.mint:
                exec_sim.observe(str(pos.mint), mark_price, ts=now)

            exit_fee_frac = costs.EXIT_FEE_PCT / 100.0
            exit_slip_frac = costs.EXIT_SLIP_PCT / 100.0
            exit_cost_frac = exit_fee_frac + exit_slip_frac

            # Deferred exit: resolver thread books the fill at signal+LATENCY.
            # Keep observing mids here; do not widen/resolve on the next tick.
            if exec_sim.ENABLED and pos.pending_exit_at is not None:
                still_open.append(pos)
                continue

            else:
                mark_pnl = pos.qty * mark_price - pos.size_usd - pos.size_usd * exit_cost_frac
                opened = _parse_ts(pos.opened_at)
                held = now - opened if opened else 0.0

                reason = None
                exit_price = mark_price
                pnl = mark_pnl

                suppress_stop = (
                    pos.entry_reason in ("launch", "cluster")
                    and _venue_gap(pos.entry_price, mark_price)
                    and held < STOP_GRACE_SEC
                    and mark_price > pos.entry_price
                )

                max_jump = pos.size_usd * TRAIL_MAX_JUMP_PCT / 100.0
                last_accepted = pos.last_mark_pnl_usd
                venue_blowup = (
                    pos.entry_reason in ("launch", "cluster")
                    and pos.entry_price > 0
                    and abs(mark_price / pos.entry_price - 1.0) > 2.0
                )
                if venue_blowup:
                    decision_pnl = last_accepted
                elif mark_pnl - last_accepted > max_jump:
                    decision_pnl = last_accepted + max_jump
                else:
                    decision_pnl = mark_pnl
                pos.last_mark_pnl_usd = decision_pnl
                if decision_pnl > pos.peak_pnl_usd:
                    pos.peak_pnl_usd = decision_pnl

                hold_limit = pos.max_hold_sec if pos.max_hold_sec else MAX_HOLD_SEC
                abs_limit = pos.abs_hold_sec if pos.abs_hold_sec else None
                situational = pos.dead_after_sec is not None
                stalled = pos.peak_pnl_usd <= 0 or decision_pnl <= 0

                if mark_pnl <= -pos.stop_loss_usd and not suppress_stop:
                    exit_price, pnl, reason = _resolve_stop_exit(
                        pos, mark_price, mark_pnl, exit_cost_frac
                    )
                    if force_reason:
                        reason = f"{force_reason} {reason}"
                elif pos.bank_at_target and (
                    (
                        (pos.take_profit_pct or 0.0) > 0
                        and decision_pnl >= pos.size_usd * pos.take_profit_pct / 100.0
                    )
                    or (
                        (pos.take_profit_pct or 0.0) <= 0
                        and decision_pnl >= pos.target_profit_usd
                    )
                ):
                    rip = pos.size_usd * (pos.take_profit_pct or 0.0) / 100.0
                    if rip > 0 and decision_pnl >= rip:
                        pnl = rip
                        reason = f"snipe_rip ${pnl:.2f}"
                    else:
                        pnl = pos.target_profit_usd
                        reason = f"snipe_target ${pnl:.2f}"
                    exit_price = _implied_exit_price(pos, pnl, exit_cost_frac)
                elif force_reason:
                    if _venue_gap(pos.entry_price, mark_price):
                        pnl = last_accepted
                        exit_price = _implied_exit_price(pos, pnl, exit_cost_frac)
                        reason = (
                            f"{force_reason} pnl=${pnl:.2f} "
                            f"(unconfirmed mark ${mark_pnl:.2f})"
                        )
                    else:
                        reason = f"{force_reason} pnl=${mark_pnl:.2f}"
                        exit_price = mark_price
                        pnl = mark_pnl
                elif (
                    situational
                    and pos.dead_after_sec
                    and pos.peak_pnl_usd <= 0
                    and held >= pos.dead_after_sec
                ):
                    if mark_pnl <= -pos.stop_loss_usd:
                        exit_price, pnl, reason = _resolve_stop_exit(
                            pos, mark_price, mark_pnl, exit_cost_frac
                        )
                        reason = f"dead_tape {held:.0f}s {reason}"
                    elif _venue_gap(pos.entry_price, mark_price):
                        pnl = last_accepted
                        exit_price = _implied_exit_price(pos, pnl, exit_cost_frac)
                        reason = (
                            f"dead_tape {held:.0f}s pnl=${pnl:.2f} "
                            f"(unconfirmed mark ${mark_pnl:.2f})"
                        )
                    else:
                        reason = f"dead_tape {held:.0f}s pnl=${mark_pnl:.2f}"
                        exit_price = mark_price
                        pnl = mark_pnl
                elif abs_limit and held >= abs_limit:
                    reason = f"abs_hold {held:.0f}s pnl=${mark_pnl:.2f}"
                    exit_price = mark_price
                    pnl = mark_pnl
                elif held >= hold_limit and (not situational or stalled):
                    if mark_pnl - last_accepted > max_jump:
                        pnl = last_accepted
                        exit_price = _implied_exit_price(pos, pnl, exit_cost_frac)
                        reason = (
                            f"max_hold {held:.0f}s pnl=${pnl:.2f} "
                            f"(unconfirmed mark ${mark_pnl:.2f})"
                        )
                    else:
                        reason = f"max_hold {held:.0f}s pnl=${mark_pnl:.2f}"
                        exit_price = mark_price
                        pnl = mark_pnl
                elif pos.bank_at_target:
                    use_trail = TRAIL_ENABLED or situational
                    trailed = _trail_exit(pos, decision_pnl) if use_trail else None
                    if trailed is not None:
                        pnl, reason = trailed
                        exit_price = _implied_exit_price(pos, pnl, exit_cost_frac)
                else:
                    trailed = _trail_exit(pos, decision_pnl) if TRAIL_ENABLED else None
                    if trailed is not None:
                        pnl, reason = trailed
                        exit_price = _implied_exit_price(pos, pnl, exit_cost_frac)
                    elif TRAIL_ENABLED and pos.trail_armed:
                        still_open.append(pos)
                        continue
                    elif mark_pnl >= pos.target_profit_usd and not TRAIL_ENABLED:
                        reason = f"target_profit ${mark_pnl:.2f}"
                        exit_price = mark_price
                        pnl = mark_pnl

                if reason is None:
                    still_open.append(pos)
                    continue

                if exec_sim.ENABLED:
                    pos.pending_exit_at = now
                    pos.pending_exit_reason = reason
                    pos.pending_exit_signal_price = float(exit_price)
                    pipeline_log.emit(
                        "paper",
                        "sell_pending",
                        address=pos.address,
                        mint=pos.mint,
                        symbol=pos.name,
                        reason=reason,
                        signal_price=exit_price,
                        latency_sec=exec_sim.LATENCY_SEC,
                    )
                    still_open.append(pos)
                    continue

            exit_fee = pos.size_usd * exit_fee_frac
            exit_slip = pos.size_usd * exit_slip_frac
            proceeds = pos.qty * exit_price - exit_fee - exit_slip
            _state.cash_usd += proceeds
            _state.realized_pnl_usd += pnl
            pos.status = "closed"
            pos.exit_price = exit_price
            pos.realized_pnl_usd = round(pnl, 4)
            pos.closed_at = datetime.now(timezone.utc).isoformat()
            pos.close_reason = reason
            pos.fills.append(
                asdict(
                    PaperFill(
                        side="sell",
                        price=exit_price,
                        size_usd=proceeds,
                        fee_usd=exit_fee,
                        slippage_pct=costs.EXIT_SLIP_PCT,
                        ts=pos.closed_at,
                    )
                )
            )
            _state.closed.append(pos)
            record = {"event": "close", "position": asdict(pos)}
            try:
                decision_store.append("paper", record)
                from decision import scoreboard

                scoreboard.record_outcome(
                    address=pos.address,
                    mint=pos.mint,
                    timeframe=pos.timeframe or "live",
                    horizon_bars=pos.horizon_bars,
                    predicted_p50=float(pos.predicted_p50 or 0.0),
                    predicted_p10=float(
                        pos.predicted_p10
                        if pos.predicted_p10 is not None
                        else -pos.stop_loss_usd / max(pos.size_usd, 1e-9) * 100.0
                    ),
                    predicted_p90=float(
                        pos.predicted_p90
                        if pos.predicted_p90 is not None
                        else pos.target_profit_usd / max(pos.size_usd, 1e-9) * 100.0
                    ),
                    entry_price=pos.entry_price,
                    exit_price=exit_price,
                    action="paper_scalp",
                )
            except OSError:
                pass
            except Exception:  # noqa: BLE001
                pass
            opened_ts = _parse_ts(pos.opened_at)
            pipeline_log.emit(
                "paper",
                "close",
                address=pos.address,
                mint=pos.mint,
                symbol=pos.name,
                position_id=pos.id,
                realized_pnl_usd=pos.realized_pnl_usd,
                reason=reason,
                exit_price=exit_price,
                entry_price=pos.entry_price,
                held_sec=round(now - opened_ts, 1) if opened_ts else None,
            )
            closed.append(asdict(pos))
            _queue_fill_audit(pos)
        _state.open = still_open
    if force_reason is None:
        closed.extend(_maybe_jev_exit(address, mark_price, now))
    return closed


def _exit_state(pos: Any, mark_price: float, held: float) -> dict[str, Any]:
    """Evidence the chair judges an open lot on — all computed in code."""
    last = float(getattr(pos, "last_mark_pnl_usd", 0.0) or 0.0)
    size = float(getattr(pos, "size_usd", 0.0) or 0.0)
    peak = float(getattr(pos, "peak_pnl_usd", 0.0) or 0.0)
    stop = float(getattr(pos, "stop_loss_usd", 0.0) or 0.0)
    target = float(getattr(pos, "target_profit_usd", 0.0) or 0.0)
    return {
        "symbol": str(getattr(pos, "name", "") or ""),
        "strategy": str(getattr(pos, "entry_reason", "") or ""),
        "size_usd": size,
        "entry_price": getattr(pos, "entry_price", None),
        "mark_price": mark_price,
        "pnl_usd": round(last, 2),
        "pnl_pct": round(last / size * 100.0, 2) if size > 0 else 0.0,
        "peak_usd": round(peak, 2),
        "held_sec": round(held, 1),
        "dist_to_stop_usd": round(last + stop, 2),
        "dist_to_target_usd": round(target - last, 2),
    }


def _maybe_jev_exit(address: str, mark_price: float, now: float) -> list[dict[str, Any]]:
    """Ask the chair whether still-open lots should exit now.

    Fail-open: any failure or an unavailable chair leaves lots untouched.
    One API call per due lot at most; force-closes route through the
    normal ``force_reason`` path so fills, store, and audit stay uniform.
    """
    if not JEV_EXIT_MONITOR or mark_price <= 0:
        return []
    try:
        from decision import jev as jev_mod
    except Exception:  # noqa: BLE001
        return []
    with _lock:
        candidates = [
            p
            for p in _state.open
            if p.address == address
            and p.status == "open"
            and p.pending_exit_at is None
        ]
    extra: list[dict[str, Any]] = []
    for pos in candidates:
        opened = _parse_ts(pos.opened_at)
        held = now - opened if opened else 0.0
        if held < JEV_EXIT_MIN_HOLD_SEC:
            continue
        pnl_now = float(getattr(pos, "last_mark_pnl_usd", 0.0) or 0.0)
        last_ts, last_pnl = _jev_exit_checked.get(pos.id, (0.0, pnl_now))
        slide = last_pnl - pnl_now
        if now - last_ts < JEV_EXIT_RECHECK_SEC and slide < JEV_EXIT_PNL_DROP_USD:
            continue
        _jev_exit_checked[pos.id] = (now, pnl_now)
        try:
            review = jev_mod.exit_review(_exit_state(pos, mark_price, held))
        except Exception:  # noqa: BLE001 — never break the mark loop
            continue
        if not review or not review.get("exit"):
            continue
        try:
            pipeline_log.emit(
                "paper",
                "jev_exit",
                address=pos.address,
                mint=pos.mint,
                symbol=pos.name,
                position_id=pos.id,
                p=review.get("p"),
                latency_ms=review.get("latency_ms"),
            )
        except Exception:  # noqa: BLE001
            pass
        extra.extend(
            mark_and_maybe_exit(
                address=address,
                mark_price=mark_price,
                force_reason=f"jev_exit p={review.get('p')}",
            )
        )
    return extra


def _resolve_stop_exit(
    pos: PaperPosition,
    mark_price: float,
    mark_pnl: float,
    exit_cost_frac: float,
) -> tuple[float, float, str]:
    """Choose stop exit price and booked PnL under the configured fill mode."""
    stop = pos.stop_loss_usd
    # Price that realizes exactly -stop after exit costs.
    barrier = pos.entry_price * (1.0 + exit_cost_frac - stop / max(pos.size_usd, 1e-9))
    if barrier <= 0:
        barrier = mark_price

    if STOP_FILL_MODE == "mark":
        floor = -pos.size_usd * (MAX_LOSS_PCT / 100.0)
        pnl = max(mark_pnl, floor)
        if pnl > mark_pnl + 1e-9:
            # Circuit breaker: gap exceeded max loss %.
            exit_price = pos.entry_price * (1.0 + exit_cost_frac + pnl / max(pos.size_usd, 1e-9))
            return exit_price, pnl, f"stop_loss ${pnl:.2f} (gap capped; mark was ${mark_pnl:.2f})"
        return mark_price, mark_pnl, f"stop_loss ${mark_pnl:.2f}"

    # Default: barrier fill. Report what the mark would have done.
    return (
        barrier,
        -stop,
        f"stop_loss $-{stop:.2f} (barrier; mark would be ${mark_pnl:.2f})",
    )


def _implied_exit_price(pos: PaperPosition, pnl: float, exit_cost_frac: float) -> float:
    """Mark that realizes ``pnl`` after exit costs. Used when we refuse a ghost mid."""
    if pos.qty <= 0:
        return pos.entry_price
    return (pnl + pos.size_usd * (1.0 + exit_cost_frac)) / pos.qty


def _venue_gap(entry_price: float, mark_price: float) -> bool:
    """True when the mark is a different print than the fill, not a small move."""
    if entry_price <= 0 or mark_price <= 0:
        return False
    return abs(mark_price / entry_price - 1.0) * 100.0 > ENTRY_CONFIRM_PCT


def stop_loss_for_size(size_usd: float) -> float:
    """Dollar stop for a position of ``size_usd``.

    Percent mode keeps the stop outside exit costs as size changes; the flat
    dollar stop is kept as the default so existing behaviour is unchanged.
    """
    if STOP_LOSS_PCT > 0:
        return round(max(size_usd * STOP_LOSS_PCT / 100.0, 0.01), 4)
    return STOP_LOSS_USD


def _trail_exit(pos: PaperPosition, mark_pnl: float) -> Optional[tuple[float, str]]:
    """Decide whether a trailing winner should bank now.

    Returns ``(pnl, reason)`` when the trail has been hit, else None. Arming is
    sticky: once a position has paid the arm amount we stop taking the fixed
    target and manage it on giveback from the peak instead.

    A single Dex print that jumps more than ``TRAIL_MAX_JUMP_PCT`` of size is
    treated as stale and ignored. A trail never books a close below the arm —
    that is the stop's job, not a take-profit.
    """
    arm = TRAIL_ARM_USD if TRAIL_ARM_USD > 0 else pos.target_profit_usd
    max_jump = pos.size_usd * TRAIL_MAX_JUMP_PCT / 100.0
    prev = pos.last_mark_pnl_usd
    if mark_pnl - prev > max_jump:
        # Ghost mid. Keep the last accepted peak; do not arm on a spike.
        return None

    pos.last_mark_pnl_usd = mark_pnl
    if not pos.trail_armed and mark_pnl < arm:
        return None

    pos.trail_armed = True
    pos.peak_pnl_usd = max(pos.peak_pnl_usd, mark_pnl)
    size_giveback = pos.size_usd * TRAIL_GIVEBACK_PCT / 100.0
    peak_giveback = pos.peak_pnl_usd * TRAIL_GIVEBACK_OF_PEAK
    giveback = max(size_giveback, peak_giveback)
    floor = max(arm, pos.peak_pnl_usd - giveback)
    if mark_pnl > floor:
        return None
    if mark_pnl < arm:
        # Peak was a ghost or the tape dumped through the trail. Disarm and
        # let the stop / max-hold handle the loser — do not label it a TP.
        pos.trail_armed = False
        pos.peak_pnl_usd = max(0.0, mark_pnl)
        return None
    return mark_pnl, (
        f"trail_take_profit ${mark_pnl:.2f} (peak ${pos.peak_pnl_usd:.2f}, "
        f"gave back ${pos.peak_pnl_usd - mark_pnl:.2f})"
    )


def _mint_key(mint: Optional[str], address: str) -> str:
    return (mint or address or "").strip() or address


def _utc_day(iso_ts: Optional[str] = None) -> str:
    if iso_ts:
        try:
            return datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            pass
    return datetime.now(timezone.utc).date().isoformat()


def _concentration_block(*, mint: Optional[str], address: str, size: float) -> Optional[str]:
    """Refuse when this mint has already eaten its session budget. Holds _lock."""
    key = _mint_key(mint, address)
    trades = 0
    notional = 0.0
    realized = 0.0
    for pos in _state.open:
        if _mint_key(pos.mint, pos.address) != key:
            continue
        if not session_scope.is_since(pos.opened_at):
            continue
        trades += 1
        notional += pos.size_usd
    for pos in _state.closed:
        if _mint_key(pos.mint, pos.address) != key:
            continue
        if not session_scope.is_since(pos.opened_at):
            continue
        trades += 1
        notional += pos.size_usd
        realized += float(pos.realized_pnl_usd or 0.0)

    if MAX_TRADES_PER_MINT_DAY > 0 and trades >= MAX_TRADES_PER_MINT_DAY:
        return f"mint daily trade cap {trades}/{MAX_TRADES_PER_MINT_DAY}"
    if MAX_NOTIONAL_PER_MINT_DAY > 0 and notional + size > MAX_NOTIONAL_PER_MINT_DAY:
        return (
            f"mint daily notional ${notional + size:.0f} > "
            f"${MAX_NOTIONAL_PER_MINT_DAY:.0f}"
        )
    if MAX_DAILY_LOSS_PER_MINT > 0 and realized <= -MAX_DAILY_LOSS_PER_MINT:
        return f"mint daily loss ${realized:.2f} hit -${MAX_DAILY_LOSS_PER_MINT:.2f} cap"
    return None


def _entry_block_reason(
    *,
    address: str,
    mint: Optional[str],
    mark_price: float,
    open_lots: list[PaperPosition],
    add_on: bool,
) -> Optional[str]:
    """Analysis-based entry blocks (not timer cool-downs). Caller holds _lock."""
    if len(open_lots) >= MAX_OPEN_LOTS_PER_ADDRESS:
        return f"max {MAX_OPEN_LOTS_PER_ADDRESS} open lots on this coin"

    if add_on and BLOCK_ADD_ON_IF_OPEN_LOSING and mark_price > 0:
        for pos in open_lots:
            if mark_price < pos.entry_price:
                return "add-on blocked: existing lot is losing at mark"

    if BLOCK_REPEAT_MINT_AFTER_STOP:
        key = _mint_key(mint, address)
        for pos in reversed(_state.closed):
            if _mint_key(pos.mint, pos.address) != key:
                continue
            if not session_scope.is_since(pos.closed_at):
                continue
            reason = str(pos.close_reason or "")
            if "stop_loss" in reason:
                return "won't re-scalp this coin this session after a stop loss"
            break
    return None


def _cooldown_remaining(address: str) -> float:
    """Seconds left before `address` may be re-entered. Caller must hold _lock."""
    if REENTRY_COOLDOWN_SEC <= 0:
        return 0.0
    now = time.time()
    latest = 0.0
    for pos in _state.closed:
        if pos.address != address or not pos.closed_at:
            continue
        ts = _parse_ts(pos.closed_at)
        if ts and ts > latest:
            latest = ts
    if latest <= 0:
        return 0.0
    return max(0.0, REENTRY_COOLDOWN_SEC - (now - latest))


def _queue_fill_audit(pos: PaperPosition) -> None:
    """Log-only: compare this close to Dex/Gecko/Helius. Never blocks a fill."""
    try:
        from decision import fill_audit

        fill_audit.observe_close(asdict(pos))
    except Exception:
        pass


def _parse_ts(raw: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
