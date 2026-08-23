"""Self-built OHLCV from batched DexScreener prices.

Gecko's keyless OHLCV endpoint is the binding constraint on how many tokens can
ever become tradeable: one 429 mid-scan stops hydration, so the trade roster
collapses to whatever was fetched first. DexScreener answers 30 mints per call,
so sampling prices ourselves gives dense candles for the whole watchlist at a
couple of requests per tick — and the series keeps growing while the token stays
on the board instead of being re-fetched from scratch.

Bars built here are *sampled* mids, not trade prints: highs/lows are the extreme
samples inside the bucket, not the true intrabar range. Good enough for scalp
decisions on seconds-scale timeframes, and honest about it.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Any, Optional

from decision import pipeline_log
from decision.config import _env_float, _env_int
from decision.sources import (
    DEXSCREENER_BATCH_MAX,
    SourceError,
    fetch_dexscreener_tokens_batch,
)

ENABLED = os.getenv("PRICEFEED_ENABLED", "1").strip() == "1"
# How often we sample prices. 5s keeps a 5S bucket honest without hammering.
TICK_SEC = _env_float("PRICEFEED_TICK_SEC", 5.0)
# Samples retained per pool (~1h at 5s).
MAX_SAMPLES = _env_int("PRICEFEED_MAX_SAMPLES", 720)
# Tokens tracked at once. Each 30 costs one HTTP call per tick.
MAX_TRACKED = _env_int("PRICEFEED_MAX_TRACKED", 90)
# On a 429 we share the DexScreener budget with discovery, so retrying at the
# normal cadence just keeps both starved. Back off and let the window clear.
BACKOFF_SEC = _env_float("PRICEFEED_BACKOFF_SEC", 30.0)

# Bucket sizes we can serve, in seconds, keyed by the timeframe name decide uses.
BUCKET_SEC = {"5S": 5, "15S": 15, "30S": 30, "1": 60}


class _Track:
    __slots__ = ("mint", "pool", "symbol", "samples", "last_seen", "last_volume_h24")

    def __init__(self, mint: str, pool: str, symbol: str) -> None:
        self.mint = mint
        self.pool = pool
        self.symbol = symbol
        # (unix_ts, price_usd, bucket_volume_usd)
        self.samples: deque[tuple[float, float, float]] = deque(maxlen=MAX_SAMPLES)
        self.last_seen = 0.0
        self.last_volume_h24: Optional[float] = None


_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_tracks: dict[str, _Track] = {}  # mint → track
_stats = {
    "ticks": 0,
    "samples": 0,
    "errors": 0,
    "last_error": None,
    "last_tick_at": None,
    "running": False,
    "backoff_until": 0.0,
}


def track(*, mint: str, pool: str, symbol: str = "") -> None:
    """Start (or refresh) price sampling for a mint."""
    mint = (mint or "").strip()
    if not mint or not pool:
        return
    with _lock:
        row = _tracks.get(mint)
        if row is None:
            if len(_tracks) >= MAX_TRACKED:
                _evict_locked()
            row = _Track(mint, pool, symbol)
            _tracks[mint] = row
        row.pool = pool or row.pool
        row.symbol = symbol or row.symbol
        row.last_seen = time.time()


def _evict_locked() -> None:
    """Drop the least recently requested track so the roster can rotate."""
    if not _tracks:
        return
    oldest = min(_tracks.values(), key=lambda t: t.last_seen)
    _tracks.pop(oldest.mint, None)


def untrack(mint: str) -> None:
    with _lock:
        _tracks.pop((mint or "").strip(), None)


def sample_count(mint: str) -> int:
    with _lock:
        row = _tracks.get((mint or "").strip())
        return len(row.samples) if row else 0


def candles(mint: str, timeframe: str = "5S", *, limit: int = 300) -> list[dict[str, Any]]:
    """OHLCV bars for one mint at ``timeframe``, oldest first."""
    step = BUCKET_SEC.get(timeframe)
    if step is None:
        return []
    with _lock:
        row = _tracks.get((mint or "").strip())
        samples = list(row.samples) if row else []
    if len(samples) < 2:
        return []

    buckets: dict[int, list[tuple[float, float, float]]] = {}
    for ts, price, vol in samples:
        key = int(ts // step) * step
        buckets.setdefault(key, []).append((ts, price, vol))

    out: list[dict[str, Any]] = []
    for key in sorted(buckets):
        rows = sorted(buckets[key], key=lambda r: r[0])
        prices = [r[1] for r in rows]
        out.append(
            {
                "timestamp": float(key),
                "open": prices[0],
                "high": max(prices),
                "low": min(prices),
                "close": prices[-1],
                "volume": round(sum(r[2] for r in rows), 4),
            }
        )
    return out[-limit:] if limit > 0 else out


def timeframes(mint: str, *, limit: int = 300) -> dict[str, list[dict[str, Any]]]:
    """All servable timeframes for a mint, skipping ones with too few bars."""
    out: dict[str, list[dict[str, Any]]] = {}
    for tf in BUCKET_SEC:
        rows = candles(mint, tf, limit=limit)
        if len(rows) >= 2:
            out[tf] = rows
    return out


def status() -> dict[str, Any]:
    with _lock:
        tracked = [
            {"mint": t.mint, "symbol": t.symbol, "samples": len(t.samples)}
            for t in _tracks.values()
        ]
        stats = dict(_stats)
    tracked.sort(key=lambda r: r["samples"], reverse=True)
    return {
        **stats,
        "tracked_n": len(tracked),
        "tracked": tracked[:40],
        "limits": {
            "tick_sec": TICK_SEC,
            "max_samples": MAX_SAMPLES,
            "max_tracked": MAX_TRACKED,
            "batch_max": DEXSCREENER_BATCH_MAX,
        },
    }


def reset() -> dict[str, Any]:
    with _lock:
        _tracks.clear()
        _stats.update({"ticks": 0, "samples": 0, "errors": 0, "last_error": None})
    return status()


def sample_once() -> dict[str, Any]:
    """One sampling pass over every tracked mint."""
    now = time.time()
    with _lock:
        if now < float(_stats.get("backoff_until") or 0.0):
            return {"status": "backoff", "sampled": 0, "batches": 0}
        mints = [t.mint for t in _tracks.values()]
    if not mints:
        return {"status": "ok", "sampled": 0, "batches": 0}

    sampled = 0
    batches = 0
    for start in range(0, len(mints), DEXSCREENER_BATCH_MAX):
        chunk = mints[start : start + DEXSCREENER_BATCH_MAX]
        try:
            found = fetch_dexscreener_tokens_batch(chunk)
        except SourceError as exc:
            throttled = "429" in str(exc) or "Too Many" in str(exc)
            with _lock:
                _stats["errors"] = int(_stats["errors"]) + 1
                _stats["last_error"] = str(exc)
                if throttled:
                    _stats["backoff_until"] = time.time() + BACKOFF_SEC
            pipeline_log.emit(
                "pricefeed",
                "throttled" if throttled else "batch_error",
                level="warning",
                reason=str(exc),
                n=len(chunk),
                backoff_sec=BACKOFF_SEC if throttled else None,
            )
            if throttled:
                break
            continue
        batches += 1
        for mint, pairs in found.items():
            best = _deepest_pair(pairs, mint)
            if best is None:
                continue
            price = _price_of(best, mint)
            if price is None or price <= 0:
                continue
            vol_h24 = _float_or_none((best.get("volume") or {}).get("h24"))
            with _lock:
                row = _tracks.get(mint)
                if row is None:
                    continue
                bucket_vol = 0.0
                if vol_h24 is not None and row.last_volume_h24 is not None:
                    bucket_vol = max(0.0, vol_h24 - row.last_volume_h24)
                if vol_h24 is not None:
                    row.last_volume_h24 = vol_h24
                row.samples.append((now, price, bucket_vol))
                sampled += 1

    with _lock:
        _stats["ticks"] = int(_stats["ticks"]) + 1
        _stats["samples"] = int(_stats["samples"]) + sampled
        _stats["last_tick_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {"status": "ok", "sampled": sampled, "batches": batches, "tracked": len(mints)}


def _deepest_pair(pairs: list[dict[str, Any]], mint: str) -> Optional[dict[str, Any]]:
    """Deepest pool where ``mint`` is the base token.

    ``priceUsd`` always describes the base side, so pools that list the mint as
    quote would hand back the wrong asset's price.
    """
    best: Optional[dict[str, Any]] = None
    best_liq = -1.0
    for pair in pairs:
        if ((pair.get("baseToken") or {}).get("address") or "") != mint:
            continue
        liq = _float_or_none((pair.get("liquidity") or {}).get("usd")) or 0.0
        if liq > best_liq:
            best_liq = liq
            best = pair
    return best


def _price_of(pair: dict[str, Any], mint: str) -> Optional[float]:
    if ((pair.get("baseToken") or {}).get("address") or "") != mint:
        return None
    return _float_or_none(pair.get("priceUsd"))


def _float_or_none(raw: Any) -> Optional[float]:
    try:
        if raw is None:
            return None
        return float(raw)
    except (TypeError, ValueError):
        return None


def start(*, interval_s: Optional[float] = None) -> dict[str, Any]:
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return status()
        _stop.clear()
        _stats["running"] = True
        period = float(interval_s if interval_s is not None else TICK_SEC)
        _thread = threading.Thread(target=_run, args=(period,), name="pricefeed", daemon=True)
        _thread.start()
    pipeline_log.emit("pricefeed", "start", interval_s=period)
    return status()


def stop() -> dict[str, Any]:
    _stop.set()
    with _lock:
        _stats["running"] = False
    pipeline_log.emit("pricefeed", "stop")
    return status()


def _run(interval_s: float) -> None:
    while not _stop.is_set():
        try:
            sample_once()
        except Exception as exc:  # noqa: BLE001
            with _lock:
                _stats["errors"] = int(_stats["errors"]) + 1
                _stats["last_error"] = str(exc)
        _stop.wait(interval_s)
    with _lock:
        _stats["running"] = False
