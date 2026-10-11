"""Paper launch + cluster + create-event sniper.

Runner for the isolated engines under ``decision.engines``:
cluster, launch (this file), sniper (``engines.sniper``), hold (labs).
Public events only: Pump.fun ``Create`` / ``Trade`` logs (already landed)
plus Gecko new_pools and unique-buyer clusters. Paper only — no live swaps,
no wash volume, no pending-tx front-running, no create+buy bundles.

Launch: Gecko ``new_pools`` younger than ``LAUNCH_MAX_AGE_MIN``.
Cluster: unique buyers on that mint (Helius) and/or a watchlist of wallets.
Sniper: watch each landed create; buy only when the curve tape lifts
(other buyers / SOL in the curve). Hold follows the tape — scratch a dead
print, stay with a runner, hard-cap a stall.
"""

from __future__ import annotations

import functools
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from decision import paper
from decision import pipeline_log
from decision import pricefeed
from decision import sizing as sizing_mod
from decision import labs
from decision.config import SETTINGS, SOL_USD_FALLBACK, WRAPPED_SOL_MINT, _env_float, _env_int
from decision.engines.sniper import feed as snipe_feed
from decision.engines.sniper import desk as snipe_desk
from decision.engines.sniper.pumpfun import (
    CreatorBook,
    curve_gave_back,
    initial_price_usd,
    snipe_entry,
    snipe_lift_pct,
    snipe_size,
)
from decision.safety import check_token
from decision.schema import SafetyVerdict
from decision.sources import (
    SourceError,
    fetch_dexscreener_pairs,
    fetch_gecko_pool_list,
    fetch_helius_transactions,
    fetch_mint_account,
)

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTE_MINTS = frozenset({WRAPPED_SOL_MINT, USDC, USDT})

LAUNCH_ENABLED = os.getenv("LAUNCH_ENABLED", "0").strip() == "1"
CLUSTER_ENABLED = os.getenv("CLUSTER_ENABLED", "1").strip() == "1"
INTERVAL_SEC = _env_float("TRENCHES_INTERVAL_SEC", 15.0)
LAUNCH_MAX_AGE_MIN = _env_float("LAUNCH_MAX_AGE_MIN", 12.0)
LAUNCH_MIN_LIQ_USD = _env_float("LAUNCH_MIN_LIQ_USD", 8_000.0)
LAUNCH_MIN_BUYERS = _env_int("LAUNCH_MIN_BUYERS", 2)
LAUNCH_SIZE_USD = _env_float("LAUNCH_SIZE_USD", 80.0)
LAUNCH_MAX_HOLD_SEC = _env_float("LAUNCH_MAX_HOLD_SEC", 180.0)
# Skip the fill when Gecko and Dex disagree by more than this — that gap was
# booking an instant paper stop on every new pool.
MAX_PRICE_DIVERGE_PCT = _env_float("TRENCHES_MAX_PRICE_DIVERGE_PCT", 8.0)
CLUSTER_MIN_WALLETS = _env_int("CLUSTER_MIN_WALLETS", 3)
CLUSTER_WINDOW_SEC = _env_float("CLUSTER_WINDOW_SEC", 90.0)
CLUSTER_SIZE_USD = _env_float("CLUSTER_SIZE_USD", 80.0)
CLUSTER_MAX_HOLD_SEC = _env_float("CLUSTER_MAX_HOLD_SEC", 300.0)
# Scratch if never green by this many seconds (0 = off). Early dead names
# were the whole −EV; survivors past ~5m were +EV.
CLUSTER_DEAD_SEC = _env_float("CLUSTER_DEAD_SEC", 0.0)
# Explicit dollar stop for cluster (0 = paper percent default). Prefer a
# tight clip over giving losers room — wider stops made v2 worse.
CLUSTER_STOP_USD = _env_float("CLUSTER_STOP_USD", 0.0)
# Require DexScreener 5m % change ≥ this before opening (0 = off).
CLUSTER_MIN_M5_PCT = _env_float("CLUSTER_MIN_M5_PCT", 0.0)
# Refuse parabolic blowoffs above this 5m % (0 = off). Historical in-sample
# note (not out-of-sample evidence): m5>150% entries averaged -$16.4/trade over
# 118 closes (instant rugs); the edge estimate (m5 minus costs) otherwise reads
# parabolas as attractive.
CLUSTER_MAX_M5_PCT = _env_float("CLUSTER_MAX_M5_PCT", 150.0)
# Copy-trading pilot: mirror tracked-leader buys as their own lane.
# Leaders are watched wallets (CLUSTER_WALLETS); their sells already
# mirror-close our lots. Entries need COPY_MIN_LEADERS distinct leaders
# buying the mint inside COPY_WINDOW_SEC, then pass the same screens
# (Gate 0, m5 bounds, JEV chair) as cluster opens.
COPY_ENABLED = os.getenv("COPY_ENABLED", "1").strip() == "1"
COPY_MIN_LEADERS = _env_int("COPY_MIN_LEADERS", 1)
COPY_WINDOW_SEC = _env_float("COPY_WINDOW_SEC", 120.0)
COPY_SIZE_USD = _env_float("COPY_SIZE_USD", 40.0)
COPY_MAX_HOLD_SEC = _env_float("COPY_MAX_HOLD_SEC", 300.0)
COPY_DEAD_SEC = _env_float("COPY_DEAD_SEC", 90.0)
COPY_STOP_USD = _env_float("COPY_STOP_USD", 1.60)
# Bonding-curve guard on opens: refuse when our size is the exit
# liquidity (moves curve > max impact) or dwarfs real pool depth.
# Pure constant-product math (decision.pump_curve), no RPC.
PUMP_CURVE_GUARD = os.getenv("PUMP_CURVE_GUARD", "1").strip() == "1"
PUMP_CURVE_MAX_IMPACT_PCT = _env_float("PUMP_CURVE_MAX_IMPACT_PCT", 10.0)
PUMP_CURVE_MAX_LIQ_FRAC = _env_float("PUMP_CURVE_MAX_LIQ_FRAC", 0.05)
# Exact on-chain curve reads (Helius getAccountInfo, cached). When a
# curve account resolves, its real reserves replace the Dex-anchored
# estimates and completed curves refuse outright. 0 = estimates only.
ONCHAIN_CURVE = os.getenv("ONCHAIN_CURVE", "1").strip() == "1"
CURVE_CACHE_SEC = _env_float("CURVE_CACHE_SEC", 300.0)
_curve_cache: dict[str, tuple[float, Optional[dict[str, Any]]]] = {}


def _curve_state(mint: str) -> Optional[dict[str, Any]]:
    """Exact on-chain curve state, cached; None when unresolvable."""
    if not ONCHAIN_CURVE:
        return None
    mint = (mint or "").strip()
    if not mint:
        return None
    now = time.time()
    hit = _curve_cache.get(mint)
    if hit and now - hit[0] < CURVE_CACHE_SEC:
        return hit[1]
    try:
        from decision import pump_onchain

        curve = pump_onchain.fetch_curve(mint)
    except Exception:  # noqa: BLE001
        curve = None
    _curve_cache[mint] = (now, curve)
    return curve
# JEV chair gate on trenches opens (0 = off, today's behavior, per
# strategy). Veto or avoid from the chair blocks the open; hold/buy (or
# an offline chair) proceeds. Blocks are remembered per mint so a
# rejected name is not re-asked every tick. A sniper proceed is reused
# for the watch window so both lanes and later ticks share one call.
# Arb fills stay out (their own veto-only gate): computed edge is never
# second-guessed there.
JEV_CLUSTER_GATE = os.getenv("JEV_CLUSTER_GATE", "1").strip() == "1"
JEV_LAUNCH_GATE = os.getenv("JEV_LAUNCH_GATE", "1").strip() == "1"
JEV_SNIPER_GATE = os.getenv("JEV_SNIPER_GATE", "1").strip() == "1"
JEV_COPY_GATE = os.getenv("JEV_COPY_GATE", "1").strip() == "1"
JEV_CLUSTER_RECHECK_SEC = _env_float("JEV_CLUSTER_RECHECK_SEC", 300.0)
# 0 = SNIPER_WATCH_SEC. Positive overrides how long a sniper proceed
# is reused without another chair call.
JEV_SNIPER_PROCEED_SEC = _env_float("JEV_SNIPER_PROCEED_SEC", 0.0)


