"""Paper fill vs venue/on-chain tape. Observation only — never sizes a trade.

After each paper close we wait a few seconds, then re-read Dex / Gecko 1m
candles / optional Helius swaps for that mint. The question is whether the
booked fill could have existed on the real tape, and whether a live stop
would have been worse than the paper barrier.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

from decision import costs
from decision import pipeline_log
from decision import store as decision_store
from decision.config import _env_float, _env_int
from decision.ohlcv_remote import gecko_rows_to_candles
from decision.sources import (
    SourceError,
    fetch_dexscreener_pairs,
    fetch_gecko_ohlcv,
    fetch_helius_transactions,
)

ENABLED = os.getenv("FILL_AUDIT_ENABLED", "1").strip() == "1"
DELAY_SEC = _env_float("FILL_AUDIT_DELAY_SEC", 20.0)
HELIUS = os.getenv("FILL_AUDIT_HELIUS", "1").strip() == "1"
HELIUS_LIMIT = _env_int("FILL_AUDIT_HELIUS_LIMIT", 20)

_stop = threading.Event()
_thread: Optional[threading.Thread] = None
_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=256)
_recent: deque[dict[str, Any]] = deque(maxlen=200)
_lock = threading.Lock()
_stats = {
    "running": False,
    "queued": 0,
    "audited": 0,
    "errors": 0,
    "last_error": None,
}


def start() -> dict[str, Any]:
    global _thread
    if not ENABLED:
        return status()
    with _lock:
        if _thread and _thread.is_alive():
            return status()
        _stop.clear()
        _stats["running"] = True
        _stats["last_error"] = None
        _thread = threading.Thread(target=_run, name="fill-audit", daemon=True)
        _thread.start()
    pipeline_log.emit("fill_audit", "start")
    return status()


def stop() -> dict[str, Any]:
    _stop.set()
    with _lock:
        _stats["running"] = False
    return status()


def observe_close(position: dict[str, Any]) -> None:
    """Queue a closed paper lot. Never raises into the fill path."""
    if not ENABLED:
        return
    mint = (position.get("mint") or "").strip()
    if not mint:
        return
    try:
        _queue.put_nowait({"due_at": time.time() + DELAY_SEC, "position": dict(position)})
    except queue.Full:
        pipeline_log.emit("fill_audit", "queue_full", level="warning", mint=mint)


def status() -> dict[str, Any]:
    rows = list(_recent)
    n = len(rows)
    by: dict[str, int] = {}
    paper_pnls: list[float] = []
    chain_last: list[float] = []
    chain_low: list[float] = []
    fast = 0
    for row in rows:
        v = str(row.get("verdict") or "unknown")
        by[v] = by.get(v, 0) + 1
        p = row.get("paper_pnl_usd")
        if p is not None:
            paper_pnls.append(float(p))
        c = row.get("chain_last_pnl_usd")
        if c is not None:
            chain_last.append(float(c))
        lo = row.get("chain_low_pnl_usd")
        if lo is not None:
            chain_low.append(float(lo))
        if row.get("fast_enough"):
            fast += 1
    return {
        "enabled": ENABLED,
        "running": _stats["running"],
        "queued": _queue.qsize(),
        "audited": _stats["audited"],
        "errors": _stats["errors"],
        "last_error": _stats["last_error"],
        "n": n,
        "by_verdict": by,
        "pct_fast_enough": round(100.0 * fast / n, 1) if n else None,
        "mean_paper_pnl_usd": round(sum(paper_pnls) / len(paper_pnls), 4) if paper_pnls else None,
        "mean_chain_last_pnl_usd": round(sum(chain_last) / len(chain_last), 4) if chain_last else None,
        "mean_chain_low_pnl_usd": round(sum(chain_low) / len(chain_low), 4) if chain_low else None,
        "recent": rows[-12:],
    }


def compare_close(
    position: dict[str, Any],
    *,
    candles: list[dict[str, Any]],
    dex: Optional[dict[str, Any]] = None,
    chain_swaps: Optional[list[dict[str, Any]]] = None,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Pure compare: paper fill vs 1m window / Dex mid / on-chain swap times."""
    now = time.time() if now is None else now
    qty = float(position.get("qty") or 0.0)
    size = float(position.get("size_usd") or 0.0)
    entry = float(position.get("entry_price") or 0.0)
    paper_exit = position.get("exit_price")
    paper_pnl = position.get("realized_pnl_usd")
    t0 = _ts(position.get("opened_at")) or now
    t1 = _ts(position.get("closed_at")) or now
    cost = (costs.EXIT_FEE_PCT + costs.EXIT_SLIP_PCT) / 100.0
    win = _window(candles, t0, t1)

    chain_open = win.get("open")
    chain_high = win.get("high")
    chain_low = win.get("low")
    chain_last = win.get("close")
    if chain_last is None and dex and dex.get("price_usd"):
        chain_last = float(dex["price_usd"])

    def at(price: Optional[float]) -> Optional[float]:
        if price is None or price <= 0 or qty <= 0 or size <= 0:
            return None
        return round(qty * price - size - size * cost, 4)

    first_swap_lag = None
    swaps_in_hold = 0
    if chain_swaps:
        for ev in chain_swaps:
            ts = float(ev.get("ts") or 0.0)
            if t0 <= ts <= t1 + 5.0:
                swaps_in_hold += 1
                if first_swap_lag is None:
                    first_swap_lag = round(ts - t0, 2)

    ghost = bool(entry > 0 and win.get("high") is not None and win["high"] < entry * 0.92)
    first_min_close = win.get("first_close")
    dumped_fast = bool(
        entry > 0
        and first_min_close is not None
        and first_min_close < entry * 0.92
    )
    too_slow = bool(
        dumped_fast and first_swap_lag is not None and first_swap_lag > 6.0
    )

    paper_pnl_f = None if paper_pnl is None else float(paper_pnl)
    chain_low_pnl = at(chain_low)
    chain_last_pnl = at(chain_last)
    chain_high_pnl = at(chain_high)
    reason = str(position.get("close_reason") or "")
    barrier = "barrier" in reason
    barrier_kinder = bool(
        barrier
        and paper_pnl_f is not None
        and chain_low_pnl is not None
        and chain_low_pnl < paper_pnl_f - 0.25
    )

    verdict = "no_tape"
    if ghost:
        verdict = "ghost_entry"
    elif too_slow:
        verdict = "too_slow"
    elif barrier_kinder:
        verdict = "barrier_kinder"
    elif paper_pnl_f is not None and chain_last_pnl is not None:
        tol = max(1.0, 0.02 * size)
        if abs(paper_pnl_f - chain_last_pnl) <= tol:
            verdict = "aligned"
        elif paper_pnl_f > chain_last_pnl:
            verdict = "paper_better"
        else:
            verdict = "paper_worse"
    elif chain_last_pnl is not None:
        verdict = "chain_only"

    fast_enough = verdict not in {"ghost_entry", "too_slow", "no_tape"}
    return {
        "event": "compare",
        "position_id": position.get("id"),
        "mint": position.get("mint"),
        "symbol": position.get("name"),
        "pool": position.get("address"),
        "entry_reason": position.get("entry_reason"),
        "close_reason": reason,
        "held_sec": round(max(t1 - t0, 0.0), 1),
        "size_usd": size,
        "paper_entry": entry,
        "paper_exit": None if paper_exit is None else float(paper_exit),
        "paper_pnl_usd": paper_pnl_f,
        "chain_open": chain_open,
        "chain_high": chain_high,
        "chain_low": chain_low,
        "chain_last": chain_last,
        "chain_high_pnl_usd": chain_high_pnl,
        "chain_low_pnl_usd": chain_low_pnl,
        "chain_last_pnl_usd": chain_last_pnl,
        "dex_price_usd": None if not dex else dex.get("price_usd"),
        "dex_m5_pct": None if not dex else dex.get("m5_pct"),
        "dex_liq_usd": None if not dex else dex.get("liq_usd"),
        "bars": win.get("n", 0),
        "swaps_in_hold": swaps_in_hold,
        "first_swap_lag_sec": first_swap_lag,
        "ghost_entry": ghost,
        "barrier_kinder": barrier_kinder,
        "fast_enough": fast_enough,
        "verdict": verdict,
        "audited_at": datetime.now(timezone.utc).isoformat(),
    }


