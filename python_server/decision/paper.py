"""Phase 4 — paper AutoHedge: buy/sell simulation with portfolio limits.

Never touches a wallet. Fills are modeled from DecisionCard costs + optional
Jupiter impact. Target: take ~$1 net profit (DECIDE_TARGET_PROFIT_USD) then exit.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from decision import costs
from decision import pipeline_log
from decision import store as decision_store
from decision.config import _env_float, _env_int
from decision.packet import SCALP_SIZE_USD, TARGET_PROFIT_USD
from decision.schema import Action, DecisionCard

LIVE_TRADING = os.getenv("LIVE_TRADING", "0") == "1"
MAX_OPEN_POSITIONS = _env_int("PAPER_MAX_OPEN", 5)
MAX_NOTIONAL_USD = _env_float("PAPER_MAX_NOTIONAL_USD", 200.0)
STOP_LOSS_USD = _env_float("PAPER_STOP_LOSS_USD", 1.5)
MAX_HOLD_SEC = _env_float("PAPER_MAX_HOLD_SEC", 600.0)
KILL_SWITCH = os.getenv("PAPER_KILL_SWITCH", "0") == "1"
# Quiet period after closing a token before it may be re-entered. Without this
# the swarm re-buys on the very next tick after a stop, churning fees into the
# same adverse move.
REENTRY_COOLDOWN_SEC = _env_float("PAPER_REENTRY_COOLDOWN_SEC", 120.0)
# How stop exits are filled when price gaps through the barrier.
#   barrier — exit at the price that realizes exactly -stop_loss_usd (limit-stop ideal)
#   mark    — exit at the jumped mark (live-like; can blow past the dollar stop)
STOP_FILL_MODE = os.getenv("PAPER_STOP_FILL_MODE", "barrier").strip().lower()
# Absolute floor even in mark mode: never book worse than this fraction of size.
MAX_LOSS_PCT = _env_float("PAPER_MAX_LOSS_PCT", 8.0)
# Concentration: one mint must not dominate the day's sample / PnL.
MAX_TRADES_PER_MINT_DAY = _env_int("PAPER_MAX_TRADES_PER_MINT_DAY", 4)
MAX_NOTIONAL_PER_MINT_DAY = _env_float("PAPER_MAX_NOTIONAL_PER_MINT_DAY", 120.0)
MAX_DAILY_LOSS_PER_MINT = _env_float("PAPER_MAX_DAILY_LOSS_PER_MINT", 5.0)


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
    # Forecast snapshot at entry — feeds the live calibration loop.
    predicted_p10: Optional[float] = None
    predicted_p50: Optional[float] = None
    predicted_p90: Optional[float] = None
    timeframe: Optional[str] = None
    horizon_bars: int = 0


@dataclass
class PortfolioState:
    cash_usd: float = 1_000.0
    realized_pnl_usd: float = 0.0
    open: list[PaperPosition] = field(default_factory=list)
    closed: list[PaperPosition] = field(default_factory=list)
    kill_switch: bool = False
    live_trading_blocked: bool = True


_lock = threading.Lock()
_state = PortfolioState(kill_switch=KILL_SWITCH, live_trading_blocked=not LIVE_TRADING)


def snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "cash_usd": _state.cash_usd,
            "realized_pnl_usd": _state.realized_pnl_usd,
            "open_count": len(_state.open),
            "closed_count": len(_state.closed),
            "kill_switch": _state.kill_switch or KILL_SWITCH,
            "live_trading": LIVE_TRADING,
            "open": [asdict(p) for p in _state.open],
            "closed_recent": [asdict(p) for p in _state.closed[-20:]],
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
            },
        }


def set_kill_switch(enabled: bool) -> dict[str, Any]:
    with _lock:
        _state.kill_switch = bool(enabled)
    return snapshot()


def reset(*, starting_cash_usd: float = 1_000.0) -> dict[str, Any]:
    """Wipe open/closed paper book and restore starting cash."""
    with _lock:
        _state.cash_usd = float(starting_cash_usd)
        _state.realized_pnl_usd = 0.0
        _state.open = []
        _state.closed = []
        # Keep kill switch as configured; do not force it on.
    pipeline_log.emit(
        "paper",
        "reset",
        level="warning",
        cash_usd=starting_cash_usd,
    )
    return snapshot()


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

    # Barriers that cannot be reached in the intended direction are a config
    # error, not a trade: refuse rather than open a guaranteed loss.
    degenerate = costs.degenerate_stop_reason(
        size_usd=size,
        target_usd=TARGET_PROFIT_USD,
        stop_usd=STOP_LOSS_USD,
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
        stop_usd=STOP_LOSS_USD,
        entry_slip_pct_=slip_pct,
    )
    needed = moves["target_from_quote_pct"]
    median = card.expected_return_pct.p50 if card.expected_return_pct else None
    if median is not None and median < needed:
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
        if any(p.address == address and p.status == "open" for p in _state.open):
            return {"status": "skipped", "reason": "already open for address"}
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
            stop_loss_usd=STOP_LOSS_USD,
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
        record = {"event": "open", "position": asdict(pos), "card_summary": card.summary()}
        try:
            decision_store.append("paper", record)
        except OSError:
            pass
        return {"status": "opened", "position": asdict(pos)}


def mark_and_maybe_exit(
    *,
    address: str,
    mark_price: float,
) -> list[dict[str, Any]]:
    """Update open positions for address; close on +$1, stop, or max hold.

    Stop fills honour ``PAPER_STOP_FILL_MODE``. Default ``barrier`` exits at the
    price that realizes exactly ``-stop_loss_usd``, so a gap through the level
    does not invent a -$9 loss on a $1.50 stop. ``mark`` keeps live-like gaps
    but still clamps to ``PAPER_MAX_LOSS_PCT`` of size as a circuit breaker.
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
            exit_fee_frac = costs.EXIT_FEE_PCT / 100.0
            exit_slip_frac = costs.EXIT_SLIP_PCT / 100.0
            exit_cost_frac = exit_fee_frac + exit_slip_frac

            mark_pnl = pos.qty * mark_price - pos.size_usd - pos.size_usd * exit_cost_frac
            opened = _parse_ts(pos.opened_at)
            held = now - opened if opened else 0.0

            reason = None
            exit_price = mark_price
            pnl = mark_pnl

            if mark_pnl >= pos.target_profit_usd:
                # Bank at least the target; if mark overshoots, keep the overshoot.
                reason = f"target_profit ${mark_pnl:.2f}"
                exit_price = mark_price
                pnl = mark_pnl
            elif mark_pnl <= -pos.stop_loss_usd:
                exit_price, pnl, reason = _resolve_stop_exit(pos, mark_price, mark_pnl, exit_cost_frac)
            elif held >= MAX_HOLD_SEC:
                reason = f"max_hold {held:.0f}s pnl=${mark_pnl:.2f}"
                exit_price = mark_price
                pnl = mark_pnl

            if reason is None:
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
                held_sec=round(held, 1),
            )
            closed.append(asdict(pos))
        _state.open = still_open
    return closed


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
    """Refuse when this mint has already eaten its daily budget. Holds _lock."""
    key = _mint_key(mint, address)
    today = _utc_day()
    trades = 0
    notional = 0.0
    realized = 0.0
    for pos in _state.open:
        if _mint_key(pos.mint, pos.address) != key:
            continue
        if _utc_day(pos.opened_at) != today:
            continue
        trades += 1
        notional += pos.size_usd
    for pos in _state.closed:
        if _mint_key(pos.mint, pos.address) != key:
            continue
        if _utc_day(pos.opened_at) != today:
            continue
        trades += 1
        notional += pos.size_usd
        realized += float(pos.realized_pnl_usd or 0.0)

    if trades >= MAX_TRADES_PER_MINT_DAY:
        return f"mint daily trade cap {trades}/{MAX_TRADES_PER_MINT_DAY}"
    if notional + size > MAX_NOTIONAL_PER_MINT_DAY:
        return (
            f"mint daily notional ${notional + size:.0f} > "
            f"${MAX_NOTIONAL_PER_MINT_DAY:.0f}"
        )
    if realized <= -MAX_DAILY_LOSS_PER_MINT:
        return f"mint daily loss ${realized:.2f} hit -${MAX_DAILY_LOSS_PER_MINT:.2f} cap"
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


def _parse_ts(raw: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