def _strategy_gate_on(strategy: str) -> bool:
    return {
        "cluster": JEV_CLUSTER_GATE,
        "launch": JEV_LAUNCH_GATE,
        "snipe": JEV_SNIPER_GATE,
        "copy": JEV_COPY_GATE,
    }.get(strategy or "", False)


def _snipe_proceed_ttl() -> float:
    if JEV_SNIPER_PROCEED_SEC > 0:
        return JEV_SNIPER_PROCEED_SEC
    return SNIPER_WATCH_SEC


def _tape_conviction(lift_pct: float | None, buyers: int, real_sol: float = 0.0) -> float:
    """Tape-strength conviction tier, mirroring the snipe_size steps."""
    lift = lift_pct or 0.0
    if lift >= 25.0 and buyers >= 4:
        return 1.0
    if lift >= 12.0 or buyers >= 3 or real_sol >= 5.0:
        return 0.75
    return 0.5


def _m5_conviction(m5_pct: float | None) -> float:
    if m5_pct is None:
        return 0.6
    if m5_pct >= 15.0:
        return 1.0
    if m5_pct >= 8.0:
        return 0.75
    return 0.5


def _size_for_open(
    *,
    strategy: str,
    base_risk_usd: float,
    flat_size_usd: float,
    flat_stop_usd: float | None,
    row: dict[str, Any],
    safety_report: Optional[Any],
    vol_proxy: float,
    conviction_mult: float,
) -> Any:
    """One shared risk model for every trenches lane (sizing.py)."""
    score, impact = sizing_mod.report_features(safety_report)
    return sizing_mod.plan_size(
        strategy=strategy,
        base_risk_usd=base_risk_usd,
        flat_size_usd=flat_size_usd,
        flat_stop_usd=flat_stop_usd,
        liquidity_usd=row.get("liquidity_usd"),
        price_impact_pct=impact,
        safety_score=score,
        vol_proxy=vol_proxy,
        conviction_mult=conviction_mult,
    )


_jev_blocked: dict[str, float] = {}
# mint → (ts, reason) for a real sniper proceed. Offline/error is not cached.
_jev_proceed: dict[str, tuple[float, str]] = {}
MAX_PER_TICK = _env_int("TRENCHES_MAX_PER_TICK", 2)
# Create-event sniper: watch the landed create, buy only if the tape lifts.
SNIPER_ENABLED = os.getenv("SNIPER_ENABLED", "1").strip() == "1"
SNIPER_INTERVAL_SEC = _env_float("SNIPER_INTERVAL_SEC", 3.0)
SNIPER_MAX_AGE_SEC = _env_float("SNIPER_MAX_AGE_SEC", 90.0)
SNIPER_WATCH_SEC = _env_float("SNIPER_WATCH_SEC", 45.0)
SNIPER_MIN_BUYERS = _env_int("SNIPER_MIN_BUYERS", 4)
SNIPER_MIN_LIFT_PCT = _env_float("SNIPER_MIN_LIFT_PCT", 12.0)
SNIPER_FAST_LIFT_PCT = _env_float("SNIPER_FAST_LIFT_PCT", 25.0)
SNIPER_MIN_REAL_SOL = _env_float("SNIPER_MIN_REAL_SOL", 10.0)
SNIPER_MIN_LIQ_USD = _env_float("SNIPER_MIN_LIQ_USD", 1_500.0)
SNIPER_SIZE_USD = _env_float("SNIPER_SIZE_USD", 20.0)
SNIPER_TARGET_USD = _env_float("SNIPER_TARGET_USD", 1.0)
SNIPER_MAX_HOLD_SEC = _env_float("SNIPER_MAX_HOLD_SEC", 45.0)
SNIPER_DEAD_SEC = _env_float("SNIPER_DEAD_SEC", 6.0)
SNIPER_ABS_HOLD_SEC = _env_float("SNIPER_ABS_HOLD_SEC", 90.0)
SNIPER_TARGET_PCT = _env_float("SNIPER_TARGET_PCT", 40.0)
SNIPER_MAX_PER_TICK = _env_int("SNIPER_MAX_PER_TICK", 1)
SNIPER_CURVE_DROP_PCT = _env_float("SNIPER_CURVE_DROP_PCT", 50.0)
SNIPER_STOP_USD = _env_float("SNIPER_STOP_USD", 0.80)
SNIPER_LISTEN = os.getenv("SNIPER_LISTEN", "1").strip() == "1"
_SNIPE_DEATH_EXTS = frozenset(
    {"nonTransferable", "permanentDelegate", "transferHook", "pausableConfig"}
)


def _wallets() -> list[str]:
    raw = os.getenv("CLUSTER_WALLETS", "")
    return [w.strip() for w in raw.split(",") if len(w.strip()) >= 32]


@dataclass
class TrenchState:
    running: bool = False
    ticks: int = 0
    last_tick_at: Optional[str] = None
    last_error: Optional[str] = None
    launch_seen: int = 0
    launch_opens: int = 0
    cluster_fires: int = 0
    cluster_opens: int = 0
    cluster_exits: int = 0
    copy_fires: int = 0
    copy_opens: int = 0
    sniper_seen: int = 0
    sniper_opens: int = 0
    sniper_expired: int = 0
    skipped: int = 0
    last_hits: list[dict[str, Any]] = field(default_factory=list)


_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_state = TrenchState()
_token_sink: Optional[Callable[[str, dict[str, Any]], None]] = None
_seen_mints: set[str] = set()
# mint → last curve/Gecko mid, used to mark sniper lots before Dex lists the pair.
_snipe_marks: dict[str, float] = {}
# mint → watch started at create; buy only after the tape confirms.
_watches: dict[str, dict[str, Any]] = {}
# mint → {wallet: last_ts}
_buys: dict[str, dict[str, float]] = {}
_sells: dict[str, dict[str, float]] = {}
_creators = CreatorBook()


def configure(*, set_token: Optional[Callable[[str, dict[str, Any]], None]] = None) -> None:
    global _token_sink
    _token_sink = set_token


def reset_counters() -> dict[str, Any]:
    with _lock:
        _state.ticks = 0
        _state.last_tick_at = None
        _state.last_error = None
        _state.launch_seen = 0
        _state.launch_opens = 0
        _state.cluster_fires = 0
        _state.cluster_opens = 0
        _state.cluster_exits = 0
        _state.copy_fires = 0
        _state.copy_opens = 0
        _state.sniper_seen = 0
        _state.sniper_opens = 0
        _state.sniper_expired = 0
        _state.skipped = 0
        _state.last_hits = []
        _seen_mints.clear()
        _jev_blocked.clear()
        _jev_proceed.clear()
        _swaps_cache.clear()
        _curve_cache.clear()
        _snipe_marks.clear()
        _watches.clear()
        _buys.clear()
        _sells.clear()
        _creators.clear()
    try:
        from decision import labs

        labs.clear_seen()
    except Exception:
        pass
    pipeline_log.emit("trenches", "reset", level="warning")
    return status()


