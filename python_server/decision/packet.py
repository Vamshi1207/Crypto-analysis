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
        orderflow=_orderflow_from_stats(snap),
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
        source="live",
        tail=tail,
    )


def _orderflow_from_stats(market: MarketSnapshot) -> dict[str, Any]:
    buys = market.buys_h1
    sells = market.sells_h1
    ratio = None
    if isinstance(buys, int) and isinstance(sells, int) and (buys + sells) > 0:
        ratio = round(buys / max(sells, 1), 3)
    return {
        "buys_h1": buys,
        "sells_h1": sells,
        "buy_sell_ratio": ratio,
        "volume_h1": market.volume_h1,
        "volume_h24": market.volume_h24,
    }


# Cost / edge thresholds used by Gate 2 (env-tunable).
# Defaults are scalp-oriented: young Solana memes, small size, ~$1 net target.
MIN_EDGE_PCT = _env_float("DECIDE_MIN_EDGE_PCT", 1.5)
HORIZON_BARS = _env_int("DECIDE_HORIZON_BARS", 6)
COVERAGE_TARGET = _env_float("DECIDE_COVERAGE_TARGET", 0.8)
# Forecast backends need ≥16 closes; keep Gate 1 aligned with that floor.
MIN_HISTORY_BARS = _env_int("DECIDE_MIN_HISTORY_BARS", 16)
SCALP_SIZE_USD = _env_float("DECIDE_SCALP_SIZE_USD", 40.0)
TARGET_PROFIT_USD = _env_float("DECIDE_TARGET_PROFIT_USD", 1.0)
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
