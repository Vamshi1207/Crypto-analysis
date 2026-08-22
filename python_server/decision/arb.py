"""Parallel paper channel: cross-pool Solana quote arbitrage (no live swaps).

This is the useful slice of the viral “ms arb bot” idea — detect when the *same*
mint prints different USD prices on two DEXes/pools, size the gap against real
round-trip costs, and book a paper atomic round-trip when net edge clears.

It does **not** do mempool racing or Jito bundles yet. Quotes come from
DexScreener (and optional Jupiter cross-check). True millisecond racing needs a
paid Geyser/gRPC or Jito path; this channel proves whether gaps survive *our*
cost model before any live tips are spent.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from decision import costs
from decision import pipeline_log
from decision import store as decision_store
from decision.config import WRAPPED_SOL_MINT, _env_float, _env_int
from decision.sources import (
    NoRouteError,
    SourceError,
    fetch_dexscreener_pairs,
    fetch_mint_account,
    fetch_sell_quote,
)

ARB_ENABLED = os.getenv("ARB_ENABLED", "0").strip() == "1"
INTERVAL_SEC = _env_float("ARB_INTERVAL_SEC", 20.0)
MIN_EDGE_PCT = _env_float("ARB_MIN_EDGE_PCT", 0.8)
MIN_POOL_LIQ_USD = _env_float("ARB_MIN_POOL_LIQ_USD", 8_000.0)
SIZE_USD = _env_float("ARB_SIZE_USD", 40.0)
MAX_PER_TICK = _env_int("ARB_MAX_PER_TICK", 3)
# Arb legs are two swaps; reuse the shared cost model (fees + slip + tip).
# Optional override if you want a tighter assumed slip for liquid pairs.
ARB_SLIP_PCT = _env_float("ARB_SLIP_PCT", 0.0)  # 0 = use costs.DEFAULT_ENTRY_SLIP_PCT
# Paper fills only count when Jupiter can route the size (default on).
REQUIRE_JUPITER = os.getenv("ARB_REQUIRE_JUPITER", "1").strip() == "1"
MAX_JUPITER_IMPACT_PCT = _env_float("ARB_MAX_JUPITER_IMPACT_PCT", 5.0)


@dataclass
class PoolQuote:
    pair: str
    dex: str
    price_usd: float
    liquidity_usd: float
    symbol: str = ""


@dataclass
class ArbOpportunity:
    mint: str
    symbol: str
    buy: PoolQuote
    sell: PoolQuote
    gross_pct: float
    cost_pct: float
    net_pct: float
    size_usd: float
    expected_pnl_usd: float


@dataclass
class ArbState:
    running: bool = False
    ticks: int = 0
    last_tick_at: Optional[str] = None
    last_error: Optional[str] = None
    opportunities_seen: int = 0
    paper_fills: int = 0
    realized_pnl_usd: float = 0.0
    last_opps: list[dict[str, Any]] = field(default_factory=list)


_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_state = ArbState()
_get_tokens: Optional[Callable[[], dict[str, Any]]] = None


def configure(*, get_tokens: Callable[[], dict[str, Any]]) -> None:
    global _get_tokens
    _get_tokens = get_tokens


def reset_counters() -> dict[str, Any]:
    """Zero session stats (keeps the loop running if already started)."""
    with _lock:
        _state.ticks = 0
        _state.last_tick_at = None
        _state.last_error = None
        _state.opportunities_seen = 0
        _state.paper_fills = 0
        _state.realized_pnl_usd = 0.0
        _state.last_opps = []
    pipeline_log.emit("arb", "reset", level="warning")
    return status()


def status() -> dict[str, Any]:
    with _lock:
        return {
            "running": _state.running,
            "ticks": _state.ticks,
            "last_tick_at": _state.last_tick_at,
            "last_error": _state.last_error,
            "opportunities_seen": _state.opportunities_seen,
            "paper_fills": _state.paper_fills,
            "realized_pnl_usd": round(_state.realized_pnl_usd, 4),
            "last_opps": list(_state.last_opps)[:20],
            "limits": {
                "interval_sec": INTERVAL_SEC,
                "min_edge_pct": MIN_EDGE_PCT,
                "min_pool_liq_usd": MIN_POOL_LIQ_USD,
                "size_usd": SIZE_USD,
                "max_per_tick": MAX_PER_TICK,
                "require_jupiter": REQUIRE_JUPITER,
                "max_jupiter_impact_pct": MAX_JUPITER_IMPACT_PCT,
            },
            "enabled_env": ARB_ENABLED,
            "live_trading": False,
            "note": (
                "paper-only cross-pool quote arb; Jupiter sim gate before fills; "
                "ms racing needs paid Geyser/Jito later"
            ),
        }


def start(*, interval_s: Optional[float] = None) -> dict[str, Any]:
    global _thread
    if _get_tokens is None:
        return {**status(), "error": "arb not configured"}
    with _lock:
        if _thread and _thread.is_alive():
            return status()
        _stop.clear()
        _state.running = True
        _state.last_error = None
        period = float(interval_s if interval_s is not None else INTERVAL_SEC)
        _thread = threading.Thread(
            target=_run, args=(period,), name="paper-arb", daemon=True
        )
        _thread.start()
    pipeline_log.emit("arb", "start", interval_s=period)
    return status()


def stop() -> dict[str, Any]:
    _stop.set()
    with _lock:
        _state.running = False
    pipeline_log.emit("arb", "stop")
    return status()


def scan_once() -> dict[str, Any]:
    if _get_tokens is None:
        return {"status": "error", "error": "arb not configured"}
    started = time.perf_counter()
    with pipeline_log.run(prefix="arb-") as run_id:
        try:
            result = _scan_and_maybe_fill()
            result["run_id"] = run_id
            with _lock:
                _state.ticks += 1
                _state.last_tick_at = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                _state.last_error = None
                _state.opportunities_seen += int(result.get("opportunities_n") or 0)
                _state.paper_fills += int(result.get("fills_n") or 0)
                _state.realized_pnl_usd += float(result.get("pnl_usd") or 0.0)
                _state.last_opps = list(result.get("opportunities") or [])[:20]
            pipeline_log.emit(
                "arb",
                "tick_done",
                duration_ms=pipeline_log.timed_ms(started),
                mints=result.get("mints_n"),
                opportunities_n=result.get("opportunities_n"),
                fills_n=result.get("fills_n"),
                pnl_usd=result.get("pnl_usd"),
            )
            return result
        except Exception as exc:  # noqa: BLE001
            with _lock:
                _state.last_error = str(exc)
            pipeline_log.emit(
                "arb", "tick_error", level="error", reason=str(exc)
            )
            return {"status": "error", "error": str(exc), "run_id": run_id}


def _run(interval_s: float) -> None:
    while not _stop.is_set():
        scan_once()
        _stop.wait(interval_s)
    with _lock:
        _state.running = False


def _universe_mints() -> list[tuple[str, str]]:
    """Return [(mint, symbol), ...] from the live discover/extension buffer."""
    tokens = (_get_tokens() or {}) if _get_tokens else {}
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for _addr, tok in tokens.items():
        if not isinstance(tok, dict):
            continue
        mint = (tok.get("mint") or "").strip()
        if not mint or mint in seen or mint == WRAPPED_SOL_MINT:
            continue
        seen.add(mint)
        out.append((mint, str(tok.get("name") or "")))
    return out


def find_opportunities(mint: str, *, symbol: str = "") -> list[ArbOpportunity]:
    """Compare DexScreener pool mid prices for one mint; return positive-net opps."""
    try:
        pairs = fetch_dexscreener_pairs(mint)
    except SourceError as exc:
        pipeline_log.emit(
            "arb", "quote_error", level="warning", mint=mint, reason=str(exc)
        )
        return []

    quotes: list[PoolQuote] = []
    for pair in pairs:
        q = _quote_from_pair(pair, mint=mint)
        if q is None:
            continue
        if q.liquidity_usd < MIN_POOL_LIQ_USD:
            continue
        quotes.append(q)

    if len(quotes) < 2:
        return []

    # Cheapest venue to buy, richest to sell — classic cross-pool arb shape.
    buy = min(quotes, key=lambda x: x.price_usd)
    sell = max(quotes, key=lambda x: x.price_usd)
    if buy.pair == sell.pair or buy.price_usd <= 0:
        return []

    gross = (sell.price_usd / buy.price_usd - 1.0) * 100.0
    cost = _arb_cost_pct()
    net = gross - cost
    pipeline_log.emit(
        "arb",
        "spread",
        mint=mint,
        symbol=symbol or buy.symbol,
        buy_dex=buy.dex,
        sell_dex=sell.dex,
        gross_pct=round(gross, 4),
        cost_pct=round(cost, 4),
        net_pct=round(net, 4),
        buy_liq=buy.liquidity_usd,
        sell_liq=sell.liquidity_usd,
    )
    if net < MIN_EDGE_PCT:
        return []

    size = SIZE_USD
    return [
        ArbOpportunity(
            mint=mint,
            symbol=symbol or buy.symbol,
            buy=buy,
            sell=sell,
            gross_pct=round(gross, 4),
            cost_pct=round(cost, 4),
            net_pct=round(net, 4),
            size_usd=size,
            expected_pnl_usd=round(size * net / 100.0, 4),
        )
    ]


def _arb_cost_pct() -> float:
    slip = ARB_SLIP_PCT if ARB_SLIP_PCT > 0 else None
    # Two swap legs through different pools — model as full round trip.
    return costs.round_trip_cost_pct(
        price_impact_pct=slip if slip else None,
        slippage_bps=None if slip else None,
    )


def _quote_from_pair(pair: dict[str, Any], *, mint: str) -> Optional[PoolQuote]:
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    base_addr = base.get("address") or ""
    quote_addr = quote.get("address") or ""
    # PriceUsd on DexScreener is for the pair's base token.
    if base_addr == mint:
        price_raw = pair.get("priceUsd")
        symbol = base.get("symbol") or ""
    elif quote_addr == mint:
        # Rare: mint on quote side — invert if we have priceNative, else skip.
        return None
    else:
        return None
    try:
        price = float(price_raw)
        liq = float(((pair.get("liquidity") or {}).get("usd")) or 0.0)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    return PoolQuote(
        pair=str(pair.get("pairAddress") or ""),
        dex=str(pair.get("dexId") or "unknown"),
        price_usd=price,
        liquidity_usd=liq,
        symbol=symbol,
    )


def _scan_and_maybe_fill() -> dict[str, Any]:
    universe = _universe_mints()
    opps: list[ArbOpportunity] = []
    for mint, symbol in universe:
        opps.extend(find_opportunities(mint, symbol=symbol))

    opps.sort(key=lambda o: o.net_pct, reverse=True)
    fills: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    pnl_total = 0.0
    for opp in opps[:MAX_PER_TICK]:
        ok, jup = _jupiter_verify(opp)
        if not ok:
            skipped.append(
                {
                    "mint": opp.mint,
                    "symbol": opp.symbol,
                    "net_pct": opp.net_pct,
                    "reason": jup.get("reason") or "jupiter_reject",
                    "jupiter": jup,
                }
            )
            pipeline_log.emit(
                "arb",
                "jupiter_skip",
                level="warning",
                mint=opp.mint,
                symbol=opp.symbol,
                reason=jup.get("reason"),
                impact_pct=jup.get("impact_pct"),
            )
            continue
        fill = _paper_fill(opp, jupiter=jup)
        fills.append(fill)
        pnl_total += float(fill.get("realized_pnl_usd") or 0.0)

    return {
        "status": "ok",
        "mints_n": len(universe),
        "opportunities_n": len(opps),
        "fills_n": len(fills),
        "skipped_n": len(skipped),
        "pnl_usd": round(pnl_total, 4),
        "opportunities": [_opp_dict(o) for o in opps[:20]],
        "fills": fills,
        "skipped": skipped,
    }


def _jupiter_verify(opp: ArbOpportunity) -> tuple[bool, dict[str, Any]]:
    """Confirm Jupiter can route ~SIZE_USD of this mint before booking paper PnL.

    Cross-pool Dex gaps often look free but are not executable. A successful
    sell quote at our size (with bounded impact) is the cheap paper filter.
    """
    if not REQUIRE_JUPITER:
        return True, {"verified": False, "reason": "jupiter_not_required"}

    price = float(opp.buy.price_usd or 0.0)
    if price <= 0:
        return False, {"verified": False, "reason": "bad_buy_price"}

    try:
        mint_info = fetch_mint_account(opp.mint)
        decimals = int(mint_info.get("decimals"))
    except (SourceError, TypeError, ValueError) as exc:
        return False, {"verified": False, "reason": f"mint_meta: {exc}"}

    amount_raw = int((SIZE_USD / price) * (10**decimals))
    if amount_raw <= 0:
        return False, {"verified": False, "reason": "size_rounds_to_zero"}

    try:
        quote = fetch_sell_quote(opp.mint, amount_raw)
    except NoRouteError as exc:
        return False, {"verified": False, "reason": f"no_route: {exc}"}
    except SourceError as exc:
        # Outage: do not invent fills — wait for a real quote.
        return False, {"verified": False, "reason": f"jupiter_unavailable: {exc}"}

    impact_raw = quote.get("priceImpactPct")
    try:
        impact_pct = float(impact_raw) * 100.0 if impact_raw is not None else None
    except (TypeError, ValueError):
        impact_pct = None

    if impact_pct is not None and impact_pct > MAX_JUPITER_IMPACT_PCT:
        return False, {
            "verified": False,
            "reason": f"impact {impact_pct:.2f}% > {MAX_JUPITER_IMPACT_PCT}",
            "impact_pct": round(impact_pct, 4),
        }

    # Haircut expected edge by measured sell impact (buy impact ~ similar).
    adj_net = opp.net_pct
    if impact_pct is not None:
        adj_net = opp.net_pct - impact_pct
        if adj_net < MIN_EDGE_PCT:
            return False, {
                "verified": False,
                "reason": f"impact-adjusted net {adj_net:.2f}% < {MIN_EDGE_PCT}",
                "impact_pct": round(impact_pct, 4),
                "adj_net_pct": round(adj_net, 4),
            }

    return True, {
        "verified": True,
        "impact_pct": None if impact_pct is None else round(impact_pct, 4),
        "adj_net_pct": round(adj_net, 4),
        "out_amount": quote.get("outAmount"),
        "amount_raw": amount_raw,
    }


def _paper_fill(opp: ArbOpportunity, *, jupiter: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Simulate an atomic buy@cheap / sell@rich fill. No wallet touch."""
    adj = None
    if jupiter and jupiter.get("adj_net_pct") is not None:
        try:
            adj = float(jupiter["adj_net_pct"])
        except (TypeError, ValueError):
            adj = None
    net_for_pnl = adj if adj is not None else opp.net_pct
    pnl = round(opp.size_usd * net_for_pnl / 100.0, 4)
    record = {
        "event": "paper_arb",
        "id": str(uuid.uuid4())[:8],
        "mint": opp.mint,
        "symbol": opp.symbol,
        "buy_pair": opp.buy.pair,
        "buy_dex": opp.buy.dex,
        "sell_pair": opp.sell.pair,
        "sell_dex": opp.sell.dex,
        "gross_pct": opp.gross_pct,
        "cost_pct": opp.cost_pct,
        "net_pct": opp.net_pct,
        "adj_net_pct": net_for_pnl,
        "size_usd": opp.size_usd,
        "realized_pnl_usd": pnl,
        "filled_at": datetime.now(timezone.utc).isoformat(),
        "mode": "paper_atomic_sim",
        "jupiter": jupiter or {},
    }
    try:
        decision_store.append("paper", record)
        decision_store.append(
            "outcomes",
            {
                "address": opp.buy.pair,
                "mint": opp.mint,
                "timeframe": "arb",
                "horizon_bars": 0,
                "action": "paper_arb",
                "predicted_p50": net_for_pnl,
                "predicted_p10": net_for_pnl,
                "predicted_p90": net_for_pnl,
                "realized_pct": net_for_pnl,
                "error_pct": 0.0,
                "covered_80": True,
                "entry_price": opp.buy.price_usd,
                "exit_price": opp.sell.price_usd,
                "scored_at": record["filled_at"],
            },
        )
    except OSError:
        pass
    pipeline_log.emit(
        "arb",
        "paper_fill",
        mint=opp.mint,
        symbol=opp.symbol,
        net_pct=opp.net_pct,
        adj_net_pct=net_for_pnl,
        realized_pnl_usd=pnl,
        buy_dex=opp.buy.dex,
        sell_dex=opp.sell.dex,
        jupiter_verified=bool((jupiter or {}).get("verified")),
    )
    return record


def _opp_dict(opp: ArbOpportunity) -> dict[str, Any]:
    return {
        "mint": opp.mint,
        "symbol": opp.symbol,
        "gross_pct": opp.gross_pct,
        "cost_pct": opp.cost_pct,
        "net_pct": opp.net_pct,
        "size_usd": opp.size_usd,
        "expected_pnl_usd": opp.expected_pnl_usd,
        "buy": asdict(opp.buy),
        "sell": asdict(opp.sell),
    }
