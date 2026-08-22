"""Headless Solana token discovery → hydrate live buffer for paper swarm.

Universe v1: DexScreener Solana boosts + GeckoTerminal trending pools.
Hard prefilters run before Gate 0; only a shortlist gets safety + OHLCV spend.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from decision import ohlcv_remote
from decision import pipeline_log
from decision.config import WRAPPED_SOL_MINT, _env_float, _env_int
from decision.safety import check_token
from decision.schema import SafetyVerdict
from decision.sources import (
    SourceError,
    fetch_dexscreener_boosts,
    fetch_dexscreener_pairs,
    fetch_gecko_trending_pools,
)

# Common Solana quote / stable mints — never trade these as the "meme" base.
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTE_MINTS = frozenset({WRAPPED_SOL_MINT, USDC_MINT, USDT_MINT})
STABLE_BASE_SYMBOLS = frozenset({"SOL", "WSOL", "USDC", "USDT", "USD1", "DAI"})

DISCOVER_ENABLED = os.getenv("DISCOVER_ENABLED", "0").strip() == "1"
INTERVAL_SEC = _env_float("DISCOVER_INTERVAL_SEC", 90.0)
# Trade roster (full Gate 0 + swarm). Observe roster fills the dashboard.
MAX_CANDIDATES = _env_int("DISCOVER_MAX_CANDIDATES", 12)
MAX_OBSERVE = _env_int("DISCOVER_MAX_OBSERVE", 24)
MIN_LIQUIDITY_USD = _env_float("DISCOVER_MIN_LIQUIDITY_USD", 10_000.0)
MIN_VOLUME_24H_USD = _env_float("DISCOVER_MIN_VOLUME_24H_USD", 15_000.0)
MIN_AGE_MIN = _env_float("DISCOVER_MIN_AGE_MIN", 30.0)
MAX_AGE_DAYS = _env_float("DISCOVER_MAX_AGE_DAYS", 14.0)
# Skip Gate 0 re-runs for mints we already screened this session (TTL seconds).
SAFETY_CACHE_SEC = _env_float("DISCOVER_SAFETY_CACHE_SEC", 600.0)
# After this many consecutive Gate-2 / no-edge holds, park the mint so the
# scanner rotates into the rest of the universe instead of babysitting it.
NO_EDGE_STREAK = _env_int("DISCOVER_NO_EDGE_STREAK", 3)
# 0 = do not time-park; next discover/swarm pass may re-evaluate immediately.
NO_EDGE_COOLDOWN_SEC = _env_float("DISCOVER_NO_EDGE_COOLDOWN_SEC", 0.0)
# Walk this many filtered names trying to fill the watchlist (past cool-downs).
EXPLORE_MULTIPLIER = _env_int("DISCOVER_EXPLORE_MULTIPLIER", 5)


@dataclass
class Candidate:
    mint: str
    pool: str
    symbol: str
    name: str
    liquidity_usd: float
    volume_24h_usd: float
    pair_created_at_ms: Optional[float]
    boost_amount: float = 0.0
    tx_h1: int = 0
    source: str = "unknown"
    score: float = 0.0
    reject_reason: Optional[str] = None
    price_usd: Optional[float] = None

    def age_minutes(self, now_ms: Optional[float] = None) -> Optional[float]:
        if self.pair_created_at_ms is None:
            return None
        now = now_ms if now_ms is not None else time.time() * 1000.0
        return max(0.0, (now - self.pair_created_at_ms) / 60_000.0)


@dataclass
class DiscoverState:
    running: bool = False
    ticks: int = 0
    last_scan_at: Optional[str] = None
    last_error: Optional[str] = None
    last_candidates: list[dict[str, Any]] = field(default_factory=list)
    watchlist: list[str] = field(default_factory=list)  # pool addresses
    rejected: list[dict[str, Any]] = field(default_factory=list)


_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_state = DiscoverState()
_safety_ok: dict[str, float] = {}  # mint → unix ts when passed Gate 0
_safety_bad: dict[str, tuple[float, str]] = {}  # mint → (ts, reason)
# Rotation: mint → cool-until unix; streak of no-edge decides before cooling.
_cool_until: dict[str, float] = {}
_no_edge_streak: dict[str, int] = {}
_cool_meta: dict[str, dict[str, Any]] = {}

# Injected by server so discover can write the shared live buffer.
_token_sink: Optional[Callable[[str, dict[str, Any]], None]] = None
_token_source: Optional[Callable[[], dict[str, Any]]] = None


def configure(
    *,
    set_token: Callable[[str, dict[str, Any]], None],
    get_tokens: Callable[[], dict[str, Any]],
) -> None:
    global _token_sink, _token_source
    _token_sink = set_token
    _token_source = get_tokens


def clear_rotation() -> dict[str, Any]:
    """Drop cool-offs, no-edge streaks, and Gate-0 cache for a clean session."""
    with _lock:
        n_cool = len(_cool_until)
        n_streak = len(_no_edge_streak)
        n_ok = len(_safety_ok)
        n_bad = len(_safety_bad)
        _cool_until.clear()
        _cool_meta.clear()
        _no_edge_streak.clear()
        _safety_ok.clear()
        _safety_bad.clear()
    pipeline_log.emit(
        "discover",
        "rotation_clear",
        level="warning",
        cool_cleared=n_cool,
        streak_cleared=n_streak,
        safety_ok_cleared=n_ok,
        safety_bad_cleared=n_bad,
    )
    # Re-promote any cooled rows still sitting in the live buffer.
    if _token_source is not None and _token_sink is not None:
        try:
            for addr, tok in list((_token_source() or {}).items()):
                if not isinstance(tok, dict):
                    continue
                if tok.get("cooled") or tok.get("tradeable") is False:
                    patched = dict(tok)
                    patched["cooled"] = False
                    # Leave tradeable to the next scan; don't force trade without Gate 0.
                    if patched.get("discover"):
                        patched["tradeable"] = bool(patched.get("role") == "trade")
                    _token_sink(addr, patched)
        except Exception:  # noqa: BLE001
            pass
    return {
        "cool_cleared": n_cool,
        "streak_cleared": n_streak,
        "safety_ok_cleared": n_ok,
        "safety_bad_cleared": n_bad,
    }


def status() -> dict[str, Any]:
    now = time.time()
    cooling = []
    with _lock:
        for mint, until in list(_cool_until.items()):
            if until <= now:
                _cool_until.pop(mint, None)
                _cool_meta.pop(mint, None)
                continue
            meta = _cool_meta.get(mint) or {}
            cooling.append(
                {
                    "mint": mint,
                    "symbol": meta.get("symbol"),
                    "reason": meta.get("reason"),
                    "edge": meta.get("edge"),
                    "remaining_sec": int(until - now),
                }
            )
        return {
            "running": _state.running,
            "ticks": _state.ticks,
            "last_scan_at": _state.last_scan_at,
            "last_error": _state.last_error,
            "watchlist": list(_state.watchlist),
            "watchlist_count": len(_state.watchlist),
            "last_candidates": list(_state.last_candidates)[:40],
            "rejected_recent": list(_state.rejected)[-20:],
            "cooling": cooling[:30],
            "cooling_count": len(cooling),
            "limits": {
                "interval_sec": INTERVAL_SEC,
                "max_candidates": MAX_CANDIDATES,
                "max_observe": MAX_OBSERVE,
                "min_liquidity_usd": MIN_LIQUIDITY_USD,
                "min_volume_24h_usd": MIN_VOLUME_24H_USD,
                "min_age_min": MIN_AGE_MIN,
                "max_age_days": MAX_AGE_DAYS,
                "no_edge_streak": NO_EDGE_STREAK,
                "no_edge_cooldown_sec": NO_EDGE_COOLDOWN_SEC,
                "explore_multiplier": EXPLORE_MULTIPLIER,
            },
            "enabled_env": DISCOVER_ENABLED,
        }


def note_decision(card: Any) -> None:
    """Swarm calls this after each decide so no-edge names rotate out.

    Consecutive Gate-2 holds (or clearly negative edge) park the mint for
    ``NO_EDGE_COOLDOWN_SEC``. A buy or cleared edge resets the streak.
    """
    tok = getattr(card, "token", None) or {}
    if not isinstance(tok, dict):
        return
    mint = (tok.get("mint") or "").strip()
    if not mint:
        return
    symbol = tok.get("name")
    action = getattr(getattr(card, "action", None), "value", None) or str(
        getattr(card, "action", "") or ""
    )
    edge = getattr(card, "cost_adjusted_edge_pct", None)
    failed = list(getattr(card, "gates_failed", None) or [])
    no_edge = "gate2" in failed or (
        action == "hold" and edge is not None and float(edge) < 0.0
    )

    if action == "buy" or (edge is not None and float(edge) >= 0.0 and "gate2" not in failed):
        _no_edge_streak.pop(mint, None)
        # Successful edge path — lift any leftover cool-down early.
        if mint in _cool_until:
            _cool_until.pop(mint, None)
            _cool_meta.pop(mint, None)
            pipeline_log.emit(
                "discover",
                "cooldown_clear",
                mint=mint,
                symbol=symbol,
                edge=edge,
                action=action,
            )
        return

    if not no_edge:
        return

    streak = _no_edge_streak.get(mint, 0) + 1
    _no_edge_streak[mint] = streak
    if streak < NO_EDGE_STREAK:
        pipeline_log.emit(
            "discover",
            "no_edge_streak",
            mint=mint,
            symbol=symbol,
            edge=edge,
            streak=streak,
            need=NO_EDGE_STREAK,
        )
        return

    _park_mint(
        mint,
        symbol=symbol,
        reason="gate2_no_edge",
        edge=float(edge) if edge is not None else None,
    )


def _park_mint(
    mint: str,
    *,
    symbol: Optional[str],
    reason: str,
    edge: Optional[float],
) -> None:
    # Timer park is optional. When cooldown is 0, keep evaluating — do not
    # hide the mint behind a clock if a later decide might find edge.
    if NO_EDGE_COOLDOWN_SEC <= 0:
        _no_edge_streak.pop(mint, None)
        pipeline_log.emit(
            "discover",
            "no_edge_keep_evaluating",
            mint=mint,
            symbol=symbol,
            reason=reason,
            edge=edge,
        )
        return

    until = time.time() + NO_EDGE_COOLDOWN_SEC
    _cool_until[mint] = until
    _cool_meta[mint] = {
        "symbol": symbol,
        "reason": reason,
        "edge": edge,
        "until": until,
    }
    _no_edge_streak.pop(mint, None)
    pipeline_log.emit(
        "discover",
        "cooldown",
        level="warning",
        mint=mint,
        symbol=symbol,
        reason=reason,
        edge=edge,
        cooldown_sec=NO_EDGE_COOLDOWN_SEC,
    )
    # Demote from trade roster but keep candles on the dashboard for insight.
    _demote_mint_from_trade(mint)


def _is_cooling(mint: str) -> bool:
    until = _cool_until.get(mint)
    if until is None:
        return False
    if time.time() >= until:
        _cool_until.pop(mint, None)
        _cool_meta.pop(mint, None)
        return False
    return True


def _demote_mint_from_trade(mint: str) -> None:
    if _token_source is None:
        return
    tokens = _token_source() or {}
    for addr, tok in list(tokens.items()):
        if not isinstance(tok, dict) or not tok.get("discover"):
            continue
        if (tok.get("mint") or "") != mint:
            continue
        tok["tradeable"] = False
        tok["cooled"] = True
        tok["role"] = "observe"
        pipeline_log.emit(
            "discover",
            "demote",
            mint=mint,
            address=addr,
            symbol=tok.get("name"),
        )


def start(*, interval_s: Optional[float] = None) -> dict[str, Any]:
    global _thread
    if _token_sink is None:
        return {**status(), "error": "discover not configured (call configure first)"}
    with _lock:
        if _thread and _thread.is_alive():
            return status()
        _stop.clear()
        _state.running = True
        _state.last_error = None
        period = float(interval_s if interval_s is not None else INTERVAL_SEC)
        _thread = threading.Thread(
            target=_run,
            args=(period,),
            name="token-discover",
            daemon=True,
        )
        _thread.start()
    pipeline_log.emit("discover", "start", interval_s=period)
    return status()


def stop() -> dict[str, Any]:
    _stop.set()
    with _lock:
        _state.running = False
    pipeline_log.emit("discover", "stop")
    return status()


def scan_once() -> dict[str, Any]:
    """Synchronous one-shot scan (also used by the background loop)."""
    if _token_sink is None:
        return {"status": "error", "error": "discover not configured"}
    started = time.perf_counter()
    with pipeline_log.run(prefix="dsc-") as run_id:
        pipeline_log.emit("discover", "scan_start")
        try:
            result = _scan_and_hydrate()
            result["run_id"] = run_id
            with _lock:
                _state.ticks += 1
                _state.last_scan_at = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                _state.last_error = None
                _state.last_candidates = result.get("candidates") or []
                _state.watchlist = result.get("watchlist") or []
                if result.get("rejected"):
                    _state.rejected.extend(result["rejected"])
                    _state.rejected = _state.rejected[-50:]
            pipeline_log.emit(
                "discover",
                "scan_done",
                duration_ms=pipeline_log.timed_ms(started),
                scanned=result.get("scanned"),
                passed_prefilter=result.get("passed_prefilter"),
                watchlist_n=len(result.get("watchlist") or []),
                hydrated_n=len(result.get("candidates") or []),
                rejected_n=len(result.get("rejected") or []),
                symbols=[c.get("symbol") for c in (result.get("candidates") or [])],
            )
            return result
        except Exception as exc:  # noqa: BLE001
            with _lock:
                _state.last_error = str(exc)
            pipeline_log.emit(
                "discover",
                "scan_error",
                level="error",
                reason=str(exc),
                duration_ms=pipeline_log.timed_ms(started),
            )
            return {"status": "error", "error": str(exc), "run_id": run_id}


def _run(interval_s: float) -> None:
    while not _stop.is_set():
        scan_once()
        _stop.wait(interval_s)
    with _lock:
        _state.running = False


# --- scan / filter / hydrate -------------------------------------------------


def scan_candidates() -> list[Candidate]:
    """Merge boost + trending feeds into enriched Candidate rows (pre-filter)."""
    by_pool: dict[str, Candidate] = {}

    for which in ("latest", "top"):
        try:
            boosts = fetch_dexscreener_boosts(which=which)
        except SourceError:
            continue
        for row in boosts:
            mint = (row.get("tokenAddress") or "").strip()
            if not mint:
                continue
            boost_amt = float(row.get("totalAmount") or row.get("amount") or 0.0)
            cand = _candidate_from_mint(
                mint,
                source=f"dex_boost_{which}",
                boost_amount=boost_amt,
            )
            if cand is None:
                continue
            prev = by_pool.get(cand.pool)
            if prev is None or cand.boost_amount > prev.boost_amount:
                by_pool[cand.pool] = cand

    try:
        pools = fetch_gecko_trending_pools(page=1)
    except SourceError:
        pools = []
    for row in pools:
        cand = _candidate_from_gecko(row)
        if cand is None:
            continue
        prev = by_pool.get(cand.pool)
        if prev is None:
            by_pool[cand.pool] = cand
        else:
            # Keep boost amount; refresh liquidity/volume from gecko if stronger.
            if cand.volume_24h_usd > prev.volume_24h_usd:
                prev.volume_24h_usd = cand.volume_24h_usd
                prev.liquidity_usd = max(prev.liquidity_usd, cand.liquidity_usd)
                prev.tx_h1 = max(prev.tx_h1, cand.tx_h1)
            if "gecko" not in prev.source:
                prev.source = f"{prev.source}+gecko_trending"

    return list(by_pool.values())


def prefilter(candidates: list[Candidate]) -> list[Candidate]:
    now_ms = time.time() * 1000.0
    kept: list[Candidate] = []
    for c in candidates:
        reason = _reject_reason(c, now_ms=now_ms)
        if reason:
            c.reject_reason = reason
            continue
        c.score = _score(c)
        kept.append(c)
    kept.sort(key=lambda x: x.score, reverse=True)
    return kept


def _reject_reason(c: Candidate, *, now_ms: float) -> Optional[str]:
    if not c.mint or not c.pool:
        return "missing mint/pool"
    if c.mint in QUOTE_MINTS:
        return "base is quote/stable mint"
    if (c.symbol or "").upper() in STABLE_BASE_SYMBOLS:
        return f"stable base symbol {c.symbol}"
    if c.liquidity_usd < MIN_LIQUIDITY_USD:
        return f"liquidity ${c.liquidity_usd:.0f} < ${MIN_LIQUIDITY_USD:.0f}"
    if c.volume_24h_usd < MIN_VOLUME_24H_USD:
        return f"volume24h ${c.volume_24h_usd:.0f} < ${MIN_VOLUME_24H_USD:.0f}"
    age_min = c.age_minutes(now_ms)
    if age_min is not None:
        if age_min < MIN_AGE_MIN:
            return f"pair age {age_min:.0f}m < {MIN_AGE_MIN:.0f}m"
        if age_min > MAX_AGE_DAYS * 24 * 60:
            return f"pair age {age_min / 1440:.1f}d > {MAX_AGE_DAYS:.0f}d"
    return None


def _score(c: Candidate) -> float:
    turnover = (c.volume_24h_usd / c.liquidity_usd) if c.liquidity_usd > 0 else 0.0
    boost = min(c.boost_amount / 100.0, 50.0)
    tx = min(c.tx_h1 / 50.0, 20.0)
    return turnover + boost + tx


def _scan_and_hydrate() -> dict[str, Any]:
    raw = scan_candidates()
    filtered = prefilter(raw)
    rejected = [
        {"mint": c.mint, "pool": c.pool, "symbol": c.symbol, "reason": c.reject_reason}
        for c in raw
        if c.reject_reason
    ]
    reason_counts: dict[str, int] = {}
    for c in raw:
        if not c.reject_reason:
            continue
        key = c.reject_reason.split(" < ")[0].split(" > ")[0][:48]
        reason_counts[key] = reason_counts.get(key, 0) + 1
    if reason_counts:
        pipeline_log.emit(
            "discover",
            "prefilter_summary",
            rejected_n=len(rejected),
            reasons=reason_counts,
        )

    explore_cap = max(MAX_OBSERVE * max(1, EXPLORE_MULTIPLIER), MAX_OBSERVE)
    cooled_skip: list[str] = []
    eligible: list[Candidate] = []
    cooled_observe: list[Candidate] = []
    seen: set[str] = set()
    for c in filtered[:explore_cap]:
        if c.mint in seen:
            continue
        seen.add(c.mint)
        if _is_cooling(c.mint):
            cooled_skip.append(c.symbol or c.mint[:8])
            cooled_observe.append(c)
            continue
        eligible.append(c)

    pipeline_log.emit(
        "discover",
        "shortlist",
        scanned=len(raw),
        passed_prefilter=len(filtered),
        eligible_n=len(eligible),
        cooled_skipped=len(cooled_skip),
        cooled_symbols=cooled_skip[:12],
        symbols=[c.symbol for c in eligible[:MAX_CANDIDATES]],
    )

    watchlist: list[str] = []
    hydrated: list[dict[str, Any]] = []
    safety_rejected: list[dict[str, Any]] = []
    gate0_blocked: set[str] = set()
    ohlcv_budget_hit = False

    def _push_token(
        c: Candidate,
        *,
        role: str,
        candles: list[dict[str, Any]],
        lite: bool = False,
    ) -> None:
        tradeable = role == "trade" and not _is_cooling(c.mint)
        token = ohlcv_remote.build_live_token(
            name=c.symbol or c.name,
            mint=c.mint,
            pool_address=c.pool,
            candles=candles,
            extra={
                "discover_score": c.score,
                "discover_source": c.source,
                "liquidity_usd": c.liquidity_usd,
                "volume_24h_usd": c.volume_24h_usd,
                "price_usd": c.price_usd,
                "role": role,
                "tradeable": tradeable,
                "cooled": _is_cooling(c.mint),
                "observe_lite": lite,
            },
        )
        assert _token_sink is not None
        _token_sink(c.pool, token)
        watchlist.append(c.pool)
        hydrated.append(
            {
                "mint": c.mint,
                "pool": c.pool,
                "symbol": c.symbol,
                "score": round(c.score, 3),
                "source": c.source,
                "bars": len(candles),
                "liquidity_usd": c.liquidity_usd,
                "volume_24h_usd": c.volume_24h_usd,
                "role": role,
                "tradeable": tradeable,
                "observe_lite": lite,
            }
        )
        pipeline_log.emit(
            "ohlcv",
            "hydrate",
            mint=c.mint,
            address=c.pool,
            symbol=c.symbol,
            bars=len(candles),
            score=round(c.score, 3),
            source=c.source,
            role=role,
            tradeable=tradeable,
            lite=lite,
            liquidity_usd=c.liquidity_usd,
            volume_24h_usd=c.volume_24h_usd,
        )

    def _hydrate_trade(c: Candidate) -> bool:
        nonlocal ohlcv_budget_hit
        gate = _gate0_ok(c.mint)
        if gate is not True:
            reason = gate if isinstance(gate, str) else "gate0_block"
            gate0_blocked.add(c.mint)
            safety_rejected.append(
                {
                    "mint": c.mint,
                    "pool": c.pool,
                    "symbol": c.symbol,
                    "reason": reason,
                }
            )
            pipeline_log.emit(
                "discover",
                "gate0_reject",
                level="warning",
                mint=c.mint,
                address=c.pool,
                symbol=c.symbol,
                reason=reason,
            )
            return False
        if ohlcv_budget_hit:
            return False
        try:
            candles = ohlcv_remote.fetch_pool_ohlcv(c.pool, aggregate=1, limit=300)
        except SourceError as exc:
            msg = str(exc)
            safety_rejected.append(
                {
                    "mint": c.mint,
                    "pool": c.pool,
                    "symbol": c.symbol,
                    "reason": f"ohlcv: {exc}",
                }
            )
            pipeline_log.emit(
                "ohlcv",
                "fail",
                level="warning",
                mint=c.mint,
                address=c.pool,
                symbol=c.symbol,
                reason=msg,
            )
            if "429" in msg or "Too Many" in msg:
                ohlcv_budget_hit = True
                pipeline_log.emit(
                    "ohlcv",
                    "rate_limit",
                    level="warning",
                    reason="stopping further gecko ohlcv this scan",
                )
            return False
        _push_token(c, role="trade", candles=candles, lite=False)
        return True

    def _hydrate_observe_lite(c: Candidate) -> bool:
        """Dashboard volume without burning Gecko OHLCV quota."""
        price = c.price_usd
        if price is None or price <= 0:
            # Last resort: one DexScreener pair lookup is cheaper than gecko ohlcv.
            try:
                pairs = fetch_dexscreener_pairs(c.mint)
                best = _best_pair(pairs, preferred_mint=c.mint)
                if best:
                    try:
                        price = float(best.get("priceUsd") or 0.0) or None
                    except (TypeError, ValueError):
                        price = None
                    if price:
                        c.price_usd = price
            except SourceError:
                price = None
        if price is None or price <= 0:
            return False
        candles = ohlcv_remote.stub_candles_from_price(price, bars=32, step_sec=60)
        _push_token(c, role="observe", candles=candles, lite=True)
        return True

    # Walk eligible until trade roster is full (Gate 0 + OHLCV may knock some out).
    cursor = 0
    while sum(1 for h in hydrated if h.get("role") == "trade") < MAX_CANDIDATES and cursor < len(
        eligible
    ):
        c = eligible[cursor]
        cursor += 1
        _hydrate_trade(c)
        if ohlcv_budget_hit and sum(1 for h in hydrated if h.get("role") == "trade") > 0:
            # Keep what we have; fill observe with lite stubs instead of more 429s.
            break

    # Observe = cooled + remaining eligible (lite stubs — no Gecko OHLCV).
    observe_targets: list[Candidate] = list(cooled_observe)
    tried_mints = {h["mint"] for h in hydrated} | gate0_blocked
    for c in eligible:
        if len(observe_targets) >= MAX_OBSERVE:
            break
        if c.mint in tried_mints or any(x.mint == c.mint for x in observe_targets):
            continue
        observe_targets.append(c)
    for c in filtered:
        if len(observe_targets) >= MAX_OBSERVE:
            break
        if c.mint in tried_mints or any(x.mint == c.mint for x in observe_targets):
            continue
        observe_targets.append(c)

    for c in observe_targets:
        if sum(1 for h in hydrated if h.get("role") == "observe") >= MAX_OBSERVE:
            break
        if c.mint in gate0_blocked or any(h["mint"] == c.mint for h in hydrated):
            continue
        _hydrate_observe_lite(c)

    _prune_stale_discover(watchlist)

    return {
        "status": "ok",
        "scanned": len(raw),
        "passed_prefilter": len(filtered),
        "watchlist": watchlist,
        "candidates": hydrated,
        "trade_n": sum(1 for h in hydrated if h.get("role") == "trade"),
        "observe_n": sum(1 for h in hydrated if h.get("role") == "observe"),
        "ohlcv_rate_limited": ohlcv_budget_hit,
        "rejected": rejected[:30] + safety_rejected,
        "cooled_skipped": cooled_skip,
    }


def _gate0_ok(mint: str) -> bool | str:
    now = time.time()
    bad = _safety_bad.get(mint)
    if bad and now - bad[0] < SAFETY_CACHE_SEC:
        return bad[1]
    ok_ts = _safety_ok.get(mint)
    if ok_ts and now - ok_ts < SAFETY_CACHE_SEC:
        return True
    try:
        report = check_token(mint)
    except Exception as exc:  # noqa: BLE001
        reason = f"gate0_error: {exc}"
        _safety_bad[mint] = (now, reason)
        return reason
    if report.blocking:
        reason = f"gate0_{report.verdict.value}: {report.summary()}"
        _safety_bad[mint] = (now, reason)
        return reason
    # caution + safe both proceed; danger/unknown block via .blocking
    if report.verdict == SafetyVerdict.DANGER:
        reason = report.summary()
        _safety_bad[mint] = (now, reason)
        return reason
    _safety_ok[mint] = now
    return True


def _prune_stale_discover(keep_pools: list[str]) -> None:
    """Drop discover-sourced tokens no longer on the watchlist (keep open paper)."""
    if _token_source is None:
        return
    keep = set(keep_pools)
    tokens = _token_source() or {}
    from decision import paper

    open_addrs: set[str] = set()
    for row in paper.snapshot().get("open") or []:
        if isinstance(row, dict) and row.get("address"):
            open_addrs.add(str(row["address"]))

    for addr, tok in list(tokens.items()):
        if not isinstance(tok, dict) or not tok.get("discover"):
            continue
        if addr in keep or addr in open_addrs:
            continue
        tokens.pop(addr, None)


def _candidate_from_mint(
    mint: str,
    *,
    source: str,
    boost_amount: float = 0.0,
) -> Optional[Candidate]:
    try:
        pairs = fetch_dexscreener_pairs(mint)
    except SourceError:
        return None
    pair = _best_pair(pairs, preferred_mint=mint)
    if not pair:
        return None
    return _candidate_from_dex_pair(pair, source=source, boost_amount=boost_amount)


def _candidate_from_gecko(row: dict[str, Any]) -> Optional[Candidate]:
    attrs = row.get("attributes") or {}
    pool = (attrs.get("address") or "").strip()
    if not pool:
        return None
    rel = row.get("relationships") or {}
    base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id") or ""
    quote_id = ((rel.get("quote_token") or {}).get("data") or {}).get("id") or ""
    mint = base_id.split("_", 1)[-1] if base_id else ""
    quote = quote_id.split("_", 1)[-1] if quote_id else ""
    if quote and quote not in QUOTE_MINTS:
        # Prefer the non-quote side if gecko flipped base/quote.
        if mint in QUOTE_MINTS and quote not in QUOTE_MINTS:
            mint, quote = quote, mint
        elif quote not in QUOTE_MINTS and mint not in QUOTE_MINTS:
            pass  # exotic pair; keep base
    name = attrs.get("name") or ""
    symbol = name.split("/")[0].strip() if name else ""
    try:
        liq = float(attrs.get("reserve_in_usd") or 0.0)
    except (TypeError, ValueError):
        liq = 0.0
    vol = attrs.get("volume_usd") or {}
    try:
        vol24 = float(vol.get("h24") or 0.0)
    except (TypeError, ValueError):
        vol24 = 0.0
    tx = attrs.get("transactions") or {}
    h1 = tx.get("h1") or {}
    try:
        tx_h1 = int(h1.get("buys") or 0) + int(h1.get("sells") or 0)
    except (TypeError, ValueError):
        tx_h1 = 0
    created = attrs.get("pool_created_at")
    created_ms: Optional[float] = None
    if isinstance(created, str) and created:
        try:
            created_ms = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() * 1000.0
        except ValueError:
            created_ms = None
    try:
        gecko_price = float(attrs.get("base_token_price_usd") or 0.0) or None
    except (TypeError, ValueError):
        gecko_price = None

    # Prefer DexScreener enrichment when available (consistent liquidity/age).
    enriched = _candidate_from_mint(mint, source="gecko_trending") if mint else None
    if enriched is not None:
        enriched.source = "gecko_trending"
        enriched.tx_h1 = max(enriched.tx_h1, tx_h1)
        if enriched.liquidity_usd <= 0:
            enriched.liquidity_usd = liq
        if enriched.volume_24h_usd <= 0:
            enriched.volume_24h_usd = vol24
        if enriched.price_usd is None:
            enriched.price_usd = gecko_price
        return enriched

    if not mint:
        return None
    return Candidate(
        mint=mint,
        pool=pool,
        symbol=symbol,
        name=name,
        liquidity_usd=liq,
        volume_24h_usd=vol24,
        pair_created_at_ms=created_ms,
        tx_h1=tx_h1,
        source="gecko_trending",
        price_usd=gecko_price,
    )


def _candidate_from_dex_pair(
    pair: dict[str, Any],
    *,
    source: str,
    boost_amount: float = 0.0,
) -> Optional[Candidate]:
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    base_addr = base.get("address") or ""
    quote_addr = quote.get("address") or ""
    if quote_addr not in QUOTE_MINTS and base_addr in QUOTE_MINTS:
        # Mint is on quote side — swap view.
        base, quote = quote, base
        base_addr, quote_addr = quote_addr, base_addr
    if quote_addr and quote_addr not in QUOTE_MINTS:
        return None
    pool = pair.get("pairAddress") or ""
    if not base_addr or not pool:
        return None
    liq = float(((pair.get("liquidity") or {}).get("usd")) or 0.0)
    vol24 = float(((pair.get("volume") or {}).get("h24")) or 0.0)
    created = pair.get("pairCreatedAt")
    try:
        created_ms = float(created) if created is not None else None
    except (TypeError, ValueError):
        created_ms = None
    tx = pair.get("txns") or {}
    h1 = tx.get("h1") or {}
    try:
        tx_h1 = int(h1.get("buys") or 0) + int(h1.get("sells") or 0)
    except (TypeError, ValueError):
        tx_h1 = 0
    try:
        price = float(pair.get("priceUsd") or 0.0) or None
    except (TypeError, ValueError):
        price = None
    return Candidate(
        mint=base_addr,
        pool=pool,
        symbol=base.get("symbol") or "",
        name=base.get("name") or "",
        liquidity_usd=liq,
        volume_24h_usd=vol24,
        pair_created_at_ms=created_ms,
        boost_amount=boost_amount,
        tx_h1=tx_h1,
        source=source,
        price_usd=price,
    )


def _best_pair(
    pairs: list[dict[str, Any]],
    *,
    preferred_mint: str,
) -> Optional[dict[str, Any]]:
    """Pick the deepest Solana pool where preferred_mint is the non-quote side."""
    scored: list[tuple[float, dict[str, Any]]] = []
    for pair in pairs:
        if pair.get("chainId") and pair.get("chainId") != "solana":
            continue
        base = (pair.get("baseToken") or {}).get("address")
        quote = (pair.get("quoteToken") or {}).get("address")
        if preferred_mint not in (base, quote):
            continue
        other = quote if base == preferred_mint else base
        if other and other not in QUOTE_MINTS:
            continue
        liq = float(((pair.get("liquidity") or {}).get("usd")) or 0.0)
        scored.append((liq, pair))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def candidate_as_dict(c: Candidate) -> dict[str, Any]:
    return asdict(c)
