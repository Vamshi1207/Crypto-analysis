"""Assemble a MarketPacket from live token_data or historical corpus OHLCV."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from decision import costs, dataset
from decision.config import _env_float, _env_int
from decision.schema import MarketPacket, MarketSnapshot, SafetyReport, TimeframePacket
from indicators import get_indicators_for_token

DEFAULT_TIMEFRAMES = ("5S", "15S", "30S", "1", "3", "5", "15", "60")
DEFAULT_TAIL = _env_int("PACKET_OHLCV_TAIL", 256)


def _est_round_trip_cost_pct(
    *,
    slippage_bps: Optional[float],
    price_impact_pct: Optional[float] = None,
) -> float:
    """All-in cost for a round trip, in percent of notional.

    Delegates to `decision.costs` so Gate 2 prices a trade with exactly the
    numbers `decision.paper` will charge on the fill. The previous local formula
    counted slippage only once and understated the true cost by ~1.2 points,
    which let buys clear the gate that execution could not make money on.
    """
    return costs.round_trip_cost_pct(
        price_impact_pct=price_impact_pct,
        slippage_bps=slippage_bps,
    )


def build_timeframe_packet(
    candles: list[dict[str, Any]],
    timeframe: str,
    *,
    tail: int = DEFAULT_TAIL,
) -> TimeframePacket:
    window = candles[-tail:] if tail > 0 else list(candles)
    rows = dataset.ohlcv_rows(window)
    indicators: dict[str, Any] = {}
    if len(window) >= 30:
        try:
            snap = get_indicators_for_token(window)
            # Drop bulky series; keep scalar snapshot fields.
            indicators = {
                k: v
                for k, v in snap.items()
                if not isinstance(v, list)
            }
        except Exception as exc:  # noqa: BLE001
            indicators = {"error": f"{type(exc).__name__}: {exc}"}

    return TimeframePacket(
        timeframe=timeframe,
        candle_count=len(window),
        ohlcv_tail=rows,
        indicators=indicators,
        summary=dataset.summarize_candles(window),
    )


def packet_from_candles(
    *,
    address: str,
    name: Optional[str],
    timeframes: dict[str, list[dict[str, Any]]],
    mint: Optional[str] = None,
    safety: Optional[SafetyReport] = None,
    market: Optional[MarketSnapshot] = None,
    axiom_stats: Optional[list[dict[str, Any]]] = None,
    source: str = "live",
    tail: int = DEFAULT_TAIL,
    wanted: Optional[tuple[str, ...]] = None,
) -> MarketPacket:
    # Prefer whatever TFs the caller actually supplied (e.g. continuous 15S),
    # falling back to the standard set when building from a full corpus file.
    if wanted is None:
        present = tuple(tf for tf in timeframes if timeframes.get(tf))
        wanted = present or DEFAULT_TIMEFRAMES

    tf_packets: dict[str, TimeframePacket] = {}
    for tf in wanted:
        candles = timeframes.get(tf) or []
        if not candles:
            continue
        tf_packets[tf] = build_timeframe_packet(candles, tf, tail=tail)

    # Decision code often asks for "1"; if only a sub-minute continuous run was
    # supplied, alias the densest available series so Gate 1 still sees history.
    if "1" not in tf_packets and tf_packets:
        densest = max(tf_packets.values(), key=lambda p: p.candle_count)
        tf_packets["1"] = densest.model_copy(update={"timeframe": "1"})

    primary = tf_packets.get("1") or next(iter(tf_packets.values()), None)
    price = None
    if primary and primary.ohlcv_tail:
        price = primary.ohlcv_tail[-1].get("close")

    snap = market or (safety.market if safety else MarketSnapshot())
    slip = None
    if safety and safety.sellability.price_impact_pct is not None:
        slip = safety.sellability.price_impact_pct * 100.0  # pct → bps

    return MarketPacket(
        address=address,
        mint=mint or (safety.mint if safety else None) or address,
        name=name or (snap.symbol if snap else None),
        as_of=datetime.now(timezone.utc),
        price=price if price is not None else snap.price_usd,
        timeframes=tf_packets,
        orderflow=_orderflow_from_sources(snap, axiom_stats),
        safety=safety,
        market=snap,
        est_slippage_bps=slip,
        est_round_trip_cost_pct=_est_round_trip_cost_pct(slippage_bps=slip),
        source=source,
    )


def packet_from_corpus(
    token: dataset.TokenRef,
    *,
    timeframe: str = "1",
    tail: int = DEFAULT_TAIL,
    wanted: tuple[str, ...] = DEFAULT_TIMEFRAMES,
) -> MarketPacket:
    """OHLCV-only packet. No live mint resolution — corpus addresses are stale."""
    header, _ = dataset.load_timeframe(token.candles_path, timeframe, tail=1)
    loaded: dict[str, list[dict[str, Any]]] = {}
    for tf in wanted:
        try:
            _, candles = dataset.load_timeframe(token.candles_path, tf, tail=tail)
        except Exception:  # noqa: BLE001
            continue
        if candles:
            loaded[tf] = candles

    return packet_from_candles(
        address=token.address,
        name=header.get("name"),
        timeframes=loaded,
        mint=None,
        safety=None,
        source="corpus",
        tail=tail,
        wanted=wanted,
    )


def packet_from_live_token(
    address: str,
    token: dict[str, Any],
    *,
    safety: Optional[SafetyReport] = None,
    tail: int = DEFAULT_TAIL,
) -> MarketPacket:
    timeframes = token.get("timeframes") or {}
    mint = (
        (safety.mint if safety else None)
        or token.get("mint")
        or address
    )
    return packet_from_candles(
        address=address,
        name=token.get("name") or (safety.market.symbol if safety else None),
        timeframes=timeframes,
        mint=mint,
        safety=safety,
        axiom_stats=token.get("stats") or [],
        source="live",
        tail=tail,
    )


def _sum_stat_buckets(
    buckets: list[dict[str, Any]],
    *,
    last_n: int,
) -> dict[str, Any]:
    """Sum Axiom pair-stats buckets. Each bucket is ~1 minute of flow."""
    if not buckets or last_n <= 0:
        return {}
    window = buckets[-last_n:]
    buys = sells = 0
    buy_vol = sell_vol = 0.0
    for b in window:
        try:
            buys += int(b.get("buyCount") or 0)
            sells += int(b.get("sellCount") or 0)
            buy_vol += float(b.get("buyVolumeSol") or 0.0)
            sell_vol += float(b.get("sellVolumeSol") or 0.0)
        except (TypeError, ValueError):
            continue
    total = buys + sells
    ratio = round(buys / max(sells, 1), 3) if total > 0 else None
    return {
        f"buys_{last_n}m": buys,
        f"sells_{last_n}m": sells,
        f"buy_volume_sol_{last_n}m": round(buy_vol, 6),
        f"sell_volume_sol_{last_n}m": round(sell_vol, 6),
        f"buy_sell_ratio_{last_n}m": ratio,
        f"stats_buckets_{last_n}m": len(window),
    }


def _orderflow_from_sources(
    market: MarketSnapshot,
    axiom_stats: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Prefer Axiom pair-stats 5m flow; fall back to DexScreener h1."""
    buys_h1 = market.buys_h1
    sells_h1 = market.sells_h1
    ratio_h1 = None
    if isinstance(buys_h1, int) and isinstance(sells_h1, int) and (buys_h1 + sells_h1) > 0:
        ratio_h1 = round(buys_h1 / max(sells_h1, 1), 3)

    out: dict[str, Any] = {
        "buys_h1": buys_h1,
        "sells_h1": sells_h1,
        "buy_sell_ratio": ratio_h1,
        "volume_h1": market.volume_h1,
        "volume_h24": market.volume_h24,
        "source": "dexscreener_h1",
    }

    buckets = [b for b in (axiom_stats or []) if isinstance(b, dict)]
    if not buckets:
        return out

    # Sort by createdAt so "last N" is chronological even if ingest order varies.
    def _ts(b: dict[str, Any]) -> str:
        return str(b.get("createdAt") or "")

    buckets = sorted(buckets, key=_ts)
    out.update(_sum_stat_buckets(buckets, last_n=1))
    out.update(_sum_stat_buckets(buckets, last_n=5))
    # Scalp decision uses 5m as the primary imbalance signal.
    ratio_5m = out.get("buy_sell_ratio_5m")
    buys_5m = out.get("buys_5m")
    sells_5m = out.get("sells_5m")
    if isinstance(buys_5m, int) and isinstance(sells_5m, int) and buys_5m + sells_5m > 0:
        out["buys"] = buys_5m
        out["sells"] = sells_5m
        out["buy_sell_ratio"] = ratio_5m
        out["source"] = "axiom_pair_stats_5m"
    return out