def status() -> dict[str, Any]:
    with _lock:
        return {
            "running": _state.running,
            "ticks": _state.ticks,
            "last_tick_at": _state.last_tick_at,
            "last_error": _state.last_error,
            "launch_seen": _state.launch_seen,
            "launch_opens": _state.launch_opens,
            "cluster_fires": _state.cluster_fires,
            "cluster_opens": _state.cluster_opens,
            "cluster_exits": _state.cluster_exits,
            "copy_fires": _state.copy_fires,
            "copy_opens": _state.copy_opens,
            "sniper_seen": _state.sniper_seen,
            "sniper_opens": _state.sniper_opens,
            "sniper_expired": _state.sniper_expired,
            "sniper_watching": len(_watches),
            "skipped": _state.skipped,
            "last_hits": list(_state.last_hits)[:12],
            "wallets_n": len(_wallets()),
            "helius": bool(SETTINGS.helius_api_keys),
            "limits": {
                "interval_sec": INTERVAL_SEC,
                "launch_max_age_min": LAUNCH_MAX_AGE_MIN,
                "launch_min_liq_usd": LAUNCH_MIN_LIQ_USD,
                "launch_min_buyers": LAUNCH_MIN_BUYERS,
                "max_price_diverge_pct": MAX_PRICE_DIVERGE_PCT,
                "launch_size_usd": LAUNCH_SIZE_USD,
                "launch_max_hold_sec": LAUNCH_MAX_HOLD_SEC,
                "cluster_min_wallets": CLUSTER_MIN_WALLETS,
                "cluster_window_sec": CLUSTER_WINDOW_SEC,
                "cluster_size_usd": CLUSTER_SIZE_USD,
                "cluster_max_hold_sec": CLUSTER_MAX_HOLD_SEC,
                "cluster_dead_sec": CLUSTER_DEAD_SEC,
                "cluster_stop_usd": CLUSTER_STOP_USD,
                "cluster_min_m5_pct": CLUSTER_MIN_M5_PCT,
                "sniper_interval_sec": SNIPER_INTERVAL_SEC,
                "sniper_max_age_sec": SNIPER_MAX_AGE_SEC,
                "sniper_watch_sec": SNIPER_WATCH_SEC,
                "sniper_min_buyers": SNIPER_MIN_BUYERS,
                "sniper_min_lift_pct": SNIPER_MIN_LIFT_PCT,
                "sniper_fast_lift_pct": SNIPER_FAST_LIFT_PCT,
                "sniper_min_real_sol": SNIPER_MIN_REAL_SOL,
                "sniper_min_liq_usd": SNIPER_MIN_LIQ_USD,
                "sniper_size_usd": SNIPER_SIZE_USD,
                "sniper_target_usd": SNIPER_TARGET_USD,
                "sniper_max_hold_sec": SNIPER_MAX_HOLD_SEC,
                "sniper_dead_sec": SNIPER_DEAD_SEC,
                "sniper_abs_hold_sec": SNIPER_ABS_HOLD_SEC,
                "sniper_target_pct": SNIPER_TARGET_PCT,
                "sniper_max_per_tick": SNIPER_MAX_PER_TICK,
                "sniper_curve_drop_pct": SNIPER_CURVE_DROP_PCT,
                "sniper_stop_usd": SNIPER_STOP_USD,
            },
            "launch_enabled": LAUNCH_ENABLED,
            "cluster_enabled": CLUSTER_ENABLED,
            "sniper_enabled": SNIPER_ENABLED,
            "snipe_feed": snipe_feed.status(),
            "live_trading": False,
        }


def start(*, interval_s: Optional[float] = None) -> dict[str, Any]:
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return status()
        _stop.clear()
        _state.running = True
        _state.last_error = None
        if interval_s is not None:
            period = float(interval_s)
        elif SNIPER_ENABLED:
            period = float(SNIPER_INTERVAL_SEC)
        else:
            period = float(INTERVAL_SEC)
        _thread = threading.Thread(target=_run, args=(period,), name="paper-trenches", daemon=True)
        _thread.start()
    if SNIPER_ENABLED and SNIPER_LISTEN:
        snipe_feed.start()
    pipeline_log.emit("trenches", "start", interval_s=period)
    return status()


def stop() -> dict[str, Any]:
    _stop.set()
    snipe_feed.stop()
    with _lock:
        _state.running = False
    pipeline_log.emit("trenches", "stop")
    return status()


def scan_once(*, do_cluster: bool = True) -> dict[str, Any]:
    started = time.perf_counter()
    with pipeline_log.run(prefix="trh-") as run_id:
        try:
            result = _scan(do_cluster=do_cluster)
            result["run_id"] = run_id
            with _lock:
                _state.ticks += 1
                _state.last_tick_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                _state.last_error = None
                _state.launch_seen += int(result.get("launch_seen") or 0)
                _state.launch_opens += int(result.get("launch_opens") or 0)
                _state.cluster_fires += int(result.get("cluster_fires") or 0)
                _state.cluster_opens += int(result.get("cluster_opens") or 0)
                _state.cluster_exits += int(result.get("cluster_exits") or 0)
                _state.copy_fires += int(result.get("copy_fires") or 0)
                _state.copy_opens += int(result.get("copy_opens") or 0)
                _state.sniper_seen += int(result.get("sniper_seen") or 0)
                _state.sniper_opens += int(result.get("sniper_opens") or 0)
                _state.sniper_expired += int(result.get("sniper_expired") or 0)
                _state.skipped += int(result.get("skipped") or 0)
                hits = list(result.get("hits") or [])
                _state.last_hits = (hits + _state.last_hits)[:12]
            pipeline_log.emit(
                "trenches",
                "tick_done",
                duration_ms=pipeline_log.timed_ms(started),
                **{k: result.get(k) for k in (
                    "launch_seen", "launch_opens", "cluster_fires",
                    "cluster_opens", "cluster_exits", "copy_fires",
                    "copy_opens",
                    "sniper_seen", "sniper_opens", "sniper_expired", "skipped",
                )},
            )
            return result
        except Exception as exc:  # noqa: BLE001
            with _lock:
                _state.last_error = str(exc)
            pipeline_log.emit("trenches", "tick_error", level="error", reason=str(exc))
            return {"status": "error", "error": str(exc), "run_id": run_id}


def _run(interval_s: float) -> None:
    last_cluster = 0.0
    while not _stop.is_set():
        now = time.time()
        do_cluster = (not SNIPER_ENABLED) or (now - last_cluster >= INTERVAL_SEC)
        scan_once(do_cluster=do_cluster)
        if do_cluster:
            last_cluster = now
        _stop.wait(interval_s)
    with _lock:
        _state.running = False


