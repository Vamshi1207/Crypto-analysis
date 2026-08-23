"""Parallel paper channel: cross-pool Solana quote arbitrage (no live swaps).

This is the useful slice of the viral “ms arb bot” idea — detect when the *same*
mint prints different USD prices on two DEXes/pools, size the gap against real
round-trip costs, and book a paper atomic round-trip when net edge clears.

Honesty upgrades (paper still optimistic vs live MEV/latency, but less naive):
- Size capped by a fraction of the thinner pool (cannot always deploy $40).
- Per-mint cooldown + daily fill cap (no double-counting every tick).
- Cross-DEX only (same-venue gaps are often noise).
- Half-gap stress gate: if the spread halves, edge must still clear the hurdle.
- Jupiter sell-route check; impact haircut applied twice (buy+sell legs).
- Booked PnL uses the **conservative** (stressed) net, not the raw Dex gap.

It does **not** do mempool racing or Jito bundles yet. True millisecond racing
needs paid Geyser/gRPC or Jito; this channel asks whether gaps survive *our*
cost model before any live tips are spent.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

from decision import costs
from decision import pipeline_log
from decision import store as decision_store
from decision.config import WRAPPED_SOL_MINT, _env_float, _env_int
from decision.sources import (
    NoRouteError,
    SourceError,
    fetch_dexscreener_pairs,
    fetch_jupiter_quote,
    fetch_mint_account,
    fetch_sell_quote,
)

ARB_ENABLED = os.getenv("ARB_ENABLED", "0").strip() == "1"
INTERVAL_SEC = _env_float("ARB_INTERVAL_SEC", 20.0)
MIN_EDGE_PCT = _env_float("ARB_MIN_EDGE_PCT", 0.8)
MIN_POOL_LIQ_USD = _env_float("ARB_MIN_POOL_LIQ_USD", 8_000.0)
SIZE_USD = _env_float("ARB_SIZE_USD", 40.0)
MIN_SIZE_USD = _env_float("ARB_MIN_SIZE_USD", 10.0)
# Never take more than this fraction of the thinner pool's USD liquidity.
MAX_POOL_FRAC = _env_float("ARB_MAX_POOL_FRAC", 0.005)
MAX_PER_TICK = _env_int("ARB_MAX_PER_TICK", 3)
# Quiet period after a paper fill on a mint. 0 = off (re-enter whenever analysis clears).
MINT_COOLDOWN_SEC = _env_float("ARB_MINT_COOLDOWN_SEC", 0.0)
# Max paper fills per mint per UTC day. 0 = unlimited (analysis gates only).
MAX_FILLS_PER_MINT_DAY = _env_int("ARB_MAX_FILLS_PER_MINT_DAY", 0)
# Require buy/sell on different DEX ids (cross-venue).
REQUIRE_CROSS_DEX = os.getenv("ARB_REQUIRE_CROSS_DEX", "1").strip() == "1"
# Only book if (gross * stress_frac - costs) still clears MIN_EDGE.
STRESS_GAP_FRAC = _env_float("ARB_STRESS_GAP_FRAC", 0.5)
# Reject DexScreener mid prices implying impossible cross-pool gaps (bad/stale pool data).
MAX_GROSS_PCT = _env_float("ARB_MAX_GROSS_PCT", 50.0)
# Arb legs are two swaps; reuse the shared cost model (fees + slip + tip).
ARB_SLIP_PCT = _env_float("ARB_SLIP_PCT", 0.0)  # 0 = use costs.DEFAULT_ENTRY_SLIP_PCT
REQUIRE_JUPITER = os.getenv("ARB_REQUIRE_JUPITER", "1").strip() == "1"
MAX_JUPITER_IMPACT_PCT = _env_float("ARB_MAX_JUPITER_IMPACT_PCT", 3.0)
# Multiply sell-leg impact by this to approximate buy+sell impact.
JUPITER_IMPACT_LEGS = _env_float("ARB_JUPITER_IMPACT_LEGS", 2.0)
# Also try SOL→mint Jupiter quote when we can price SOL.
REQUIRE_BUY_ROUTE = os.getenv("ARB_REQUIRE_BUY_ROUTE", "1").strip() == "1"
SOL_USD_FALLBACK = _env_float("ARB_SOL_USD_FALLBACK", 140.0)


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
    stress_net_pct: float
    size_usd: float
    expected_pnl_usd: float
    stress_pnl_usd: float


@dataclass
class ArbState:
    running: bool = False
    ticks: int = 0
    last_tick_at: Optional[str] = None
    last_error: Optional[str] = None
    opportunities_seen: int = 0
    paper_fills: int = 0
    realized_pnl_usd: float = 0.0
    skipped_cooldown: int = 0
    skipped_jupiter: int = 0
    skipped_stress: int = 0
    last_opps: list[dict[str, Any]] = field(default_factory=list)


_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_state = ArbState()
_get_tokens: Optional[Callable[[], dict[str, Any]]] = None
# mint → cool-until unix; mint → (utc-day-iso, fill_count)
_cool_until: dict[str, float] = {}
_fills_today: dict[str, tuple[str, int]] = {}
_sol_usd_cache: tuple[float, float] = (0.0, 0.0)  # (unix_ts, usd)


def configure(*, get_tokens: Callable[[], dict[str, Any]]) -> None:
    global _get_tokens
    _get_tokens = get_tokens


def reset_counters() -> dict[str, Any]:
    """Zero session stats + cool-downs (keeps the loop running if already started)."""
    with _lock:
        _state.ticks = 0
        _state.last_tick_at = None
        _state.last_error = None
        _state.opportunities_seen = 0
        _state.paper_fills = 0
        _state.realized_pnl_usd = 0.0
        _state.skipped_cooldown = 0
        _state.skipped_jupiter = 0
        _state.skipped_stress = 0
        _state.last_opps = []
        _cool_until.clear()
        _fills_today.clear()
    pipeline_log.emit("arb", "reset", level="warning")
    return status()


def status() -> dict[str, Any]:
    with _lock:
        cooling = sum(1 for t in _cool_until.values() if t > time.time())
        return {
            "running": _state.running,
            "ticks": _state.ticks,
            "last_tick_at": _state.last_tick_at,
            "last_error": _state.last_error,
            "opportunities_seen": _state.opportunities_seen,
            "paper_fills": _state.paper_fills,
            "realized_pnl_usd": round(_state.realized_pnl_usd, 4),
            "skipped_cooldown": _state.skipped_cooldown,
            "skipped_jupiter": _state.skipped_jupiter,
            "skipped_stress": _state.skipped_stress,
            "cooling_mints": cooling,
            "last_opps": list(_state.last_opps)[:20],
            "limits": {
                "interval_sec": INTERVAL_SEC,
                "min_edge_pct": MIN_EDGE_PCT,
                "min_pool_liq_usd": MIN_POOL_LIQ_USD,
                "size_usd": SIZE_USD,
                "min_size_usd": MIN_SIZE_USD,
                "max_pool_frac": MAX_POOL_FRAC,
                "max_per_tick": MAX_PER_TICK,
                "mint_cooldown_sec": MINT_COOLDOWN_SEC,
                "max_fills_per_mint_day": MAX_FILLS_PER_MINT_DAY,
                "require_cross_dex": REQUIRE_CROSS_DEX,
                "stress_gap_frac": STRESS_GAP_FRAC,
                "max_gross_pct": MAX_GROSS_PCT,
                "require_jupiter": REQUIRE_JUPITER,
                "max_jupiter_impact_pct": MAX_JUPITER_IMPACT_PCT,
                "jupiter_impact_legs": JUPITER_IMPACT_LEGS,
                "require_buy_route": REQUIRE_BUY_ROUTE,
            },
            "enabled_env": ARB_ENABLED,
            "live_trading": False,
            "note": (
                "paper-only cross-pool arb; entries gated by analysis "
                "(stress gap + Jupiter + liq size), not by default timers; "
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
                _state.skipped_cooldown += int(result.get("skipped_cooldown_n") or 0)
                _state.skipped_jupiter += int(result.get("skipped_jupiter_n") or 0)
                _state.skipped_stress += int(result.get("skipped_stress_n") or 0)
                _state.last_opps = list(result.get("opportunities") or [])[:20]
            pipeline_log.emit(
                "arb",
                "tick_done",
                duration_ms=pipeline_log.timed_ms(started),
                mints=result.get("mints_n"),
                opportunities_n=result.get("opportunities_n"),
                fills_n=result.get("fills_n"),
                pnl_usd=result.get("pnl_usd"),
                skipped_cooldown_n=result.get("skipped_cooldown_n"),
                skipped_jupiter_n=result.get("skipped_jupiter_n"),
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


def _sized_usd(buy: PoolQuote, sell: PoolQuote) -> Optional[float]:
    """Cap notional by thinner-pool liquidity; None if below minimum."""
    thin = min(buy.liquidity_usd, sell.liquidity_usd)
    capped = min(SIZE_USD, thin * MAX_POOL_FRAC)
    if capped < MIN_SIZE_USD:
        return None
    return round(capped, 4)


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

    buy = min(quotes, key=lambda x: x.price_usd)
    sell = max(quotes, key=lambda x: x.price_usd)
    if buy.pair == sell.pair or buy.price_usd <= 0:
        return []
    if REQUIRE_CROSS_DEX and buy.dex == sell.dex:
        pipeline_log.emit(
            "arb",
            "same_dex_skip",
            mint=mint,
            symbol=symbol or buy.symbol,
            dex=buy.dex,
        )
        return []

    gross = (sell.price_usd / buy.price_usd - 1.0) * 100.0
    if gross > MAX_GROSS_PCT:
        pipeline_log.emit(
            "arb",
            "insane_spread_skip",
            level="warning",
            mint=mint,
            symbol=symbol or buy.symbol,
            gross_pct=round(gross, 4),
            max_gross_pct=MAX_GROSS_PCT,
            buy_dex=buy.dex,
            sell_dex=sell.dex,
            buy_price=buy.price_usd,
            sell_price=sell.price_usd,
        )
        return []
    cost = _arb_cost_pct()
    net = gross - cost
    stress_net = gross * STRESS_GAP_FRAC - cost
    size = _sized_usd(buy, sell)
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
        stress_net_pct=round(stress_net, 4),
        size_usd=size,
        buy_liq=buy.liquidity_usd,
        sell_liq=sell.liquidity_usd,
    )
    if size is None:
        return []
    if net < MIN_EDGE_PCT:
        return []
    if stress_net < MIN_EDGE_PCT:
        pipeline_log.emit(
            "arb",
            "stress_skip",
            level="warning",
            mint=mint,
            symbol=symbol or buy.symbol,
            stress_net_pct=round(stress_net, 4),
            need=MIN_EDGE_PCT,
        )
        return []

    return [
        ArbOpportunity(
            mint=mint,
            symbol=symbol or buy.symbol,
            buy=buy,
            sell=sell,
            gross_pct=round(gross, 4),
            cost_pct=round(cost, 4),
            net_pct=round(net, 4),
            stress_net_pct=round(stress_net, 4),
            size_usd=size,
            expected_pnl_usd=round(size * net / 100.0, 4),
            stress_pnl_usd=round(size * stress_net / 100.0, 4),
        )
    ]


def _arb_cost_pct() -> float:
    slip = ARB_SLIP_PCT if ARB_SLIP_PCT > 0 else None
    return costs.round_trip_cost_pct(
        price_impact_pct=slip if slip else None,
        slippage_bps=None if slip else None,
    )


def _quote_from_pair(pair: dict[str, Any], *, mint: str) -> Optional[PoolQuote]:
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    base_addr = base.get("address") or ""
    quote_addr = quote.get("address") or ""
    if base_addr == mint:
        price_raw = pair.get("priceUsd")
        symbol = base.get("symbol") or ""
    elif quote_addr == mint:
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


def _mint_blocked(mint: str) -> Optional[str]:
    """Optional timer/quota barriers. Both default off — analysis decides.

    Returns a skip reason only when the corresponding limit is explicitly > 0.
    """
    now = time.time()
    with _lock:
        if MINT_COOLDOWN_SEC > 0:
            until = _cool_until.get(mint)
            if until and until > now:
                return f"cooldown {int(until - now)}s"
            if until and until <= now:
                _cool_until.pop(mint, None)

        if MAX_FILLS_PER_MINT_DAY > 0:
            day = date.today().isoformat()
            prev = _fills_today.get(mint)
            if prev and prev[0] == day and prev[1] >= MAX_FILLS_PER_MINT_DAY:
                return f"mint daily cap {prev[1]}/{MAX_FILLS_PER_MINT_DAY}"
            if prev and prev[0] != day:
                _fills_today.pop(mint, None)
    return None


def _record_fill_limits(mint: str) -> None:
    """Track optional cool-down / daily counts when those limits are enabled."""
    day = date.today().isoformat()
    with _lock:
        if MINT_COOLDOWN_SEC > 0:
            _cool_until[mint] = time.time() + MINT_COOLDOWN_SEC
        if MAX_FILLS_PER_MINT_DAY > 0:
            prev = _fills_today.get(mint)
            if prev and prev[0] == day:
                _fills_today[mint] = (day, prev[1] + 1)
            else:
                _fills_today[mint] = (day, 1)


def _scan_and_maybe_fill() -> dict[str, Any]:
    universe = _universe_mints()
    opps: list[ArbOpportunity] = []
    for mint, symbol in universe:
        opps.extend(find_opportunities(mint, symbol=symbol))

    opps.sort(key=lambda o: o.stress_net_pct, reverse=True)
    fills: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    skipped_cooldown_n = 0
    skipped_jupiter_n = 0
    skipped_stress_n = 0
    pnl_total = 0.0

    for opp in opps:
        if len(fills) >= MAX_PER_TICK:
            break

        blocked = _mint_blocked(opp.mint)
        if blocked:
            skipped_cooldown_n += 1
            skipped.append(
                {
                    "mint": opp.mint,
                    "symbol": opp.symbol,
                    "net_pct": opp.net_pct,
                    "reason": blocked,
                }
            )
            pipeline_log.emit(
                "arb",
                "cooldown_skip",
                mint=opp.mint,
                symbol=opp.symbol,
                reason=blocked,
            )
            continue

        ok, jup = _jupiter_verify(opp)
        if not ok:
            reason = str(jup.get("reason") or "jupiter_reject")
            if "stress" in reason or "impact-adjusted" in reason:
                skipped_stress_n += 1
            else:
                skipped_jupiter_n += 1
            skipped.append(
                {
                    "mint": opp.mint,
                    "symbol": opp.symbol,
                    "net_pct": opp.net_pct,
                    "stress_net_pct": opp.stress_net_pct,
                    "reason": reason,
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
        _record_fill_limits(opp.mint)
        fills.append(fill)
        pnl_total += float(fill.get("realized_pnl_usd") or 0.0)

    return {
        "status": "ok",
        "mints_n": len(universe),
        "opportunities_n": len(opps),
        "fills_n": len(fills),
        "skipped_n": len(skipped),
        "skipped_cooldown_n": skipped_cooldown_n,
        "skipped_jupiter_n": skipped_jupiter_n,
        "skipped_stress_n": skipped_stress_n,
        "pnl_usd": round(pnl_total, 4),
        "opportunities": [_opp_dict(o) for o in opps[:20]],
        "fills": fills,
        "skipped": skipped,
    }


def _estimate_sol_usd() -> float:
    """Best-effort SOL/USD for buy-leg sizing; cached briefly."""
    global _sol_usd_cache
    now = time.time()
    ts, cached = _sol_usd_cache
    if cached > 0 and now - ts < 60.0:
        return cached

    # Prefer a SOL-priced meme pair already on the board is hard; use fallback
    # unless DexScreener returns a direct SOL/USDC style hit via WSOL pairs.
    usd = SOL_USD_FALLBACK
    try:
        pairs = fetch_dexscreener_pairs(WRAPPED_SOL_MINT)
        for pair in pairs[:8]:
            try:
                px = float(pair.get("priceUsd") or 0)
            except (TypeError, ValueError):
                continue
            # WSOL as base should print ~SOL USD; sanity band.
            if 20.0 <= px <= 1000.0:
                usd = px
                break
    except SourceError:
        pass
    _sol_usd_cache = (now, usd)
    return usd


def _jupiter_verify(opp: ArbOpportunity) -> tuple[bool, dict[str, Any]]:
    """Confirm Jupiter can route our sized notional before booking paper PnL."""
    if not REQUIRE_JUPITER:
        booked = min(opp.net_pct, opp.stress_net_pct)
        return True, {
            "verified": False,
            "reason": "jupiter_not_required",
            "booked_net_pct": round(booked, 4),
        }

    price = float(opp.buy.price_usd or 0.0)
    if price <= 0:
        return False, {"verified": False, "reason": "bad_buy_price"}

    try:
        mint_info = fetch_mint_account(opp.mint)
        decimals = int(mint_info.get("decimals"))
    except (SourceError, TypeError, ValueError) as exc:
        return False, {"verified": False, "reason": f"mint_meta: {exc}"}

    amount_raw = int((opp.size_usd / price) * (10**decimals))
    if amount_raw <= 0:
        return False, {"verified": False, "reason": "size_rounds_to_zero"}

    buy_impact = None
    if REQUIRE_BUY_ROUTE:
        sol_usd = _estimate_sol_usd()
        lamports = int((opp.size_usd / sol_usd) * (10**9))
        if lamports <= 0:
            return False, {"verified": False, "reason": "buy_size_rounds_to_zero"}
        try:
            buy_q = fetch_jupiter_quote(WRAPPED_SOL_MINT, opp.mint, lamports)
        except NoRouteError as exc:
            return False, {"verified": False, "reason": f"no_buy_route: {exc}"}
        except SourceError as exc:
            return False, {"verified": False, "reason": f"jupiter_buy_unavailable: {exc}"}
        buy_impact = _impact_pct(buy_q)

    try:
        sell_q = fetch_sell_quote(opp.mint, amount_raw)
    except NoRouteError as exc:
        return False, {"verified": False, "reason": f"no_route: {exc}"}
    except SourceError as exc:
        return False, {"verified": False, "reason": f"jupiter_unavailable: {exc}"}

    sell_impact = _impact_pct(sell_q)
    impacts = [x for x in (buy_impact, sell_impact) if x is not None]
    if not impacts and sell_impact is None:
        impact_pct = None
        impact_rt = None
    elif impacts:
        # Prefer measured sum of legs; else 2× sell as proxy.
        if buy_impact is not None and sell_impact is not None:
            impact_rt = buy_impact + sell_impact
            impact_pct = sell_impact
        else:
            impact_pct = sell_impact if sell_impact is not None else buy_impact
            impact_rt = float(impact_pct) * JUPITER_IMPACT_LEGS
    else:
        impact_pct = None
        impact_rt = None

    if impact_pct is not None and impact_pct > MAX_JUPITER_IMPACT_PCT:
        return False, {
            "verified": False,
            "reason": f"impact {impact_pct:.2f}% > {MAX_JUPITER_IMPACT_PCT}",
            "impact_pct": round(impact_pct, 4),
            "impact_rt_pct": None if impact_rt is None else round(impact_rt, 4),
        }

    # Conservative book: stress gap, then subtract round-trip Jupiter impact.
    booked = opp.stress_net_pct
    if impact_rt is not None:
        booked = opp.stress_net_pct - impact_rt
    if booked < MIN_EDGE_PCT:
        return False, {
            "verified": False,
            "reason": (
                f"stress/impact net {booked:.2f}% < {MIN_EDGE_PCT}"
            ),
            "impact_pct": None if impact_pct is None else round(impact_pct, 4),
            "impact_rt_pct": None if impact_rt is None else round(impact_rt, 4),
            "booked_net_pct": round(booked, 4),
            "optimistic_net_pct": opp.net_pct,
            "stress_net_pct": opp.stress_net_pct,
        }

    return True, {
        "verified": True,
        "impact_pct": None if impact_pct is None else round(impact_pct, 4),
        "impact_rt_pct": None if impact_rt is None else round(impact_rt, 4),
        "buy_impact_pct": None if buy_impact is None else round(buy_impact, 4),
        "sell_impact_pct": None if sell_impact is None else round(sell_impact, 4),
        "booked_net_pct": round(booked, 4),
        "optimistic_net_pct": opp.net_pct,
        "stress_net_pct": opp.stress_net_pct,
        "out_amount": sell_q.get("outAmount"),
        "amount_raw": amount_raw,
        "size_usd": opp.size_usd,
    }


def _impact_pct(quote: dict[str, Any]) -> Optional[float]:
    impact_raw = quote.get("priceImpactPct")
    try:
        if impact_raw is None:
            return None
        return float(impact_raw) * 100.0
    except (TypeError, ValueError):
        return None


def _paper_fill(opp: ArbOpportunity, *, jupiter: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Simulate an atomic buy@cheap / sell@rich fill. Books conservative net."""
    booked = opp.stress_net_pct
    if jupiter and jupiter.get("booked_net_pct") is not None:
        try:
            booked = float(jupiter["booked_net_pct"])
        except (TypeError, ValueError):
            booked = opp.stress_net_pct
    pnl = round(opp.size_usd * booked / 100.0, 4)
    optimistic_pnl = round(opp.size_usd * opp.net_pct / 100.0, 4)
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
        "stress_net_pct": opp.stress_net_pct,
        "adj_net_pct": booked,
        "booked_net_pct": booked,
        "optimistic_pnl_usd": optimistic_pnl,
        "size_usd": opp.size_usd,
        "realized_pnl_usd": pnl,
        "filled_at": datetime.now(timezone.utc).isoformat(),
        "mode": "paper_atomic_sim_conservative",
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
                "predicted_p50": booked,
                "predicted_p10": booked,
                "predicted_p90": opp.net_pct,
                "realized_pct": booked,
                "error_pct": round(opp.net_pct - booked, 4),
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
        stress_net_pct=opp.stress_net_pct,
        booked_net_pct=booked,
        realized_pnl_usd=pnl,
        optimistic_pnl_usd=optimistic_pnl,
        size_usd=opp.size_usd,
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
        "stress_net_pct": opp.stress_net_pct,
        "size_usd": opp.size_usd,
        "expected_pnl_usd": opp.expected_pnl_usd,
        "stress_pnl_usd": opp.stress_pnl_usd,
        "buy": asdict(opp.buy),
        "sell": asdict(opp.sell),
    }