# Back-compat alias used by older call sites / tests.
def _orderflow_from_stats(market: MarketSnapshot) -> dict[str, Any]:
    return _orderflow_from_sources(market, None)


# Cost / edge thresholds used by Gate 2 (env-tunable).
# Defaults are scalp-oriented: young Solana memes, small size, ~$1 net target.
MIN_EDGE_PCT = _env_float("DECIDE_MIN_EDGE_PCT", 1.5)
HORIZON_BARS = _env_int("DECIDE_HORIZON_BARS", 6)
COVERAGE_TARGET = _env_float("DECIDE_COVERAGE_TARGET", 0.8)
# Forecast backends need ≥16 closes; keep Gate 1 aligned with that floor.
MIN_HISTORY_BARS = _env_int("DECIDE_MIN_HISTORY_BARS", 16)
SCALP_SIZE_USD = _env_float("DECIDE_SCALP_SIZE_USD", 40.0)
TARGET_PROFIT_USD = _env_float("DECIDE_TARGET_PROFIT_USD", 1.0)
# Floor on the confidence multiplier applied to scalp size. Because the profit
# target is a fixed dollar amount, shrinking size raises the percent move the
# trade must catch — so sizing down on low confidence makes the trade harder to
# win, not safer.
MIN_SIZE_FRACTION = _env_float("DECIDE_MIN_SIZE_FRACTION", 0.25)
# Paper-only: Gate 2 uses this fraction of the $1 target (live uses 1.0).
PAPER_GATE2_EDGE_FRACTION = _env_float("PAPER_GATE2_EDGE_FRACTION", 0.55)
MAX_SLIPPAGE_BPS = _env_float("DECIDE_MAX_SLIPPAGE_BPS", 1200.0)
MIN_LIQUIDITY_USD = _env_float("DECIDE_MIN_LIQUIDITY_USD", 1_000.0)