def _run() -> None:
    while not _stop.is_set():
        try:
            item = _queue.get(timeout=0.5)
        except queue.Empty:
            continue
        wait = float(item.get("due_at") or 0.0) - time.time()
        if wait > 0:
            _stop.wait(wait)
            if _stop.is_set():
                break
        try:
            record = _audit(item["position"])
            if record:
                with _lock:
                    _recent.append(record)
                    _stats["audited"] += 1
                try:
                    decision_store.append("fill_audit", record)
                except OSError:
                    pass
                pipeline_log.emit(
                    "fill_audit",
                    "compare",
                    mint=record.get("mint"),
                    symbol=record.get("symbol"),
                    verdict=record.get("verdict"),
                    paper_pnl_usd=record.get("paper_pnl_usd"),
                    chain_last_pnl_usd=record.get("chain_last_pnl_usd"),
                    chain_low_pnl_usd=record.get("chain_low_pnl_usd"),
                    fast_enough=record.get("fast_enough"),
                )
        except Exception as exc:  # noqa: BLE001
            with _lock:
                _stats["errors"] += 1
                _stats["last_error"] = type(exc).__name__
            pipeline_log.emit(
                "fill_audit", "error", level="warning", reason=type(exc).__name__
            )
    with _lock:
        _stats["running"] = False


