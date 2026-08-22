"""Phase 3 — memecoin desk specialists + risk committee.

Free-tier path: deterministic specialists (no LLM). ``mode=deep`` optionally
asks Antigravity CLI for a one-shot review; missing auth degrades cleanly.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Optional

from decision.config import _env_float
from decision.schema import (
    Action,
    DecisionCard,
    DecideMode,
    Direction,
    Driver,
    MarketPacket,
)

# Window return above which momentum is a blowoff to fade, not a trend to join.
BLOWOFF_RETURN_PCT = _env_float("VIBE_BLOWOFF_RETURN_PCT", 50.0)
EXTENDED_RETURN_PCT = _env_float("VIBE_EXTENDED_RETURN_PCT", 20.0)
# Below this ensemble agreement the forecast carries no directional information.
MIN_AGREEMENT_TO_BUY = _env_float("VIBE_MIN_AGREEMENT", 0.35)


@dataclass
class SpecialistOpinion:
    name: str
    vote: str  # buy | hold | avoid
    confidence: float
    claim: str
    weight: float = 1.0


@dataclass
class DeskReview:
    opinions: list[SpecialistOpinion] = field(default_factory=list)
    risk_pass: bool = True
    veto_reasons: list[str] = field(default_factory=list)
    action_override: Optional[Action] = None
    drivers: list[Driver] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    backend: str = "heuristic"


def review_card(
    card: DecisionCard,
    packet: MarketPacket,
    *,
    mode: DecideMode = DecideMode.FULL,
) -> DeskReview:
    """Run specialist swarm + risk veto. Never upgrades avoid→buy."""
    opinions = _run_specialists(card, packet)
    review = _risk_committee(card, packet, opinions)
    review.opinions = opinions
    review.drivers = [
        Driver(
            source=f"vibe:{o.name}",
            claim=f"{o.vote} ({o.confidence:.0%}): {o.claim}",
            weight=o.weight,
        )
        for o in opinions
    ]

    if mode is DecideMode.DEEP:
        deep = _deep_agy_review(card, packet)
        if deep is not None:
            review.warnings.append("deep agy review applied")
            review.backend = "heuristic+agy"
            if deep.get("veto"):
                review.risk_pass = False
                review.veto_reasons.append(str(deep.get("reason") or "agy veto"))
                review.action_override = Action.AVOID
            elif deep.get("action") in ("hold", "avoid", "buy"):
                proposed = Action(deep["action"])
                if proposed is Action.BUY and not review.risk_pass:
                    review.warnings.append("agy buy ignored — risk committee veto stands")
                elif proposed is Action.BUY and card.action is not Action.BUY:
                    review.warnings.append("agy buy ignored — fast path did not clear Gate 2")
                else:
                    review.action_override = proposed
            if deep.get("claim"):
                review.drivers.append(
                    Driver(source="vibe:agy", claim=str(deep["claim"]), weight=1.2)
                )
        else:
            review.warnings.append("deep mode: agy unavailable — heuristic desk only")

    return review


def apply_review(card: DecisionCard, review: DeskReview) -> DecisionCard:
    """Mutate a DecisionCard with desk / risk outcomes (Gate 3)."""
    card.drivers = list(card.drivers) + list(review.drivers)
    card.warnings = list(card.warnings) + list(review.warnings)
    card.gates_passed = list(card.gates_passed)
    card.gates_failed = list(card.gates_failed)

    if review.risk_pass:
        if "gate3" not in card.gates_passed:
            card.gates_passed.append("gate3")
    else:
        card.gates_failed.append("gate3")
        card.action = Action.AVOID
        card.risk.risk_pass = False
        card.risk.veto_reasons = list(review.veto_reasons)
        card.position.max_size_usd = 0.0
        card.position.size_basis = "risk_veto"
        card.confidence_basis = "risk_committee_veto"
        return card

    if review.action_override is not None:
        card.action = review.action_override
        if review.action_override is not Action.BUY:
            card.position.max_size_usd = 0.0
            card.position.size_basis = "desk_override"

    card.risk.risk_pass = card.action is Action.BUY
    return card


def _run_specialists(card: DecisionCard, packet: MarketPacket) -> list[SpecialistOpinion]:
    specs = (
        _momentum_specialist,
        _liquidity_specialist,
        _holder_specialist,
        _forecast_specialist,
        _orderflow_specialist,
    )
    opinions: list[SpecialistOpinion] = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = [pool.submit(fn, card, packet) for fn in specs]
        for fut in as_completed(futs):
            opinions.append(fut.result())
    return opinions


def _momentum_specialist(card: DecisionCard, packet: MarketPacket) -> SpecialistOpinion:
    """Reports trailing momentum without voting on it.

    tools/barrier_study.py replayed ~10k entries from the archived corpus and
    found forward outcomes essentially flat across every trailing-return bucket:
    a +50-200% blowoff averaged -$0.75 and a flat window -$0.94, both within
    noise of the round-trip cost. Trailing return carries no usable directional
    signal here, so this desk describes the tape and abstains. It used to vote
    buy on anything above +3%, which is how a +855% vertical move became a buy.
    """
    tf = packet.timeframes.get(card.timeframe)
    closes = [r.get("close") for r in (tf.ohlcv_tail if tf else []) if r.get("close") is not None]
    if len(closes) < 8:
        return SpecialistOpinion("momentum", "hold", 0.4, "too few closes for momentum", 0.0)
    ret = (closes[-1] / closes[0] - 1.0) * 100.0
    if ret > BLOWOFF_RETURN_PCT:
        shape = "blowoff"
    elif ret > EXTENDED_RETURN_PCT:
        shape = "extended"
    elif ret > 3:
        shape = "rising"
    elif ret < -3:
        shape = "falling"
    else:
        shape = "flat"
    # weight=0 keeps this out of the buy/avoid tally: informational only.
    return SpecialistOpinion(
        "momentum", "hold", 0.5, f"{shape}: window return {ret:+.1f}%", 0.0
    )


def _liquidity_specialist(card: DecisionCard, packet: MarketPacket) -> SpecialistOpinion:
    liq = packet.market.liquidity_usd_total or packet.market.liquidity_usd
    impact = None
    if packet.safety and packet.safety.sellability:
        impact = packet.safety.sellability.price_impact_pct
    if liq is not None and liq < 1000:
        return SpecialistOpinion("liquidity", "avoid", 0.8, f"liq ${liq:,.0f} too thin", 1.2)
    if impact is not None and impact > 15:
        return SpecialistOpinion("liquidity", "avoid", 0.75, f"exit impact {impact:.1f}%", 1.2)
    if liq is not None and liq >= 5000 and (impact is None or impact < 5):
        return SpecialistOpinion("liquidity", "buy", 0.55, f"liq ${liq:,.0f}, impact ok", 1.0)
    return SpecialistOpinion(
        "liquidity",
        "hold",
        0.5,
        f"liq {liq if liq is not None else 'n/a'}, impact {impact if impact is not None else 'n/a'}",
        1.0,
    )


def _holder_specialist(card: DecisionCard, packet: MarketPacket) -> SpecialistOpinion:
    if not packet.safety or not packet.safety.holders:
        return SpecialistOpinion("holders", "hold", 0.4, "holder data missing", 0.7)
    h = packet.safety.holders
    top = h.top10_pct_ex_largest if h.top10_pct_ex_largest is not None else h.top10_pct
    if top is not None and top > 60:
        return SpecialistOpinion("holders", "avoid", 0.8, f"top10 ex-pool {top:.0f}%", 1.3)
    if top is not None and top < 35:
        return SpecialistOpinion("holders", "buy", 0.55, f"top10 ex-pool {top:.0f}%", 1.0)
    return SpecialistOpinion("holders", "hold", 0.5, f"top10 ex-pool {top}", 1.0)


def _forecast_specialist(card: DecisionCard, packet: MarketPacket) -> SpecialistOpinion:
    band = card.expected_return_pct
    agree = card.forecast.agreement
    if card.direction is Direction.UP and band.p50 >= 2 and agree >= 0.4:
        return SpecialistOpinion(
            "forecast",
            "buy",
            min(0.85, 0.45 + agree),
            f"p50={band.p50:+.2f}% agree={agree:.2f}",
            1.3,
        )
    if card.direction is Direction.DOWN or band.p50 <= -1:
        return SpecialistOpinion(
            "forecast",
            "avoid",
            0.7,
            f"p50={band.p50:+.2f}%",
            1.3,
        )
    return SpecialistOpinion(
        "forecast",
        "hold",
        0.5,
        f"p50={band.p50:+.2f}% agree={agree:.2f}",
        1.1,
    )


def _orderflow_specialist(card: DecisionCard, packet: MarketPacket) -> SpecialistOpinion:
    of = packet.orderflow or {}
    # Prefer Axiom pair-stats 5m (Token Info buy/sell/vol) when streamed.
    buys = of.get("buys_5m") if of.get("buys_5m") is not None else of.get("buys_h1")
    sells = of.get("sells_5m") if of.get("sells_5m") is not None else of.get("sells_h1")
    ratio = of.get("buy_sell_ratio")
    window = "5m" if of.get("source") == "axiom_pair_stats_5m" else "h1"
    # 5m windows are short — lower the trade-count floor vs the old h1 gate.
    min_trades = 8 if window == "5m" else 20
    if isinstance(buys, int) and isinstance(sells, int) and buys + sells >= min_trades:
        if sells == 0 and buys >= min_trades:
            return SpecialistOpinion(
                "orderflow", "avoid", 0.85, f"{buys} buys / 0 sells ({window})", 1.4
            )
        if ratio is not None and ratio >= 1.8:
            return SpecialistOpinion(
                "orderflow", "buy", 0.55, f"buy/sell {ratio} ({window})", 1.0
            )
        if ratio is not None and ratio <= 0.55:
            return SpecialistOpinion(
                "orderflow", "avoid", 0.6, f"buy/sell {ratio} ({window})", 1.0
            )
        return SpecialistOpinion(
            "orderflow",
            "hold",
            0.5,
            f"buy/sell {ratio} ({window}, {buys}b/{sells}s)",
            0.9,
        )
    return SpecialistOpinion("orderflow", "hold", 0.45, "orderflow inconclusive", 0.8)


def _risk_committee(
    card: DecisionCard,
    packet: MarketPacket,
    opinions: list[SpecialistOpinion],
) -> DeskReview:
    review = DeskReview()
    buy_w = sum(o.weight for o in opinions if o.vote == "buy")
    avoid_w = sum(o.weight for o in opinions if o.vote == "avoid")

    if packet.safety and packet.safety.blocking:
        review.risk_pass = False
        review.veto_reasons.append("safety blocking")
    if packet.safety and packet.safety.sellability and packet.safety.sellability.sellable is False:
        review.risk_pass = False
        review.veto_reasons.append("no exit route")
    if avoid_w >= buy_w + 1.5 and card.action is Action.BUY:
        review.risk_pass = False
        review.veto_reasons.append(f"specialist avoid weight {avoid_w:.1f} > buy {buy_w:.1f}")
    if card.cost_adjusted_edge_pct < 0 and card.action is Action.BUY:
        review.risk_pass = False
        review.veto_reasons.append("negative cost-adjusted edge")
    agree = card.forecast.agreement if card.forecast else 1.0
    if card.action is Action.BUY and agree < MIN_AGREEMENT_TO_BUY:
        review.risk_pass = False
        review.veto_reasons.append(
            f"ensemble agreement {agree:.2f} < {MIN_AGREEMENT_TO_BUY:.2f} — no directional signal"
        )
    # A band that cannot lose is a calibration failure, not an opportunity.
    band = card.expected_return_pct
    if card.action is Action.BUY and band and band.p10 > 0:
        review.risk_pass = False
        review.veto_reasons.append(
            f"implausible interval: p10={band.p10:+.2f}% > 0 implies riskless trade"
        )

    if not review.risk_pass:
        review.action_override = Action.AVOID
    elif card.action is Action.BUY and buy_w < 1.5:
        review.action_override = Action.HOLD
        review.warnings.append("weak specialist buy consensus → hold")
    return review


def _deep_agy_review(card: DecisionCard, packet: MarketPacket) -> Optional[dict[str, Any]]:
    try:
        from decision import agy_cli
    except Exception:
        return None
    status = agy_cli.preflight()
    if not status.ready:
        return None

    prompt = (
        "You are the risk chair for a Solana memecoin scalp desk. "
        "Reply ONLY with JSON: "
        '{"action":"buy|hold|avoid","veto":true|false,"reason":"...","claim":"..."}. '
        "Never invent on-chain facts. Prefer avoid when unsure.\n\n"
        f"card={card.summary()}\n"
        f"token={card.token}\n"
        f"edge_pct={card.cost_adjusted_edge_pct}\n"
        f"safety={card.safety}\n"
        f"market_liq={packet.market.liquidity_usd_total or packet.market.liquidity_usd}\n"
    )
    try:
        return agy_cli.ask_json(prompt, timeout_s=90)
    except Exception as exc:  # noqa: BLE001
        return {"veto": False, "action": "hold", "claim": f"agy error: {exc}", "reason": str(exc)}
