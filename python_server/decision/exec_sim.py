"""On-chain latency fill simulation for paper trading.

Live Solana fills do not happen at the mid you saw when the bot decided.
You sign → submit → land in a later slot; the book can move against you.
This module records venue mids as they arrive and, after a short delay,
fills at the *worst* price in that window:

  * buy  → max(mid in [signal, signal+latency])  (you pay more)
  * sell → min(mid in [signal, signal+latency])  (you receive less)

Uses the same Dex / pricefeed marks the desk already polls — no synthetic
randomness. If the buffer is empty at resolve time, the optional fallback
poll (current Dex mid) is included, still taking the worse of signal vs now.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

from decision.config import _env_float, _env_int
from decision import pipeline_log

ENABLED = os.getenv("EXEC_SIM_ENABLED", "0").strip() == "1"
# ~a few Solana slots + send/land. Not multi-second "lag trading".
LATENCY_SEC = _env_float("EXEC_SIM_LATENCY_SEC", 1.5)
# Keep enough tape to cover latency + a little slack.
BUFFER_SEC = _env_float("EXEC_SIM_BUFFER_SEC", 45.0)
MAX_MARKS_PER_MINT = _env_int("EXEC_SIM_MAX_MARKS", 256)
# Always take the adverse extreme in the window (the live-like default).
WORST_FILL = os.getenv("EXEC_SIM_WORST_FILL", "1").strip() == "1"
# Flat adverse-selection adder applied on top of the worst mid, in basis points
# (5 bps = 0.05%). Models the residual selection beyond the observed mid window
# — sandwich/priority-race cost that scales poorly with thin books. This is a
# lower-bound proxy, not a liquidity model: live MEV/JITO losses are typically
# larger. 0 = off (worst-mid only).
ADVERSE_BPS = _env_float("EXEC_SIM_ADVERSE_BPS", 5.0)

_lock = threading.Lock()
_marks: dict[str, deque[tuple[float, float]]] = defaultdict(
    lambda: deque(maxlen=MAX_MARKS_PER_MINT)
)
_pending_buys: list["PendingBuy"] = []


@dataclass
class PendingBuy:
    id: str
    lane: str
    address: str
    mint: str
    name: str
    signal_price: float
    signal_at: float
    fill_at: float
    size_usd: float
    strategy: str
    reserved_usd: float
    max_hold_sec: Optional[float] = None
    extra: dict[str, Any] = field(default_factory=dict)
    target_profit_usd: Optional[float] = None
    bank_at_target: bool = False
    stop_loss_usd: Optional[float] = None
    take_profit_pct: float = 0.0
    dead_after_sec: Optional[float] = None
    abs_hold_sec: Optional[float] = None


def status() -> dict[str, Any]:
    with _lock:
        return {
            "enabled": ENABLED,
            "latency_sec": LATENCY_SEC,
            "worst_fill": WORST_FILL,
            "buffer_sec": BUFFER_SEC,
            "pending_buys": len(_pending_buys),
            "mints_tracked": len(_marks),
            "marks": sum(len(v) for v in _marks.values()),
        }


def reset() -> None:
    with _lock:
        _marks.clear()
        _pending_buys.clear()


def observe(mint: str, price: float, *, ts: Optional[float] = None) -> None:
    """Record a venue mid for later worst-fill resolution."""
    mint = (mint or "").strip()
    if not mint or price <= 0:
        return
    now = time.time() if ts is None else float(ts)
    cutoff = now - BUFFER_SEC
    with _lock:
        buf = _marks[mint]
        buf.append((now, float(price)))
        while buf and buf[0][0] < cutoff:
            buf.popleft()


def window_prices(
    mint: str, t0: float, t1: float, *, include: Optional[list[float]] = None
) -> list[float]:
    mint = (mint or "").strip()
    out: list[float] = []
    with _lock:
        for ts, px in _marks.get(mint, ()):
            if t0 - 1e-6 <= ts <= t1 + 1e-6 and px > 0:
                out.append(float(px))
    if include:
        out.extend(float(p) for p in include if p and p > 0)
    return out


def worst_buy_price(signal_price: float, prices: list[float]) -> float:
    xs = [p for p in prices if p > 0]
    if signal_price > 0:
        xs.append(float(signal_price))
    if not xs:
        return float(signal_price)
    return max(xs) if WORST_FILL else xs[-1]


def worst_sell_price(signal_price: float, prices: list[float]) -> float:
    xs = [p for p in prices if p > 0]
    if signal_price > 0:
        xs.append(float(signal_price))
    if not xs:
        return float(signal_price)
    return min(xs) if WORST_FILL else xs[-1]


def queue_buy(**kwargs: Any) -> PendingBuy:
    """Reserve a buy that will fill after ``LATENCY_SEC`` at the worst mid."""
    now = time.time()
    signal = float(kwargs["signal_price"])
    observe(str(kwargs["mint"]), signal, ts=now)
    row = PendingBuy(
        id=str(uuid.uuid4())[:8],
        lane=str(kwargs.get("lane") or "main"),
        address=str(kwargs["address"]),
        mint=str(kwargs["mint"]),
        name=str(kwargs.get("name") or ""),
        signal_price=signal,
        signal_at=now,
        fill_at=now + LATENCY_SEC,
        size_usd=float(kwargs["size_usd"]),
        strategy=str(kwargs["strategy"]),
        reserved_usd=float(kwargs["reserved_usd"]),
        max_hold_sec=kwargs.get("max_hold_sec"),
        extra=dict(kwargs.get("extra") or {}),
        target_profit_usd=kwargs.get("target_profit_usd"),
        bank_at_target=bool(kwargs.get("bank_at_target") or False),
        stop_loss_usd=kwargs.get("stop_loss_usd"),
        take_profit_pct=float(kwargs.get("take_profit_pct") or 0.0),
        dead_after_sec=kwargs.get("dead_after_sec"),
        abs_hold_sec=kwargs.get("abs_hold_sec"),
    )
    with _lock:
        _pending_buys.append(row)
    pipeline_log.emit(
        "exec_sim",
        "buy_queued",
        mint=row.mint,
        symbol=row.name,
        signal_price=row.signal_price,
        fill_at_in_sec=LATENCY_SEC,
        size_usd=row.size_usd,
        strategy=row.strategy,
    )
    return row


def due_buys(*, now: Optional[float] = None) -> list[PendingBuy]:
    now = time.time() if now is None else now
    with _lock:
        due = [b for b in _pending_buys if b.fill_at <= now]
        keep = [b for b in _pending_buys if b.fill_at > now]
        _pending_buys[:] = keep
        return due


def pending_buys_snapshot(*, now: Optional[float] = None) -> list[PendingBuy]:
    """All queued buys (due and waiting), without removing them."""
    now = time.time() if now is None else now
    with _lock:
        return list(_pending_buys)


def waiting_buys(*, now: Optional[float] = None) -> list[PendingBuy]:
    """Buys still inside their latency window (not yet due)."""
    now = time.time() if now is None else now
    with _lock:
        return [b for b in _pending_buys if b.fill_at > now]


def resolve_buy(
    pending: PendingBuy, *, fallback_price: Optional[float] = None
) -> dict[str, Any]:
    """Compute the adverse buy fill from the mark buffer.

    The adverse window is exactly ``[signal, signal+LATENCY]`` — the submit→land
    interval. Resolving late must not widen that window into minutes of tape.
    ``fallback_price`` (Dex poll at resolve) is one extra print inside the land.
    """
    t1 = pending.signal_at + LATENCY_SEC
    prices = window_prices(
        pending.mint,
        pending.signal_at,
        t1,
        include=[pending.signal_price]
        + ([fallback_price] if fallback_price else []),
    )
    raw = worst_buy_price(pending.signal_price, prices)
    adverse_mult = 1.0 + ADVERSE_BPS / 10_000.0
    raw = raw * adverse_mult
    return {
        "pending": pending,
        "signal_price": pending.signal_price,
        "fill_price_raw": raw,
        "marks_n": len(prices),
        "adverse_pct": round((raw / pending.signal_price - 1.0) * 100.0, 4)
        if pending.signal_price > 0
        else 0.0,
        "latency_sec": round(LATENCY_SEC, 3),
        "adverse_bps": round(ADVERSE_BPS, 2),
    }


def resolve_sell(
    *,
    mint: str,
    signal_price: float,
    signal_at: float,
    fallback_price: Optional[float] = None,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Adverse sell fill over exactly ``[signal, signal+LATENCY]``."""
    _ = now  # kept for call-site compatibility
    t1 = signal_at + LATENCY_SEC
    prices = window_prices(
        mint,
        signal_at,
        t1,
        include=[signal_price] + ([fallback_price] if fallback_price else []),
    )
    raw = worst_sell_price(signal_price, prices)
    adverse_mult = 1.0 - ADVERSE_BPS / 10_000.0
    raw = raw * adverse_mult
    return {
        "signal_price": signal_price,
        "fill_price_raw": raw,
        "marks_n": len(prices),
        "adverse_pct": round((1.0 - raw / signal_price) * 100.0, 4)
        if signal_price > 0
        else 0.0,
        "latency_sec": round(LATENCY_SEC, 3),
        "adverse_bps": round(ADVERSE_BPS, 2),
    }


def pending_buy_as_dict(row: PendingBuy) -> dict[str, Any]:
    return asdict(row)