def _scan(*, do_cluster: bool = True) -> dict[str, Any]:
    hits: list[dict[str, Any]] = []
    launch_seen = 0
    launch_opens = 0
    cluster_fires = 0
    cluster_opens = 0
    cluster_exits = 0
    sniper_seen = 0
    sniper_opens = 0
    sniper_expired = 0
    skipped = 0
    opened_this_tick = 0

    if SNIPER_ENABLED:
        s_seen, s_opens, s_skip, s_exp, s_hits = _scan_sniper()
        sniper_seen = s_seen
        sniper_opens = s_opens
        skipped += s_skip
        sniper_expired = s_exp
        hits.extend(s_hits)

    if CLUSTER_ENABLED and do_cluster:
        cluster_exits += _process_watchlist_and_exits()

    candidates: list[dict[str, Any]] = []
    if (LAUNCH_ENABLED or CLUSTER_ENABLED) and do_cluster:
        candidates = list_launch_candidates()
        launch_seen = len(candidates)

    helius_lookups = 0
    for row in candidates:
        if opened_this_tick >= MAX_PER_TICK:
            break
        mint = row["mint"]
        if mint in _seen_mints:
            continue
        # Helius-per-mint is what stretched a trenches tick to minutes.
        if helius_lookups >= 3:
            skipped += 1
            continue
        buyers = unique_buyers_recent(mint, window_s=CLUSTER_WINDOW_SEC)
        helius_lookups += 1
        is_cluster = CLUSTER_ENABLED and len(buyers) >= CLUSTER_MIN_WALLETS
        if is_cluster:
            cluster_fires += 1
        elif LAUNCH_ENABLED and len(buyers) >= LAUNCH_MIN_BUYERS:
            pass
        else:
            # Wait for buyers instead of locking the mint out.
            skipped += 1
            continue
        if not is_cluster and not LAUNCH_ENABLED:
            skipped += 1
            continue
        strategy = "cluster" if is_cluster else "launch"
        size = CLUSTER_SIZE_USD if is_cluster else LAUNCH_SIZE_USD
        hold = CLUSTER_MAX_HOLD_SEC if is_cluster else LAUNCH_MAX_HOLD_SEC
        ok, reason, safety_report = _gate0(mint)
        if not ok:
            skipped += 1
            _seen_mints.add(mint)
            pipeline_log.emit(
                "trenches", "skip", mint=mint, symbol=row.get("symbol"), reason=reason
            )
            continue
        m5_pct: Optional[float] = None
        if is_cluster and (CLUSTER_MIN_M5_PCT > 0 or CLUSTER_MAX_M5_PCT > 0):
            m5_pct = dex_m5_pct(mint, pool=row.get("pool"))
            ok_m5, why_m5 = _m5_ok(m5_pct)
            if not ok_m5:
                skipped += 1
                pipeline_log.emit(
                    "trenches",
                    "skip",
                    mint=mint,
                    symbol=row.get("symbol"),
                    reason=why_m5,
                    m5_pct=m5_pct,
                )
                continue
        dead = CLUSTER_DEAD_SEC if is_cluster and CLUSTER_DEAD_SEC > 0 else None
        stop_usd = CLUSTER_STOP_USD if is_cluster and CLUSTER_STOP_USD > 0 else None
        flow = flow_toxicity(mint, window_s=CLUSTER_WINDOW_SEC)
        if TOXICITY_GATE and flow["toxic"]:
            skipped += 1
            pipeline_log.emit(
                "trenches",
                "skip",
                mint=mint,
                symbol=row.get("symbol"),
                reason=flow["reason"],
            )
            continue
        if is_cluster or strategy == "launch":
            proceed, jev_reason = _jev_cluster_gate(
                row, buyers, m5_pct, safety_report, strategy=strategy, flow=flow
            )
            if not proceed:
                skipped += 1
                pipeline_log.emit(
                    "trenches",
                    "skip",
                    mint=mint,
                    symbol=row.get("symbol"),
                    reason=jev_reason,
                )
                continue
        plan = _size_for_open(
            strategy=strategy,
            base_risk_usd=(
                sizing_mod.RISK_CLUSTER_USD if is_cluster else sizing_mod.RISK_LAUNCH_USD
            ),
            flat_size_usd=size,
            flat_stop_usd=stop_usd,
            row=row,
            safety_report=safety_report,
            vol_proxy=abs(m5_pct or 0.0) / 100.0,
            conviction_mult=_m5_conviction(m5_pct),
        )
        with paper.use_lane(labs.trading_lane(strategy)):
            result = _open(
                row,
                strategy=strategy,
                size=plan.size_usd,
                hold=hold,
                extra={"buyers": buyers, "m5_pct": m5_pct, "risk_usd": plan.risk_usd},
                dead_after_sec=dead,
                stop_loss_usd=plan.stop_usd,
            )
        if result.get("status") in {"opened", "pending"}:
            _seen_mints.add(mint)
            opened_this_tick += 1
            if is_cluster:
                cluster_opens += 1
            else:
                launch_opens += 1
            hits.append(
                {
                    **row,
                    "strategy": strategy,
                    "buyers": len(buyers),
                    "m5_pct": m5_pct,
                    "status": result.get("status"),
                }
            )
        else:
            skipped += 1

    _mark_open_lots()
    copy_fires = 0
    copy_opens = 0
    if COPY_ENABLED:
        for row in candidates:
            if opened_this_tick >= MAX_PER_TICK:
                break
            mint = row["mint"]
            if mint in _seen_mints:
                continue
            leaders = _copy_leaders(mint)
            if len(leaders) < COPY_MIN_LEADERS:
                continue
            copy_fires += 1
            ok, reason, safety_report = _gate0(mint)
            if not ok:
                skipped += 1
                _seen_mints.add(mint)
                pipeline_log.emit(
                    "trenches", "skip", mint=mint, symbol=row.get("symbol"),
                    reason=reason, strategy="copy",
                )
                continue
            m5_pct = None
            if CLUSTER_MIN_M5_PCT > 0 or CLUSTER_MAX_M5_PCT > 0:
                m5_pct = dex_m5_pct(mint, pool=row.get("pool"))
                ok_m5, why_m5 = _m5_ok(m5_pct)
                if not ok_m5:
                    skipped += 1
                    pipeline_log.emit(
                        "trenches", "skip", mint=mint, symbol=row.get("symbol"),
                        reason=why_m5, m5_pct=m5_pct, strategy="copy",
                    )
                    continue
            copy_flow = flow_toxicity(mint, window_s=COPY_WINDOW_SEC)
            if TOXICITY_GATE and copy_flow["toxic"]:
                skipped += 1
                pipeline_log.emit(
                    "trenches", "skip", mint=mint, symbol=row.get("symbol"),
                    reason=copy_flow["reason"], strategy="copy",
                )
                continue
            proceed, jev_reason = _jev_cluster_gate(
                row, [], m5_pct, safety_report, strategy="copy",
                flow=copy_flow,
            )
            if not proceed:
                skipped += 1
                pipeline_log.emit(
                    "trenches", "skip", mint=mint, symbol=row.get("symbol"),
                    reason=jev_reason, strategy="copy",
                )
                continue
            dead = COPY_DEAD_SEC if COPY_DEAD_SEC > 0 else None
            stop_usd = COPY_STOP_USD if COPY_STOP_USD > 0 else None
            plan = _size_for_open(
                strategy="copy",
                base_risk_usd=sizing_mod.RISK_COPY_USD,
                flat_size_usd=COPY_SIZE_USD,
                flat_stop_usd=stop_usd,
                row=row,
                safety_report=safety_report,
                vol_proxy=abs(m5_pct or 0.0) / 100.0,
                conviction_mult=1.0 if len(leaders) >= 2 else 0.75,
            )
            with paper.use_lane(labs.trading_lane("copy")):
                result = _open(
                    row,
                    strategy="copy",
                    size=plan.size_usd,
                    hold=COPY_MAX_HOLD_SEC,
                    extra={"leaders": leaders, "m5_pct": m5_pct, "risk_usd": plan.risk_usd},
                    dead_after_sec=dead,
                    stop_loss_usd=plan.stop_usd,
                )
            if result.get("status") in {"opened", "pending"}:
                _seen_mints.add(mint)
                opened_this_tick += 1
                copy_opens += 1
                hits.append(
                    {
                        **row,
                        "strategy": "copy",
                        "leaders": len(leaders),
                        "m5_pct": m5_pct,
                        "status": result.get("status"),
                    }
                )
            else:
                skipped += 1
    _mark_open_lots()
    return {
        "status": "ok",
        "launch_seen": launch_seen,
        "launch_opens": launch_opens,
        "cluster_fires": cluster_fires,
        "cluster_opens": cluster_opens,
        "cluster_exits": cluster_exits,
        "copy_fires": copy_fires,
        "copy_opens": copy_opens,
        "sniper_seen": sniper_seen,
        "sniper_opens": sniper_opens,
        "sniper_expired": sniper_expired,
        "skipped": skipped,
        "hits": hits,
    }


def list_sniper_candidates() -> list[dict[str, Any]]:
    """Newest Gecko pools — seconds old, not minutes."""
    try:
        rows = fetch_gecko_pool_list(kind="new_pools", page=1)
    except SourceError as exc:
        pipeline_log.emit("trenches", "feed_error", level="warning", reason=str(exc))
        return []
    now = time.time()
    out: list[dict[str, Any]] = []
    for row in rows:
        parsed = _parse_gecko_pool(row)
        if parsed is None:
            continue
        age_min = parsed.get("age_min")
        if age_min is None:
            continue
        age_sec = float(age_min) * 60.0
        if age_sec > SNIPER_MAX_AGE_SEC:
            continue
        if parsed["liquidity_usd"] < SNIPER_MIN_LIQ_USD:
            continue
        if parsed["mint"] in QUOTE_MINTS:
            continue
        if float(parsed.get("price_usd") or 0.0) <= 0:
            continue
        parsed["age_min"] = round(float(age_min), 2)
        parsed["age_sec"] = round(age_sec, 1)
        parsed["seen_at"] = now
        out.append(parsed)
    out.sort(key=lambda r: float(r.get("age_sec") or 9e9))
    return out


def _sol_usd() -> float:
    try:
        from decision.engines.arb.engine import _estimate_sol_usd

        return float(_estimate_sol_usd())
    except Exception:  # noqa: BLE001
        return float(SOL_USD_FALLBACK)


def _ingest_pump_creates() -> int:
    """Queue landed creates as watches. Do not buy the create itself."""
    sol = _sol_usd()
    px = initial_price_usd(sol)
    now = time.time()
    added = 0
    for ev in snipe_feed.drain(limit=32):
        mint = ev.get("mint") or ""
        creator = ev.get("creator") or ""
        if not mint or mint in _seen_mints or mint in _watches:
            continue
        if creator and _creators.too_hot(creator):
            pipeline_log.emit(
                "trenches", "skip", mint=mint, symbol=ev.get("symbol"),
                reason="creator_spray", strategy="snipe",
            )
            _seen_mints.add(mint)
            continue
        if creator:
            _creators.note(creator)
        if px > 0:
            _snipe_marks[mint] = px
        _watches[mint] = {
            "mint": mint,
            "pool": ev.get("bonding_curve") or mint,
            "symbol": ev.get("symbol") or mint[:6],
            "create_px": px,
            "seen_at": float(ev.get("seen_at") or now),
            "creator": creator,
            "signature": ev.get("signature"),
            "source": "pump_create",
        }
        added += 1
    return added