def _audit(position: dict[str, Any]) -> Optional[dict[str, Any]]:
    mint = (position.get("mint") or "").strip()
    pool = (position.get("address") or "").strip()
    candles: list[dict[str, Any]] = []
    if pool:
        try:
            raw = fetch_gecko_ohlcv(pool, timeframe="minute", aggregate=1, limit=40)
            candles = gecko_rows_to_candles(raw)
        except SourceError:
            candles = []
    dex = _dex_snapshot(mint, pool)
    swaps: list[dict[str, Any]] = []
    if HELIUS and mint:
        try:
            txs = fetch_helius_transactions(mint, limit=HELIUS_LIMIT)
            swaps = _swaps_for_mint(txs, mint)
        except SourceError:
            swaps = []
    return compare_close(position, candles=candles, dex=dex, chain_swaps=swaps)


def _dex_snapshot(mint: str, pool: str) -> Optional[dict[str, Any]]:
    if not mint:
        return None
    try:
        pairs = fetch_dexscreener_pairs(mint)
    except SourceError:
        return None
    chosen = None
    pool_l = pool.lower()
    ranked = [p for p in pairs if isinstance(p, dict)]
    if pool_l:
        for pair in ranked:
            if str(pair.get("pairAddress") or "").lower() == pool_l:
                chosen = pair
                break
    if chosen is None and ranked:
        def liq(row: dict[str, Any]) -> float:
            try:
                return float((row.get("liquidity") or {}).get("usd") or 0.0)
            except (TypeError, ValueError):
                return 0.0
        chosen = max(ranked, key=liq)
    if not chosen:
        return None
    try:
        px = float(chosen.get("priceUsd") or 0.0) or None
    except (TypeError, ValueError):
        px = None
    chg = chosen.get("priceChange") or {}
    liq = chosen.get("liquidity") or {}
    try:
        m5 = float(chg.get("m5")) if chg.get("m5") is not None else None
    except (TypeError, ValueError):
        m5 = None
    try:
        liq_usd = float(liq.get("usd") or 0.0) or None
    except (TypeError, ValueError):
        liq_usd = None
    return {"price_usd": px, "m5_pct": m5, "liq_usd": liq_usd}


def _swaps_for_mint(txs: list[dict[str, Any]], mint: str) -> list[dict[str, Any]]:
    mint_l = mint.lower()
    out: list[dict[str, Any]] = []
    for tx in txs:
        try:
            ts = float(tx.get("timestamp") or 0.0)
        except (TypeError, ValueError):
            continue
        hit = False
        for transfer in tx.get("tokenTransfers") or []:
            if not isinstance(transfer, dict):
                continue
            if (transfer.get("mint") or "").strip().lower() == mint_l:
                hit = True
                break
        if hit:
            out.append({"ts": ts, "signature": tx.get("signature")})
    out.sort(key=lambda r: r["ts"])
    return out


def _window(candles: list[dict[str, Any]], t0: float, t1: float) -> dict[str, Any]:
    bars = [
        c
        for c in candles
        if isinstance(c, dict)
        and t0 - 15.0 <= float(c.get("timestamp") or 0.0) <= t1 + 90.0
    ]
    if not bars:
        return {"n": 0}
    highs = [float(c["high"]) for c in bars if c.get("high")]
    lows = [float(c["low"]) for c in bars if c.get("low")]
    first = bars[0]
    last = bars[-1]
    return {
        "n": len(bars),
        "open": float(first.get("open") or 0.0) or None,
        "close": float(last.get("close") or 0.0) or None,
        "high": max(highs) if highs else None,
        "low": min(lows) if lows else None,
        "first_close": float(first.get("close") or 0.0) or None,
    }


def _ts(raw: Any) -> Optional[float]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None
