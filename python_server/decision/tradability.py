"""Gate 1 — tradability. Reject before spending on forecast/LLM.

Tuned for young Solana memecoins: decide on whatever dense TF we have
(5S/15S/…) as soon as forecast has enough bars (~16), not after an hour of 1m.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from decision.packet import (
    MAX_SLIPPAGE_BPS,
    MIN_HISTORY_BARS,
    MIN_LIQUIDITY_USD,
    min_bars_for_tf,
)
from decision.schema import MarketPacket


@dataclass
class TradabilityResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def check_tradability(
    packet: MarketPacket,
    *,
    timeframe: str = "1",
    min_history: int | None = None,
    max_slippage_bps: float = MAX_SLIPPAGE_BPS,
    min_liquidity_usd: float = MIN_LIQUIDITY_USD,
) -> TradabilityResult:
    reasons: list[str] = []
    warnings: list[str] = []

    need = min_history if min_history is not None else min_bars_for_tf(timeframe)
    tf = packet.timeframes.get(timeframe)
    if tf is None or tf.candle_count < need:
        have = tf.candle_count if tf else 0
        reasons.append(
            f"insufficient history on {timeframe}: {have} bars < {need}"
        )
    elif tf.candle_count < MIN_HISTORY_BARS * 2:
        warnings.append(
            f"short history on {timeframe}: {tf.candle_count} bars (scalp mode)"
        )

    # Live packets may carry market depth; corpus packets usually do not —
    # missing liquidity is a warning for corpus, a hard fail for live only when
    # we actually measured thin depth.
    liq = packet.market.liquidity_usd_total or packet.market.liquidity_usd
    if liq is not None and liq < min_liquidity_usd:
        reasons.append(f"liquidity ${liq:,.0f} below ${min_liquidity_usd:,.0f} floor")
    elif liq is None and packet.source == "live":
        warnings.append("liquidity not measured")

    slip = packet.est_slippage_bps
    if slip is not None and slip > max_slippage_bps:
        reasons.append(
            f"estimated slippage {slip:.0f} bps exceeds {max_slippage_bps:.0f} bps"
        )
    elif slip is not None and slip > 500:
        warnings.append(f"elevated slippage {slip:.0f} bps — size carefully")

    if packet.safety and packet.safety.sellability.sellable is False:
        reasons.append("token is not sellable (no exit route)")

    return TradabilityResult(passed=not reasons, reasons=reasons, warnings=warnings)