def _scan_sniper() -> tuple[int, int, int, int, list[dict[str, Any]]]:
    added = _ingest_pump_creates()
    sol = _sol_usd()
    tape_map = snipe_feed.tapes(sol_usd=sol)
    for mint, tape in tape_map.items():
        px = float(tape.get("last_px") or 0.0)
        if px > 0:
            _snipe_marks[mint] = px
    now = time.time()
    opens = 0
    skipped = 0
    expired = 0
    hits: list[dict[str, Any]] = []
    for mint, watch in list(_watches.items()):
        tape = tape_map.get(mint) or {}
        create_px = float(watch.get("create_px") or 0.0)
        last_px = float(tape.get("last_px") or 0.0) or create_px
        unique = int(tape.get("unique_buyers") or 0)
        buys = int(tape.get("buys") or 0)
        sells = int(tape.get("sells") or 0)
        real_sol = float(tape.get("real_sol") or 0.0)
        peak_real = float(tape.get("peak_real_sol") or 0.0)
        age = now - float(watch.get("seen_at") or now)
        ok, why = snipe_entry(
            create_px=create_px,
            last_px=last_px,
            unique_buyers=unique,
            buys=buys,
            sells=sells,
            real_sol=real_sol,
            age_sec=age,
            watch_sec=SNIPER_WATCH_SEC,
            min_buyers=SNIPER_MIN_BUYERS,
            min_lift_pct=SNIPER_MIN_LIFT_PCT,
            fast_lift_pct=SNIPER_FAST_LIFT_PCT,
            min_real_sol=SNIPER_MIN_REAL_SOL,
            dev_sold=bool(tape.get("dev_sold")),
            peak_real_sol=peak_real,
            curve_drop_pct=SNIPER_CURVE_DROP_PCT,
        )
        if why in {"watch_expired", "net_selling", "dev_sold", "curve_dump"}:
            _seen_mints.add(mint)
            _watches.pop(mint, None)
            expired += 1
            skipped += 1
            pipeline_log.emit(
                "trenches", "skip", mint=mint, symbol=watch.get("symbol"),
                reason=why, strategy="snipe",
            )
            snipe_desk.observe(
                unique_buyers=unique,
                lift_pct=snipe_lift_pct(create_px, last_px),
                real_sol=real_sol,
                age_sec=age,
            )
            continue
        snipe_lanes = labs.snipe_lanes()
        if snipe_lanes:
            tape_ctx = {
                "buyers_unique": unique,
                "buys": buys,
                "sells": sells,
                "real_sol": round(real_sol, 2),
                "peak_real_sol": round(peak_real, 2),
                "lift_pct": round(snipe_lift_pct(create_px, last_px), 2),
                "age_sec": round(age, 1),
            }
            gated_open = functools.partial(_snipe_gated_open, _tape=tape_ctx)
            for lane in snipe_lanes:
                opened_n = sum(1 for h in hits if h.get("lane") == lane.id)
                hit = labs.consider_snipe(
                    lane=lane,
                    watch=watch,
                    tape=tape,
                    age_sec=age,
                    sol_usd=sol,
                    watch_sec=SNIPER_WATCH_SEC,
                    curve_drop_pct=SNIPER_CURVE_DROP_PCT,
                    max_per_tick_already=opened_n,
                    max_per_tick=SNIPER_MAX_PER_TICK,
                    open_fn=gated_open,
                )
                if hit:
                    opens += 1
                    hits.append(hit)
            snipe_desk.observe(
                unique_buyers=unique,
                lift_pct=snipe_lift_pct(create_px, last_px),
                real_sol=real_sol,
                age_sec=age,
            )
            if labs.all_snipe_lanes_seen(mint):
                _watches.pop(mint, None)
            continue
        if not ok or mint in _seen_mints:
            continue
        liq_usd = real_sol * sol
        if liq_usd < SNIPER_MIN_LIQ_USD:
            continue
        if opens >= SNIPER_MAX_PER_TICK:
            continue
        lift = snipe_lift_pct(create_px, last_px)
        size = snipe_size(
            SNIPER_SIZE_USD,
            lift_pct=lift,
            unique_buyers=unique,
            real_sol=real_sol,
        )
        row = {
            "mint": mint,
            "pool": watch.get("pool") or mint,
            "symbol": watch.get("symbol") or mint[:6],
            "liquidity_usd": real_sol * sol,
            "price_usd": last_px,
            "age_min": round(age / 60.0, 2),
            "age_sec": round(age, 1),
            "seen_at": watch.get("seen_at"),
            "source": "pump_create",
            "creator": watch.get("creator"),
            "buyers_unique": unique,
            "buys": buys,
            "sells": sells,
            "real_sol": round(real_sol, 2),
            "peak_real_sol": round(peak_real, 2),
            "lift_pct": round(lift, 2),
            "size_usd": size,
        }
        snipe_toxic, snipe_why = toxic_flow(buys, sells)
        if TOXICITY_GATE and snipe_toxic:
            skipped += 1
            pipeline_log.emit(
                "trenches", "skip", mint=mint, symbol=row.get("symbol"),
                reason=snipe_why, strategy="snipe",
            )
            continue
        proceed, jev_reason = _jev_cluster_gate(
            row, [], None, None, strategy="snipe",
            flow={"buys": buys, "sells": sells},
        )
        if not proceed:
            skipped += 1
            pipeline_log.emit(
                "trenches", "skip", mint=mint, symbol=row.get("symbol"),
                reason=jev_reason, strategy="snipe",
            )
            continue
        plan = _size_for_open(
            strategy="snipe",
            base_risk_usd=sizing_mod.RISK_SNIPE_USD,
            flat_size_usd=size,
            flat_stop_usd=SNIPER_STOP_USD,
            row=row,
            safety_report=None,
            vol_proxy=lift / 100.0,
            conviction_mult=_tape_conviction(lift, unique, real_sol),
        )
        result = _open(
            row,
            strategy="snipe",
            size=plan.size_usd,
            hold=SNIPER_MAX_HOLD_SEC,
            confirm_dex=False,
            bank_at_target=True,
            target_usd=SNIPER_TARGET_USD,
            take_profit_pct=SNIPER_TARGET_PCT,
            dead_after_sec=SNIPER_DEAD_SEC,
            abs_hold_sec=SNIPER_ABS_HOLD_SEC,
            stop_loss_usd=plan.stop_usd,
        )
        if result.get("status") in {"opened", "pending"}:
            _seen_mints.add(mint)
            _watches.pop(mint, None)
            opens += 1
            hits.append({
                **row,
                "strategy": "snipe",
                "buyers": unique,
                "lift_pct": round(lift, 2),
                "entry_why": why,
                "size_usd": size,
            })
        else:
            skipped += 1
    # ``added`` is the count of creates newly ingested this tick; ``len(_watches)``
    # is surfaced separately via status()["sniper_watching"]. Do not add the two
    # — the old ``added + len(_watches)`` double-counted still-watching mints
    # every tick and made "sniper_seen" meaningless as a cumulative metric.
    return added, opens, skipped, expired, hits


def _snipe_hard_block(mint: str) -> tuple[bool, str]:
    """Fast mint-only screen. Allows bonding-curve mint authority; blocks freezes."""
    try:
        info = fetch_mint_account(mint)
    except Exception as exc:  # noqa: BLE001
        return False, f"snipe_mint: {exc}"
    if info.get("freezeAuthority"):
        return False, "freeze_authority"
    for entry in info.get("extensions") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("extension")
        if name in _SNIPE_DEATH_EXTS:
            return False, f"ext_{name}"
    return True, "ok"


def list_launch_candidates() -> list[dict[str, Any]]:
    """Young Gecko new_pools that clear the launch liquidity/age screen."""
    try:
        rows = fetch_gecko_pool_list(kind="new_pools", page=1)
    except SourceError as exc:
        pipeline_log.emit("trenches", "feed_error", level="warning", reason=str(exc))
        return []
    now = time.time()
    out: list[dict[str, Any]] = []
    for row in rows:
        parsed = _parse_gecko_pool(row)
        if parsed is None:
            continue
        age_min = parsed.get("age_min")
        if age_min is None or age_min > LAUNCH_MAX_AGE_MIN:
            continue
        if parsed["liquidity_usd"] < LAUNCH_MIN_LIQ_USD:
            continue
        if parsed["mint"] in QUOTE_MINTS:
            continue
        parsed["age_min"] = round(float(age_min), 2)
        parsed["seen_at"] = now
        out.append(parsed)
    return out


