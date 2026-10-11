"""Synchronous rug confirmation for the paper stop-loss path.

A mark that gaps far past the stop is one of two things:

  * a **ghost wick** — a single bad venue print (usually DexScreener mid) with
    no on-chain reality, or
  * a **real rug** — genuine selling that already happened on-chain.

The paper book must not book a full loss on a ghost wick, and must not mask a
real rug behind ``PAPER_MAX_LOSS_PCT``. The rule: a gapped stop-out books the
*full mark* only on **positive confirmation**, and caps otherwise (fail-closed).

Confirmation is a second, independent signal that the dump actually happened:

  1. **On-chain sell flow** (Helius): ≥ ``MIN_SELLS`` sell-side token transfers
     of the mint in the last ``WINDOW_SEC`` seconds. Ground truth — a rug means
     real people (or the dev) sent tokens back to the curve/pool.
  2. **Second venue agreement** (GeckoTerminal 1m): the recent low is within
     ``TOL_PCT`` of the gapped mark, i.e. an independent venue printed the same
     collapse.

Either signal confirms; the absence of both returns ``confirmed=False`` and the
caller caps. Any fetch error or missing data degrades to "unconfirmed" — never
book a full loss on evidence we could not obtain.

Self-contained on purpose: it imports only ``sources`` + ``config`` + helpers,
so it can be called from ``paper`` without creating an import cycle.
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

from decision.config import _env_float, _env_int
from decision.ohlcv_remote import gecko_rows_to_candles
from decision.sources import SourceError, fetch_gecko_ohlcv, fetch_helius_transactions

ENABLED = os.getenv("RUG_CONFIRM_ENABLED", "1").strip() == "1"
# How far back to look for on-chain sells / a confirming Gecko low.
WINDOW_SEC = _env_float("RUG_CONFIRM_WINDOW_SEC", 120.0)
# How many sell-side transfers count as a real dump.
MIN_SELLS = _env_int("RUG_CONFIRM_MIN_SELLS", 1)
# Gecko low within this % of the gapped mark counts as agreeing.
TOL_PCT = _env_float("RUG_CONFIRM_TOL_PCT", 5.0)


def _recent_sells(txs: list[dict[str, Any]], mint: str, cutoff: float) -> int:
    """Count sell-side token transfers of ``mint`` at or after ``cutoff``."""
    mint_l = mint.lower()
    sells = 0
    for tx in txs:
        try:
            ts = float(tx.get("timestamp") or 0.0)
        except (TypeError, ValueError):
            continue
        if ts < cutoff:
            continue
        for transfer in tx.get("tokenTransfers") or []:
            if not isinstance(transfer, dict):
                continue
            if (transfer.get("mint") or "").strip().lower() != mint_l:
                continue
            # Token leaving a user account = sell-side flow.
            if (transfer.get("fromUserAccount") or "").strip():
                sells += 1
    return sells


def _recent_low(candles: list[dict[str, Any]], cutoff: float) -> Optional[float]:
    lows = [
        float(c["low"])
        for c in candles
        if c.get("low") is not None and float(c.get("timestamp") or 0.0) >= cutoff
    ]
    return min(lows) if lows else None


def confirmed_rug(
    *,
    mint: str,
    pool: Optional[str],
    gapped_price: Optional[float],
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Return ``{"confirmed", "evidence", "sells", "gecko_low"}``.

    Fail-closed: anything that cannot be positively confirmed returns
    ``confirmed=False`` so the caller applies the ``PAPER_MAX_LOSS_PCT`` cap.
    """
    result: dict[str, Any] = {
        "confirmed": False,
        "evidence": "no signal",
        "sells": 0,
        "gecko_low": None,
    }
    if not ENABLED:
        result["evidence"] = "disabled"
        return result
    now = time.time() if now is None else now
    mint = (mint or "").strip()
    if not mint:
        result["evidence"] = "no mint"
        return result
    cutoff = now - WINDOW_SEC

    # 1. On-chain sell flow (ground truth).
    sells = 0
    try:
        txs = fetch_helius_transactions(mint, limit=20)
        sells = _recent_sells(txs, mint, cutoff)
    except SourceError:
        sells = 0
    result["sells"] = sells
    if sells >= MIN_SELLS:
        result["confirmed"] = True
        result["evidence"] = f"on-chain sells {sells} in {WINDOW_SEC:.0f}s"
        return result

    # 2. Independent venue (Gecko 1m) printing the same collapse.
    if gapped_price is not None and gapped_price > 0 and pool:
        try:
            rows = fetch_gecko_ohlcv(pool, timeframe="minute", aggregate=1, limit=10)
            candles = gecko_rows_to_candles(rows)
            low = _recent_low(candles, cutoff)
            result["gecko_low"] = low
            if low is not None and low <= gapped_price * (1.0 + TOL_PCT / 100.0):
                result["confirmed"] = True
                result["evidence"] = (
                    f"gecko low {low:.8g} agrees with mark {gapped_price:.8g}"
                )
                return result
        except SourceError:
            pass

    result["evidence"] = (
        f"unconfirmed (sells={sells}, gecko_low={result['gecko_low']})"
    )
    return result