# Prefer dense short TFs so minutes-old tokens can still decide.
SCALP_TF_PRIORITY = ("5S", "15S", "30S", "1", "3", "5", "15", "30", "60")
MIN_BARS_BY_TF = {
    "5S": 16,   # ~80 seconds
    "15S": 16,  # ~4 minutes
    "30S": 16,  # ~8 minutes
    "1": 16,    # ~16 minutes
    "3": 16,
    "5": 16,
    "15": 16,
    "30": 16,
    "60": 16,
}
HORIZON_BY_TF = {
    "5S": 6,   # ~30s
    "15S": 6,  # ~90s
    "30S": 6,  # ~3m
    "1": 5,    # ~5m
    "3": 4,
    "5": 3,
}


def min_bars_for_tf(timeframe: str) -> int:
    return int(MIN_BARS_BY_TF.get(timeframe, MIN_HISTORY_BARS))


def horizon_for_tf(timeframe: str, requested: Optional[int] = None) -> int:
    if requested is not None and requested > 0:
        return requested
    return int(HORIZON_BY_TF.get(timeframe, HORIZON_BARS))


def pick_decision_timeframe(
    packet: MarketPacket,
    preferred: str = "1",
) -> str:
    """Choose a TF with enough bars for a short-horizon decision.

    Young memecoins often lack 1m depth; 5S/15S seeds from the extension are
    enough to act within the first minutes of life.
    """
    tfs = packet.timeframes or {}
    if preferred in tfs and tfs[preferred].candle_count >= min_bars_for_tf(preferred):
        return preferred

    for tf in SCALP_TF_PRIORITY:
        pkt = tfs.get(tf)
        if pkt is not None and pkt.candle_count >= min_bars_for_tf(tf):
            return tf

    # Last resort: densest available series (may still fail Gate 1).
    if tfs:
        return max(tfs, key=lambda k: tfs[k].candle_count)
    return preferred


def expected_profit_usd(edge_pct: float, *, size_usd: float = SCALP_SIZE_USD) -> float:
    return size_usd * float(edge_pct) / 100.0


def min_edge_pct_for_target(
    *,
    size_usd: float = SCALP_SIZE_USD,
    target_usd: float = TARGET_PROFIT_USD,
) -> float:
    if size_usd <= 0:
        return MIN_EDGE_PCT
    return max(MIN_EDGE_PCT, (target_usd / size_usd) * 100.0)


def gate2_thresholds(*, live_trading: bool) -> tuple[float, float]:
    """Return (min_edge_pct, min_profit_usd) for Gate 2 buy hurdle."""
    profit_target = TARGET_PROFIT_USD
    if not live_trading:
        profit_target = TARGET_PROFIT_USD * PAPER_GATE2_EDGE_FRACTION
    need_edge = min_edge_pct_for_target(
        size_usd=SCALP_SIZE_USD,
        target_usd=profit_target,
    )
    return need_edge, profit_target