def _parse_gecko_pool(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    attrs = row.get("attributes") or {}
    rel = row.get("relationships") or {}
    pool = (attrs.get("address") or "").strip()
    base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id") or ""
    mint = base_id.split("_", 1)[-1] if base_id else ""
    if not pool or not mint:
        return None
    name = attrs.get("name") or ""
    symbol = name.split("/")[0].strip() if name else mint[:6]
    try:
        liq = float(attrs.get("reserve_in_usd") or 0.0)
    except (TypeError, ValueError):
        liq = 0.0
    try:
        price = float(attrs.get("base_token_price_usd") or 0.0)
    except (TypeError, ValueError):
        price = 0.0
    created = attrs.get("pool_created_at")
    age_min = None
    if isinstance(created, str) and created:
        try:
            ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
            age_min = max(0.0, (time.time() - ts) / 60.0)
        except ValueError:
            age_min = None
    return {
        "mint": mint,
        "pool": pool,
        "symbol": symbol,
        "liquidity_usd": liq,
        "price_usd": price,
        "age_min": age_min,
        "source": "gecko_new_pools",
    }


def parse_swaps(txs: list[dict[str, Any]], *, wallet: Optional[str] = None) -> list[dict[str, Any]]:
    """Extract buy/sell mint events from Helius parsed transactions."""
    wallet_l = (wallet or "").lower()
    events: list[dict[str, Any]] = []
    for tx in txs:
        try:
            ts = float(tx.get("timestamp") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        sig = tx.get("signature")
        for transfer in tx.get("tokenTransfers") or []:
            if not isinstance(transfer, dict):
                continue
            mint = (transfer.get("mint") or "").strip()
            if not mint or mint in QUOTE_MINTS:
                continue
            to_acc = (transfer.get("toUserAccount") or "").lower()
            from_acc = (transfer.get("fromUserAccount") or "").lower()
            side = None
            actor = None
            if wallet_l:
                if to_acc == wallet_l:
                    side, actor = "buy", wallet
                elif from_acc == wallet_l:
                    side, actor = "sell", wallet
            else:
                if to_acc:
                    side, actor = "buy", transfer.get("toUserAccount")
                elif from_acc:
                    side, actor = "sell", transfer.get("fromUserAccount")
            if side and actor:
                events.append(
                    {"side": side, "mint": mint, "wallet": actor, "ts": ts, "signature": sig}
                )
    return events


def unique_buyers_recent(mint: str, *, window_s: float) -> list[str]:
    """Unique wallets that bought ``mint`` in the last ``window_s`` seconds."""
    now = time.time()
    cutoff = now - window_s
    with _lock:
        existing = {
            w: ts for w, ts in (_buys.get(mint) or {}).items() if ts >= cutoff
        }
    if not SETTINGS.helius_api_keys:
        return list(existing)
    for ev in _mint_swaps(mint):
        if ev["side"] != "buy" or ev["mint"] != mint:
            continue
        if ev["ts"] < cutoff:
            continue
        existing[ev["wallet"]] = ev["ts"]
    with _lock:
        _buys.setdefault(mint, {}).update(existing)
    return list(existing)


_swaps_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
SWAPS_CACHE_SEC = _env_float("SWAPS_CACHE_SEC", 30.0)

# Order-flow toxicity (VPIN-lite): distribution shows up as sell flow
# before the price does. Both sides come from the same cached fetch the
# buyer screen already pays for, so this adds no Helius cost.
TOXICITY_GATE = os.getenv("TOXICITY_GATE", "1").strip() == "1"
TOX_MAX_SELL_FRAC = _env_float("TOX_MAX_SELL_FRAC", 0.6)
TOX_MIN_SWAPS = _env_int("TOX_MIN_SWAPS", 6)


def _mint_swaps(mint: str) -> list[dict[str, Any]]:
    """Recent swap events for a mint (both sides), cached to bound cost."""
    now = time.time()
    hit = _swaps_cache.get(mint)
    if hit and now - hit[0] < SWAPS_CACHE_SEC:
        return hit[1]
    events: list[dict[str, Any]] = []
    if SETTINGS.helius_api_keys:
        try:
            txs = fetch_helius_transactions(mint, limit=20)
            events = [ev for ev in parse_swaps(txs) if ev["mint"] == mint]
        except SourceError:
            events = []
    _swaps_cache[mint] = (now, events)
    return events


def toxic_flow(buys: int, sells: int) -> tuple[bool, str]:
    """True when sell flow dominates a sufficiently large sample."""
    n = (buys or 0) + (sells or 0)
    if n < TOX_MIN_SWAPS:
        return False, ""
    frac = sells / n
    if frac >= TOX_MAX_SELL_FRAC:
        return True, f"toxic_flow sells {frac:.0%} ({sells}/{n})"
    return False, ""


def flow_toxicity(mint: str, *, window_s: float) -> dict[str, Any]:
    """Sell-side distribution stats for a mint over the window."""
    now = time.time()
    cutoff = now - window_s
    buys = sells = 0
    sellers: dict[str, int] = {}
    for ev in _mint_swaps(mint):
        if ev["ts"] < cutoff:
            continue
        if ev["side"] == "buy":
            buys += 1
        elif ev["side"] == "sell":
            sells += 1
            sellers[ev["wallet"]] = sellers.get(ev["wallet"], 0) + 1
    toxic, reason = toxic_flow(buys, sells)
    top_share = (max(sellers.values()) / sells) if sells else 0.0
    return {
        "buys": buys,
        "sells": sells,
        "swaps": buys + sells,
        "sell_frac": round(sells / (buys + sells), 3) if (buys + sells) else 0.0,
        "sellers": len(sellers),
        "top_seller_share": round(top_share, 3),
        "toxic": toxic,
        "reason": reason,
    }


def cluster_score(buyers_by_wallet: dict[str, float], *, now: Optional[float] = None) -> int:
    """How many unique wallets bought inside the cluster window."""
    now = now if now is not None else time.time()
    cutoff = now - CLUSTER_WINDOW_SEC
    return sum(1 for ts in buyers_by_wallet.values() if ts >= cutoff)


def _process_watchlist_and_exits() -> int:
    exits = 0
    now = time.time()
    for wallet in _wallets():
        try:
            txs = fetch_helius_transactions(wallet, limit=15)
        except SourceError as exc:
            pipeline_log.emit(
                "trenches", "wallet_error", level="warning", wallet=wallet[:8], reason=str(exc)
            )
            continue
        for ev in parse_swaps(txs, wallet=wallet):
            mint = ev["mint"]
            if ev["side"] == "buy" and ev["ts"] >= now - CLUSTER_WINDOW_SEC:
                with _lock:
                    _buys.setdefault(mint, {})[wallet] = ev["ts"]
            elif ev["side"] == "sell":
                with _lock:
                    _sells.setdefault(mint, {})[wallet] = ev["ts"]
                mark = dex_price_usd(mint)
                if mark:
                    closed = paper.close_by_mint(
                        mint=mint, mark_price=mark, reason=f"cluster_sell {wallet[:6]}"
                    )
                    exits += len(closed)
    return exits


def _copy_leaders(mint: str, *, now: Optional[float] = None) -> list[str]:
    """Tracked wallets that bought this mint inside the copy window."""
    if not COPY_ENABLED:
        return []
    now = now if now is not None else time.time()
    cutoff = now - COPY_WINDOW_SEC
    with _lock:
        return [w for w, ts in _buys.get(mint, {}).items() if ts >= cutoff]


def _buyers_ok(n: int) -> bool:
    return n >= CLUSTER_MIN_WALLETS


def _m5_ok(m5_pct: Optional[float]) -> tuple[bool, str]:
    """Lower bound keeps momentum; upper bound rejects blowoffs.

    Missing Dex data (None) never blocks — other screens still apply.
    """
    if m5_pct is None:
        return True, ""
    if CLUSTER_MIN_M5_PCT > 0 and m5_pct < CLUSTER_MIN_M5_PCT:
        return False, f"m5_low {m5_pct}<{CLUSTER_MIN_M5_PCT}"
    if CLUSTER_MAX_M5_PCT > 0 and m5_pct > CLUSTER_MAX_M5_PCT:
        return False, f"m5_high {m5_pct}>{CLUSTER_MAX_M5_PCT}"
    return True, ""


def _gate0(mint: str) -> tuple[bool, str, Optional[Any]]:
    """Gate 0 safety screen. Returns (ok, reason, report) — the report is
    reused as JEV chair state so safety evidence is judged, not re-fetched."""
    try:
        report = check_token(mint)
    except Exception as exc:  # noqa: BLE001
        return False, f"gate0_error: {exc}", None
    if report.blocking or report.verdict == SafetyVerdict.DANGER:
        return False, f"gate0_{report.verdict.value}", report
    return True, "ok", report


def _jev_cluster_gate(
    row: dict[str, Any],
    buyers: list[str],
    m5_pct: Optional[float],
    safety_report: Optional[Any] = None,
    *,
    strategy: str = "cluster",
    flow: Optional[dict[str, Any]] = None,
) -> tuple[bool, str]:
    """Ask the JEV chair before a trenches open. Returns (proceed, reason).

    Covers the cluster, launch, and snipe entry paths; each has its own
    kill switch (``JEV_<STRATEGY>_GATE``). State is evidence the desk
    already paid for, never re-fetched: the Gate 0 safety report, a
    code-computed edge estimate where one exists, and the tape that
    fired the entry (buyers / lift / curve SOL).
    Fail-open: a disabled/unavailable chair proceeds exactly as before.
    A blocked mint stays blocked for ``JEV_CLUSTER_RECHECK_SEC`` without
    re-asking (bounds API cost); the mint is NOT added to ``_seen_mints``
    so a better tape can still open it later. A sniper proceed is reused
    for the watch window (both lanes, later ticks) and is not a block.
    """
    from types import SimpleNamespace

    from decision import costs

    if not _strategy_gate_on(strategy):
        return True, "jev_gate_off"
    mint = str(row.get("mint") or "")
    symbol = str(row.get("symbol") or mint[:6])
    now = time.time()
    blocked_at = _jev_blocked.get(mint, 0.0)
    if blocked_at and now - blocked_at < JEV_CLUSTER_RECHECK_SEC:
        return False, "jev_cooldown"
    if strategy == "snipe" and mint:
        cached = _jev_proceed.get(mint)
        if cached and now - cached[0] < _snipe_proceed_ttl():
            return True, cached[1]
    try:
        from decision import jev as jev_mod
    except Exception:  # noqa: BLE001 — chair unavailable, trade as before
        return True, "jev_unavailable"
    n_buyers = len(buyers) if buyers else int(row.get("buyers_unique") or 0)
    flow = flow or {}
    n_sells = int(flow.get("sells") or row.get("sells") or 0)
    liq = row.get("liquidity_usd")
    price_impact = None
    try:
        price_impact = float(getattr(getattr(safety_report, "sellability", None), "price_impact_pct", None))
    except (TypeError, ValueError, AttributeError):
        price_impact = None
    edge_pct = None
    if m5_pct is not None:
        try:
            edge_pct = round(
                float(m5_pct) - costs.round_trip_cost_pct(price_impact_pct=price_impact),
                2,
            )
        except (TypeError, ValueError):
            edge_pct = None
    safety_line = "gate0 passed"
    try:
        summary = safety_report.summary() if safety_report is not None else None
        if summary:
            safety_line = str(summary)[:220]
    except Exception:  # noqa: BLE001 — summary is decoration, not load-bearing
        pass
    if strategy == "snipe":
        evidence = (
            f"snipe candidate {symbol} ({mint[:8]}): lift "
            f"{row.get('lift_pct')}% over curve in {row.get('age_sec')}s, "
            f"{n_buyers} unique buyers "
            f"(buys {row.get('buys')}/sells {n_sells}), real SOL "
            f"{row.get('real_sol')} (peak {row.get('peak_real_sol')}), size "
            f"${row.get('size_usd')}. Safety: {safety_line}"
        )
    else:
        evidence = (
            f"{strategy} candidate {symbol} ({mint[:8]}): {n_buyers} unique "
            f"buyers / {n_sells} sellers in {CLUSTER_WINDOW_SEC:.0f}s, m5 "
            f"{m5_pct}%, edge {edge_pct}% after costs, liquidity ${liq}, "
            f"age {row.get('age_min')}m. Safety: {safety_line}"
        )
    card = SimpleNamespace(
        summary=lambda: evidence,
        action=SimpleNamespace(value="buy"),
        action_confidence=0.5,
        direction=SimpleNamespace(value="long"),
        cost_adjusted_edge_pct=edge_pct,
        expected_return_pct=None,
        forecast=None,
        timeframe="cluster",
        token={"address": mint, "name": symbol},
        safety={},
        notes={},
    )
    packet = SimpleNamespace(
        market=SimpleNamespace(liquidity_usd_total=liq, liquidity_usd=liq),
        safety=safety_report,
        orderflow={"buys_5m": n_buyers, "sells_5m": n_sells, "source": strategy},
    )
    try:
        review = jev_mod.chair_review(card, packet, [])
    except Exception:  # noqa: BLE001 — never break the trading loop
        pipeline_log.emit("trenches", "jev_error", mint=mint, symbol=symbol)
        return True, "jev_error"
    if review is None:
        return True, "jev_offline"
    if review.get("veto"):
        _jev_proceed.pop(mint, None)
        _jev_blocked[mint] = now
        return False, f"jev_veto: {review.get('reason', '')}"[:200]
    if review.get("action") == "avoid":
        _jev_proceed.pop(mint, None)
        _jev_blocked[mint] = now
        return False, f"jev_avoid: {review.get('reason', '')}"[:200]
    reason = f"jev_{review.get('action', 'hold')}"
    if strategy == "snipe" and mint:
        _jev_proceed[mint] = (now, reason)
    return True, reason


def _snipe_gated_open(row: dict[str, Any], *, _tape: Optional[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    """``open_fn`` for the labs snipe-lane path with safety + JEV gate.

    The lane path calls ``open_fn`` directly (no Gate 0, no pre-gate), so
    both live here: a ``check_token`` screen first (blocking verdicts
    skip outright), then the shared entry gate with the report attached
    so the chair judges real safety evidence, then the real ``_open``.
    """
    merged = {**row, **(_tape or {})}
    mint = str(merged.get("mint") or "")
    symbol = str(merged.get("symbol") or mint[:6])
    try:
        report = check_token(mint)
    except Exception as exc:  # noqa: BLE001 — fail closed, like _gate0
        reason = f"snipe_gate0_error: {exc}"[:200]
        pipeline_log.emit(
            "trenches", "skip", mint=mint, symbol=symbol,
            reason=reason, strategy="snipe",
        )
        return {"status": "skipped", "reason": reason}
    if report.blocking or report.verdict == SafetyVerdict.DANGER:
        reason = f"snipe_gate0_{report.verdict.value if hasattr(report.verdict, 'value') else report.verdict}"
        pipeline_log.emit(
            "trenches", "skip", mint=mint, symbol=symbol,
            reason=reason, strategy="snipe",
        )
        return {"status": "skipped", "reason": reason}
    try:
        lane_toxic, lane_why = toxic_flow(
            int(merged.get("buys") or 0), int(merged.get("sells") or 0)
        )
    except (TypeError, ValueError):
        lane_toxic, lane_why = False, ""
    if TOXICITY_GATE and lane_toxic:
        pipeline_log.emit(
            "trenches", "skip", mint=mint, symbol=symbol,
            reason=lane_why, strategy="snipe",
        )
        return {"status": "skipped", "reason": lane_why}
    ok, why = _jev_cluster_gate(merged, [], None, report, strategy="snipe")
    if not ok:
        pipeline_log.emit(
            "trenches", "skip", mint=merged.get("mint"), symbol=merged.get("symbol"),
            reason=why, strategy="snipe",
        )
        return {"status": "skipped", "reason": why}
    return _open(merged, **kw)


def _open(
    row: dict[str, Any],
    *,
    strategy: str,
    size: float,
    hold: float,
    extra: Optional[dict[str, Any]] = None,
    confirm_dex: bool = True,
    bank_at_target: bool = False,
    target_usd: Optional[float] = None,
    take_profit_pct: float = 0.0,
    dead_after_sec: Optional[float] = None,
    abs_hold_sec: Optional[float] = None,
    stop_loss_usd: Optional[float] = None,
) -> dict[str, Any]:
    gecko = float(row.get("price_usd") or 0.0)
    if confirm_dex:
        price, why = confirm_entry_price(
            mint=row["mint"],
            pool=row.get("pool"),
            gecko_usd=gecko,
        )
        if price is None:
            return {"status": "skipped", "reason": why}
    else:
        price = gecko
        if price <= 0:
            return {"status": "skipped", "reason": "no price"}
    if PUMP_CURVE_GUARD and strategy in ("cluster", "launch", "snipe", "copy"):
        from decision import pump_curve

        sol = _sol_usd()
        exact = _curve_state(row["mint"])
        if exact is not None and exact.get("sol_quoted"):
            if exact.get("complete"):
                return {"status": "skipped", "reason": "curve_complete"}
            sol_in = size / sol if sol > 0 else 0.0
            real_sol = float(exact.get("real_sol") or 0.0)
            if real_sol > 0 and sol_in > 0.5 * real_sol:
                return {"status": "skipped", "reason": "curve_thin_exact"}
            impact = pump_curve.buy_impact_pct(
                sol_in,
                float(exact.get("v_tok") or 0.0),
                float(exact.get("v_sol") or 0.0),
                float(exact.get("real_tok") or 0.0),
            )
            if impact is not None and impact > PUMP_CURVE_MAX_IMPACT_PCT:
                return {
                    "status": "skipped",
                    "reason": f"curve_impact_exact {impact:.1f}%>{PUMP_CURVE_MAX_IMPACT_PCT:g}%",
                }
        else:
            curve_ok, curve_why, _ = pump_curve.guard_buy(
                size_usd=size,
                price_usd=price,
                liquidity_usd=row.get("liquidity_usd"),
                sol_usd=sol,
                max_impact_pct=PUMP_CURVE_MAX_IMPACT_PCT,
                max_liq_frac=PUMP_CURVE_MAX_LIQ_FRAC,
            )
            if not curve_ok:
                return {"status": "skipped", "reason": curve_why}
    result = paper.execute_signal(
        address=row["pool"],
        mint=row["mint"],
        name=str(row.get("symbol") or ""),
        mark_price=price,
        size_usd=size,
        strategy=strategy,
        max_hold_sec=hold,
        extra=extra,
        bank_at_target=bank_at_target,
        target_profit_usd=target_usd,
        take_profit_pct=take_profit_pct,
        dead_after_sec=dead_after_sec,
        abs_hold_sec=abs_hold_sec,
        stop_loss_usd=stop_loss_usd,
    )
    if result.get("status") in {"opened", "pending"}:
        pricefeed.track(mint=row["mint"], pool=row["pool"], symbol=str(row.get("symbol") or ""))
        if _token_sink is not None:
            _token_sink(
                row["pool"],
                {
                    "name": row.get("symbol"),
                    "mint": row["mint"],
                    "discover": True,
                    "role": "trade",
                    "tradeable": True,
                    "strategy": strategy,
                    "price_usd": price,
                    "liquidity_usd": row.get("liquidity_usd"),
                    "timeframes": {
                        "1": [
                            {
                                "timestamp": time.time(),
                                "open": price,
                                "high": price,
                                "low": price,
                                "close": price,
                                "volume": 0,
                            }
                        ]
                    },
                },
            )
        pipeline_log.emit(
            "trenches",
            "open",
            mint=row["mint"],
            symbol=row.get("symbol"),
            strategy=strategy,
            size_usd=size,
            age_min=row.get("age_min"),
        )
    return result


def _tape_force_reason(tape: dict[str, Any]) -> Optional[str]:
    if tape.get("dev_sold"):
        return "dev_sell"
    if curve_gave_back(
        float(tape.get("peak_real_sol") or 0.0),
        float(tape.get("real_sol") or 0.0),
        drop_pct=SNIPER_CURVE_DROP_PCT,
    ):
        return "curve_dump"
    return None


def _mark_open_lots() -> None:
    tape_map = snipe_feed.tapes(sol_usd=_sol_usd())
    lane_ids = [paper.DEFAULT_LANE]
    for lane in labs.snipe_lanes():
        if lane.id not in lane_ids:
            lane_ids.append(lane.id)
    if labs.ENABLED:
        for lid in (labs.CLUSTER_LANE, labs.SCALP_LANE, labs.COPY_LANE):
            if lid not in lane_ids:
                lane_ids.append(lid)

    def _fallback(mint: str, pool: str = "") -> Optional[float]:
        return dex_price_usd(mint, pool=pool or None)

    paper.drain_pending_buys(fallback_price_fn=_fallback)

    for lane_id in lane_ids:
        with paper.use_lane(lane_id):
            snap = paper.snapshot()
            for pos in snap.get("open") or []:
                if not isinstance(pos, dict):
                    continue
                addr = pos.get("address")
                mint = pos.get("mint")
                if not addr:
                    continue
                opened = pos.get("opened_at")
                reason = pos.get("entry_reason")
                if reason != "snipe" and _just_opened(opened):
                    continue
                tape = tape_map.get(str(mint or "")) or {}
                mark = None
                if reason in {"snipe", "cluster", "launch", "copy"} and mint:
                    mark = _snipe_marks.get(str(mint))
                    if mark is None:
                        mark = float(tape.get("last_px") or 0.0) or None
                if mark is None:
                    mark = dex_price_usd(str(mint or ""), pool=str(addr))
                if mark is None and mint:
                    mark = _snipe_marks.get(str(mint))
                if mark:
                    if mint:
                        paper.observe_mark(str(mint), float(mark))
                    force = (
                        _tape_force_reason(tape)
                        if reason in {"snipe", "cluster", "launch", "copy"}
                        else None
                    )
                    paper.mark_and_maybe_exit(
                        address=str(addr),
                        mark_price=mark,
                        force_reason=force,
                    )


def _just_opened(opened_at: Any) -> bool:
    if not isinstance(opened_at, str) or not opened_at:
        return False
    try:
        ts = datetime.fromisoformat(opened_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False
    return (time.time() - ts) < 5.0


def confirm_entry_price(
    *,
    mint: str,
    pool: Optional[str],
    gecko_usd: float,
) -> tuple[Optional[float], str]:
    """Require a Dex print that agrees with Gecko before we paper-buy."""
    dex = dex_price_usd(mint, pool=pool)
    if dex is None or dex <= 0:
        return None, "no_dex_price"
    if gecko_usd > 0:
        diverge = abs(dex / gecko_usd - 1.0) * 100.0
        if diverge > MAX_PRICE_DIVERGE_PCT:
            return None, f"price_diverge {diverge:.1f}%"
    return dex, "ok"


def dex_price_usd(mint: str, pool: Optional[str] = None) -> Optional[float]:
    pair = _pick_dex_pair(mint, pool=pool)
    if pair is None:
        return None
    try:
        px = float(pair.get("priceUsd") or 0.0)
    except (TypeError, ValueError):
        return None
    return px or None


def dex_m5_pct(mint: str, pool: Optional[str] = None) -> Optional[float]:
    """DexScreener 5-minute % change for the chosen pair, or None if missing."""
    pair = _pick_dex_pair(mint, pool=pool)
    if pair is None:
        return None
    chg = pair.get("priceChange") or {}
    try:
        m5 = chg.get("m5")
        return float(m5) if m5 is not None else None
    except (TypeError, ValueError):
        return None


def _pick_dex_pair(mint: str, pool: Optional[str] = None) -> Optional[dict[str, Any]]:
    if not mint:
        return None
    try:
        pairs = fetch_dexscreener_pairs(mint)
    except SourceError:
        return None
    return pick_dex_pair(pairs, pool=pool)


def pick_dex_pair(
    pairs: list[dict[str, Any]], pool: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """``pool`` if present, else the most liquid Solana pair — not the max mid."""
    ranked = [p for p in pairs if isinstance(p, dict)]
    if not ranked:
        return None
    pool_l = (pool or "").lower()
    if pool_l:
        for pair in ranked:
            if str(pair.get("pairAddress") or "").lower() == pool_l:
                return pair
    return max(ranked, key=_pair_liq_usd)


def pick_dex_price(pairs: list[dict[str, Any]], pool: Optional[str] = None) -> Optional[float]:
    """Price of ``pool`` if present, else the most liquid Solana pair — not the max mid."""
    chosen = pick_dex_pair(pairs, pool=pool)
    if chosen is None:
        return None
    try:
        px = float(chosen.get("priceUsd") or 0.0)
    except (TypeError, ValueError):
        return None
    return px or None


def _pair_liq_usd(pair: dict[str, Any]) -> float:
    liq = pair.get("liquidity") or {}
    try:
        return float(liq.get("usd") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _mark_for_mint(mint: str, pool: Optional[str] = None) -> Optional[float]:
    return dex_price_usd(mint, pool=pool)
