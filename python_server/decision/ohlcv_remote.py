"""Headless OHLCV fetch for discovery (GeckoTerminal → live token buffer shape)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional

from decision.sources import SourceError, fetch_gecko_ohlcv

# Cap matches server LIVE_MAX_BARS_PER_TF intent without importing server.
MAX_BARS = 2_000


def gecko_rows_to_candles(rows: list[list[float]]) -> list[dict[str, Any]]:
    """Convert Gecko ``[ts_sec, o, h, l, c, vol]`` (any order) to live candles.

    Timestamps stay in Unix seconds — same unit TradingView / Axiom use.
    """
    candles: list[dict[str, Any]] = []
    for row in rows:
        if len(row) < 5:
            continue
        ts = float(row[0])
        # Defensive: some feeds emit ms.
        if ts > 1e12:
            ts = ts / 1000.0
        candles.append(
            {
                "timestamp": ts,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]) if len(row) > 5 else 0.0,
            }
        )
    candles.sort(key=lambda c: c["timestamp"])
    if len(candles) > MAX_BARS:
        candles = candles[-MAX_BARS:]
    return candles


def fetch_pool_ohlcv(
    pool_address: str,
    *,
    aggregate: int = 1,
    limit: int = 300,
) -> list[dict[str, Any]]:
    """1m (or aggregated) USD candles for a Solana pool address."""
    raw = fetch_gecko_ohlcv(
        pool_address,
        timeframe="minute",
        aggregate=aggregate,
        limit=limit,
        currency="usd",
    )
    candles = gecko_rows_to_candles(raw)
    if len(candles) < 2:
        raise SourceError("gecko ohlcv: fewer than 2 bars after normalize")
    return candles


def stub_candles_from_price(
    price: float,
    *,
    bars: int = 32,
    step_sec: int = 60,
) -> list[dict[str, Any]]:
    """Synthetic flat candles for observe-only dashboard rows (no Gecko spend)."""
    now = int(time.time())
    px = float(price)
    out: list[dict[str, Any]] = []
    for i in range(bars):
        ts = now - (bars - 1 - i) * step_sec
        out.append(
            {
                "timestamp": ts,
                "open": px,
                "high": px,
                "low": px,
                "close": px,
                "volume": 0.0,
            }
        )
    return out


def build_live_token(
    *,
    name: str,
    mint: Optional[str],
    pool_address: str,
    candles: list[dict[str, Any]],
    source: str = "geckoterminal",
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Shape matching extension ingest so swarm/decide need no special case."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    token: dict[str, Any] = {
        "payload_id": f"discover:{pool_address[:12]}",
        "name": name or "Unknown",
        "mint": mint,
        "pool": pool_address,
        "timeframes": {"1": list(candles)},
        "stats": [],
        "updated": now,
        "live": True,
        "source": source,
        "discover": True,
    }
    if extra:
        token.update(extra)
    return token
