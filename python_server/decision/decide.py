"""Decision orchestrator: Gates 0–4 + calibrated forecast → DecisionCard.

`mode=fast`  — Gates 0–2 + forecast (Phase 2)
`mode=full`  — + heuristic specialist desk + risk veto (Phase 3)
`mode=deep`  — + optional Antigravity CLI chair review
Paper execute / swarm loop live outside this function (Phase 4–5).
"""

from __future__ import annotations

import time
from typing import Any, Optional

from decision import calibrate, dataset, forecast, packet as packet_mod
from decision import indicators_signal
from decision import pipeline_log
from decision import store as decision_store
from decision.packet import (
    COVERAGE_TARGET,
    HORIZON_BARS,
    MIN_EDGE_PCT,
    SCALP_SIZE_USD,
    TARGET_PROFIT_USD,
    expected_profit_usd,
    horizon_for_tf,
    min_edge_pct_for_target,
    pick_decision_timeframe,
)
from decision.safety import check_token
from decision.schema import (
    Action,
    DecisionCard,
    DecideMode,
    Direction,
    Driver,
    ForecastEnsemble,
    MarketPacket,
    PositionPlan,
    PriceTargets,
    ReturnBand,
    RiskBlock,
    SafetyReport,
    SafetyVerdict,
)
from decision.sources import SourceError, resolve_to_mint
from decision.tradability import check_tradability
from decision import vibe as vibe_mod


def _resolve_gate_mint(
    *,
    address: Optional[str],
    mint: Optional[str],
) -> tuple[Optional[str], Optional[dict[str, Any]], Optional[str]]:
    """Return (mint_for_gate0, resolved_meta, resolve_error).

    Axiom live URLs usually carry a pool/pair id. Gate 0 needs the SPL mint.
    """
    if mint and mint != address:
        # Caller already supplied an explicit mint distinct from the candle key.
        return mint, {"mint": mint, "source": "caller", "pool": address}, None

    candidate = mint or address
    if not candidate:
        return None, None, "no address/mint to resolve"

    try:
        resolved = resolve_to_mint(candidate)
        return resolved["mint"], resolved, None
    except SourceError as exc:
        return None, None, str(exc)


def decide(
    *,
    address: Optional[str] = None,
    mint: Optional[str] = None,
    mode: DecideMode | str = DecideMode.FAST,
    timeframe: str = "1",
    horizon: int = HORIZON_BARS,
    live_token: Optional[dict[str, Any]] = None,
    corpus_address: Optional[str] = None,
    skip_safety: bool = False,
    run_safety: bool = True,
) -> DecisionCard:
    """Produce a DecisionCard.

    Sources (first match wins):
      * ``live_token`` — in-memory Flask token_data entry
      * ``corpus_address`` — historical OHLCV file key (no live chain calls for candles)
      * ``address`` / ``mint`` — live Gate 0 only; candles still need live_token

    For live Axiom streams, ``address`` is typically the pool id from the URL.
    Gate 0 resolves that to an SPL mint automatically.
    """
    started = time.perf_counter()
    mode = DecideMode(mode) if not isinstance(mode, DecideMode) else mode
    warnings: list[str] = []
    gates_passed: list[str] = []
    gates_failed: list[str] = []
    live_name = (live_token or {}).get("name") if live_token else None

    with pipeline_log.run(prefix="dec-") as run_id:
        pipeline_log.emit(
            "decide",
            "start",
            address=address or corpus_address,
            mint=mint,
            symbol=live_name,
            mode=mode.value,
            timeframe=timeframe,
            horizon=horizon,
            has_live_token=live_token is not None,
            source=(
                (live_token or {}).get("source")
                if live_token
                else ("corpus" if corpus_address else "thin")
            ),
        )
        try:
            card = _decide_body(
                address=address,
                mint=mint,
                mode=mode,
                timeframe=timeframe,
                horizon=horizon,
                live_token=live_token,
                corpus_address=corpus_address,
                skip_safety=skip_safety,
                run_safety=run_safety,
                started=started,
                warnings=warnings,
                gates_passed=gates_passed,
                gates_failed=gates_failed,
            )
            pipeline_log.emit_decide_card(card, run_id=run_id)
            return card
        except Exception as exc:
            pipeline_log.emit(
                "decide",
                "error",
                level="error",
                address=address or corpus_address,
                mint=mint,
                symbol=live_name,
                reason=f"{type(exc).__name__}: {exc}",
                duration_ms=pipeline_log.timed_ms(started),
            )
            raise


def _decide_body(
    *,
    address: Optional[str],
    mint: Optional[str],
    mode: DecideMode,
    timeframe: str,
    horizon: int,
    live_token: Optional[dict[str, Any]],
    corpus_address: Optional[str],
    skip_safety: bool,
    run_safety: bool,
    started: float,
    warnings: list[str],
    gates_passed: list[str],
    gates_failed: list[str],
) -> DecisionCard:
    if mode in (DecideMode.FULL, DecideMode.DEEP):
        warnings.append(f"mode={mode.value}: running specialist desk + risk committee")
    elif mode is not DecideMode.FAST:
        warnings.append(f"mode={mode.value} unknown; running fast path")

    safety: Optional[SafetyReport] = None
    market_packet: MarketPacket
    resolved_meta: Optional[dict[str, Any]] = None
    gate_mint: Optional[str] = mint

    if run_safety and not skip_safety and (mint or address):
        gate_mint, resolved_meta, resolve_err = _resolve_gate_mint(
            address=address, mint=mint
        )
        if resolve_err:
            warnings.append(f"mint resolve failed: {resolve_err}")
            # Thin packet so the avoid card still has an identity.
            market_packet = packet_mod.packet_from_candles(
                address=address or mint or "unknown",
                name=(live_token or {}).get("name") if live_token else None,
                timeframes=(live_token or {}).get("timeframes") or {},
                mint=None,
                safety=None,
                source="live",
            )
            gates_failed.append("gate0")
            pipeline_log.emit(
                "gate0",
                "fail",
                level="warning",
                address=address,
                reason=f"could not resolve mint: {resolve_err}",
            )
            return _avoid_card(
                market_packet,
                mode=mode,
                horizon=horizon,
                timeframe=timeframe,
                reason=f"could not resolve mint: {resolve_err}",
                warnings=warnings,
                gates_passed=gates_passed,
                gates_failed=gates_failed + ["gate0_block"],
                started=started,
                safety=None,
            )
        if resolved_meta and resolved_meta.get("source") == "dexscreener_pair":
            warnings.append(
                f"resolved pool→mint via DexScreener ({resolved_meta.get('mint')})"
            )
            pipeline_log.emit(
                "gate0",
                "resolve",
                address=address,
                mint=gate_mint,
                symbol=resolved_meta.get("symbol"),
                resolve_source=resolved_meta.get("source"),
            )

    if live_token is not None and address:
        if run_safety and not skip_safety and gate_mint:
            safety = check_token(gate_mint)
        market_packet = packet_mod.packet_from_live_token(
            address, live_token, safety=safety
        )
        if gate_mint:
            market_packet.mint = gate_mint
    elif corpus_address or (address and not run_safety):
        key = corpus_address or address
        assert key is not None
        refs = {t.address: t for t in dataset.list_tokens()}
        if key not in refs:
            raise FileNotFoundError(f"no corpus candles for {key}")
        market_packet = packet_mod.packet_from_corpus(refs[key], timeframe=timeframe)
        skip_safety = True
        warnings.append("corpus OHLCV path: Gate 0 skipped (addresses are historical)")
    else:
        # Safety-only / thin packet — forecast will fail Gate 1 on history.
        if run_safety and not skip_safety and gate_mint:
            safety = check_token(gate_mint)
        market_packet = packet_mod.packet_from_candles(
            address=address or (gate_mint or "unknown"),
            name=safety.market.symbol if safety else None,
            timeframes={},
            mint=gate_mint,
            safety=safety,
            source="live",
        )
        warnings.append("no candle payload supplied; forecast needs live_token or corpus_address")

    # --- Gate 0 ---
    if skip_safety:
        gates_passed.append("gate0_skipped")
        pipeline_log.emit(
            "gate0",
            "skip",
            address=market_packet.address,
            mint=market_packet.mint,
            symbol=market_packet.name,
        )
    elif safety is None:
        gates_failed.append("gate0")
        warnings.append("safety report missing")
        pipeline_log.emit(
            "gate0",
            "fail",
            level="warning",
            address=market_packet.address,
            mint=market_packet.mint,
            reason="safety unavailable",
        )
        return _avoid_card(
            market_packet,
            mode=mode,
            horizon=horizon,
            timeframe=timeframe,
            reason="safety unavailable",
            warnings=warnings,
            gates_passed=gates_passed,
            gates_failed=gates_failed,
            started=started,
            safety=safety,
        )
    elif safety.blocking:
        gates_failed.append("gate0")
        pipeline_log.emit(
            "gate0",
            "fail",
            level="warning",
            address=market_packet.address,
            mint=market_packet.mint,
            symbol=market_packet.name,
            verdict=safety.verdict.value,
            risk_score=safety.risk_score,
            reason=safety.summary(),
        )
        return _avoid_card(
            market_packet,
            mode=mode,
            horizon=horizon,
            timeframe=timeframe,
            reason=safety.summary(),
            warnings=warnings,
            gates_passed=gates_passed,
            gates_failed=gates_failed + ["gate0_block"],
            started=started,
            safety=safety,
        )
    else:
        gates_passed.append("gate0")
        pipeline_log.emit(
            "gate0",
            "pass",
            address=market_packet.address,
            mint=market_packet.mint,
            symbol=market_packet.name,
            verdict=safety.verdict.value,
            risk_score=safety.risk_score,
            degraded=safety.degraded,
        )
        if safety.verdict is SafetyVerdict.CAUTION:
            warnings.append(f"safety caution: {safety.summary()}")

    # --- Gate 1 ---
    # Prefer a dense short TF so minutes-old tokens can still clear history.
    chosen_tf = pick_decision_timeframe(market_packet, preferred=timeframe)
    if chosen_tf != timeframe:
        warnings.append(f"scalp TF {chosen_tf} (requested {timeframe})")
        timeframe = chosen_tf
    # Live scalp: short horizon on dense TFs. Corpus / explicit stays as requested.
    if market_packet.source == "live":
        horizon = horizon_for_tf(timeframe, None)
    else:
        horizon = horizon_for_tf(timeframe, horizon)

    tradable = check_tradability(market_packet, timeframe=timeframe)
    warnings.extend(tradable.warnings)
    if not tradable.passed:
        gates_failed.append("gate1")
        pipeline_log.emit(
            "gate1",
            "fail",
            level="warning",
            address=market_packet.address,
            mint=market_packet.mint,
            symbol=market_packet.name,
            timeframe=timeframe,
            reason="; ".join(tradable.reasons),
        )
        return _hold_card(
            market_packet,
            mode=mode,
            horizon=horizon,
            timeframe=timeframe,
            reason="; ".join(tradable.reasons),
            warnings=warnings,
            gates_passed=gates_passed,
            gates_failed=gates_failed,
            started=started,
            safety=safety,
            action=Action.HOLD,
        )
    gates_passed.append("gate1")
    pipeline_log.emit(
        "gate1",
        "pass",
        address=market_packet.address,
        mint=market_packet.mint,
        symbol=market_packet.name,
        timeframe=timeframe,
        horizon=horizon,
    )

    # --- Forecast + conformal ---
    tf = market_packet.timeframes[timeframe]
    closes = [row["close"] for row in tf.ohlcv_tail]
    try:
        ens = forecast.forecast_closes(
            closes, horizon=horizon, timeframe=timeframe, coverage_target=COVERAGE_TARGET
        )
        ens = calibrate.apply_conformal(ens)
    except Exception as exc:  # noqa: BLE001
        gates_failed.append("forecast")
        pipeline_log.emit(
            "forecast",
            "fail",
            level="error",
            address=market_packet.address,
            mint=market_packet.mint,
            symbol=market_packet.name,
            reason=str(exc),
        )
        return _hold_card(
            market_packet,
            mode=mode,
            horizon=horizon,
            timeframe=timeframe,
            reason=f"forecast failed: {exc}",
            warnings=warnings,
            gates_passed=gates_passed,
            gates_failed=gates_failed,
            started=started,
            safety=safety,
        )
    gates_passed.append("forecast")
    pipeline_log.emit(
        "forecast",
        "pass",
        address=market_packet.address,
        mint=market_packet.mint,
        symbol=market_packet.name,
        p50=ens.calibrated.p50,
        p10=ens.calibrated.p10,
        p90=ens.calibrated.p90,
        agreement=ens.agreement,
        backend=ens.backend,
        residual_count=ens.residual_count,
    )

    band = ens.calibrated
    cost = market_packet.est_round_trip_cost_pct or 2.0
    edge = band.p50 - cost
    profit_usd = expected_profit_usd(edge, size_usd=SCALP_SIZE_USD)
    need_edge = min_edge_pct_for_target(
        size_usd=SCALP_SIZE_USD, target_usd=TARGET_PROFIT_USD
    )

    # --- Gate 2 ---
    # Buy-fast / sell-fast: require cost-adjusted edge that clears ~$1 on scalp size.
    direction = _direction(band)
    edge_ok = edge >= need_edge and profit_usd >= TARGET_PROFIT_USD
    if not edge_ok or direction is Direction.SIDEWAYS:
        gates_failed.append("gate2")
        reason = (
            f"edge {edge:+.2f}% (${profit_usd:+.2f} on ${SCALP_SIZE_USD:.0f}) "
            f"below target ${TARGET_PROFIT_USD:.2f} "
            f"(need ≥{need_edge:.2f}%, cost≈{cost:.2f}%)"
            if not edge_ok
            else "forecast is sideways"
        )
        pipeline_log.emit(
            "gate2",
            "fail",
            address=market_packet.address,
            mint=market_packet.mint,
            symbol=market_packet.name,
            edge=round(edge, 4),
            cost=cost,
            need_edge=need_edge,
            direction=direction.value,
            reason=reason,
        )
        card = _card_from_forecast(
            market_packet,
            ens=ens,
            mode=mode,
            action=Action.HOLD,
            direction=direction,
            edge=edge,
            confidence=_confidence(ens, safety, edge, passed_edge=False),
            warnings=warnings + [reason],
            gates_passed=gates_passed,
            gates_failed=gates_failed,
            started=started,
            safety=safety,
        )
        return _finalize(card, market_packet, mode=mode, started=started)

    gates_passed.append("gate2")
    pipeline_log.emit(
        "gate2",
        "pass",
        address=market_packet.address,
        mint=market_packet.mint,
        symbol=market_packet.name,
        edge=round(edge, 4),
        cost=cost,
        need_edge=need_edge,
        direction=direction.value,
        profit_usd=round(profit_usd, 4),
    )

    # --- Indicator alignment (after edge clears; does not replace Gate 2) ---
    ind_sig = indicators_signal.evaluate_for_direction(tf.indicators, direction)
    pipeline_log.emit(
        "indicators",
        "score",
        address=market_packet.address,
        mint=market_packet.mint,
        symbol=market_packet.name,
        score=ind_sig.score,
        available=ind_sig.available,
        reason=ind_sig.reason,
        votes=ind_sig.votes,
    )
    if ind_sig.available and (ind_sig.hard_block or not ind_sig.soft_ok):
        gates_failed.append("indicators")
        pipeline_log.emit(
            "indicators",
            "fail",
            level="warning",
            address=market_packet.address,
            mint=market_packet.mint,
            symbol=market_packet.name,
            score=ind_sig.score,
            reason=ind_sig.reason,
        )
        card = _card_from_forecast(
            market_packet,
            ens=ens,
            mode=mode,
            action=Action.HOLD,
            direction=direction,
            edge=edge,
            confidence=_confidence(
                ens,
                safety,
                edge,
                passed_edge=True,
                indicator_mult=indicators_signal.confidence_multiplier(ind_sig),
            ),
            warnings=warnings + [ind_sig.reason],
            gates_passed=gates_passed,
            gates_failed=gates_failed,
            started=started,
            safety=safety,
            indicator_signal=ind_sig,
        )
        return _finalize(card, market_packet, mode=mode, started=started)

    if ind_sig.available:
        gates_passed.append("indicators")
        warnings.append(ind_sig.reason)
    else:
        warnings.append(f"indicator gate skipped: {ind_sig.reason}")

    action = Action.BUY if direction is Direction.UP else Action.AVOID
    # Downside with edge → avoid (we are long-biased for memecoins).
    if direction is Direction.DOWN:
        action = Action.AVOID
        warnings.append("calibrated median is down; long-only book → avoid")

    card = _card_from_forecast(
        market_packet,
        ens=ens,
        mode=mode,
        action=action,
        direction=direction,
        edge=edge,
        confidence=_confidence(
            ens,
            safety,
            edge,
            passed_edge=True,
            indicator_mult=indicators_signal.confidence_multiplier(ind_sig),
        ),
        warnings=warnings
        + [
            f"scalp edge {edge:+.2f}% → ${profit_usd:+.2f} on ${SCALP_SIZE_USD:.0f} "
            f"(target ${TARGET_PROFIT_USD:.2f})"
        ],
        gates_passed=gates_passed,
        gates_failed=gates_failed,
        started=started,
        safety=safety,
        indicator_signal=ind_sig,
    )
    return _finalize(card, market_packet, mode=mode, started=started)


def _direction(band: ReturnBand) -> Direction:
    if band.p50 >= 0.5:
        return Direction.UP
    if band.p50 <= -0.5:
        return Direction.DOWN
    return Direction.SIDEWAYS


def _confidence(
    ens: ForecastEnsemble,
    safety: Optional[SafetyReport],
    edge: float,
    *,
    passed_edge: bool,
    indicator_mult: float = 1.0,
) -> float:
    base = 0.35 + 0.35 * ens.agreement
    if ens.residual_count >= 30:
        base += 0.1
    if passed_edge:
        base += min(0.15, max(0.0, edge) / 20.0)
    if safety and safety.verdict is SafetyVerdict.CAUTION:
        base *= 0.75
    if safety and safety.degraded:
        base *= 0.9
    base *= max(0.55, min(1.2, float(indicator_mult or 1.0)))
    return round(min(0.95, max(0.05, base)), 3)


def _price_targets(packet: MarketPacket, band: ReturnBand) -> PriceTargets:
    px = packet.price
    if px is None:
        return PriceTargets()
    return PriceTargets(
        entry=round(px, 8),
        upside=round(px * (1.0 + band.p90 / 100.0), 8),
        downside=round(px * (1.0 + band.p10 / 100.0), 8),
        invalidation=round(px * (1.0 + band.p10 / 100.0), 8),
    )


def _position(packet: MarketPacket, safety: Optional[SafetyReport], confidence: float) -> PositionPlan:
    from decision.packet import SCALP_SIZE_USD

    liq = packet.market.liquidity_usd_total or packet.market.liquidity_usd or 0.0
    # Cap at 1% of exit depth and scalp size; confidence scales down.
    raw = min(SCALP_SIZE_USD, liq * 0.01) * max(0.25, confidence)
    if safety and safety.verdict is SafetyVerdict.CAUTION:
        raw *= 0.5
    return PositionPlan(
        max_size_usd=round(min(raw, SCALP_SIZE_USD), 2),
        size_basis="scalp_size_x_confidence_liq_capped",
    )


def _safety_dict(safety: Optional[SafetyReport]) -> dict[str, Any]:
    if safety is None:
        return {"verdict": "skipped", "score": None, "flags": []}
    return {
        "verdict": safety.verdict.value,
        "score": safety.risk_score,
        "flags": [f.model_dump(mode="json") for f in safety.flags[:8]],
    }


def _empty_ensemble(horizon: int, timeframe: str) -> ForecastEnsemble:
    zero = ReturnBand(p10=0.0, p50=0.0, p90=0.0)
    return ForecastEnsemble(
        horizon_bars=horizon,
        timeframe=timeframe,
        coverage_target=COVERAGE_TARGET,
        raw=zero,
        calibrated=zero,
        agreement=0.0,
        models=[],
        residual_count=0,
        backend="none",
    )


def _card_from_forecast(
    packet: MarketPacket,
    *,
    ens: ForecastEnsemble,
    mode: DecideMode,
    action: Action,
    direction: Direction,
    edge: float,
    confidence: float,
    warnings: list[str],
    gates_passed: list[str],
    gates_failed: list[str],
    started: float,
    safety: Optional[SafetyReport],
    indicator_signal: Optional[indicators_signal.IndicatorSignal] = None,
) -> DecisionCard:
    band = ens.calibrated
    drivers = [
        Driver(source=m.name, claim=f"p50={m.p50:+.2f}%", weight=1.0 / max(1, len(ens.models)))
        for m in ens.models
        if m.available
    ]
    drivers.append(
        Driver(
            source="conformal",
            claim=f"coverage_target={ens.coverage_target:.0%} n={ens.residual_count}",
            weight=0.2,
        )
    )
    if indicator_signal and indicator_signal.available:
        drivers.append(
            Driver(
                source="indicators",
                claim=indicator_signal.reason,
                weight=0.25,
            )
        )
    return DecisionCard(
        token={
            "address": packet.address,
            "mint": packet.mint,
            "name": packet.name,
            "source": packet.source,
        },
        horizon_bars=ens.horizon_bars,
        timeframe=ens.timeframe,
        action=action,
        action_confidence=confidence,
        confidence_basis=(
            "conformal_coverage + model_agreement + safety + edge + indicators"
        ),
        direction=direction,
        expected_return_pct=band,
        interval_coverage_target=ens.coverage_target,
        cost_adjusted_edge_pct=round(edge, 4),
        price_targets=_price_targets(packet, band),
        position=_position(packet, safety, confidence),
        safety=_safety_dict(safety),
        risk=RiskBlock(
            risk_pass=action != Action.AVOID,
            max_loss_pct=abs(min(0.0, band.p10)),
            veto_reasons=[],
        ),
        forecast=ens,
        drivers=drivers,
        warnings=warnings,
        gates_passed=gates_passed,
        gates_failed=gates_failed,
        mode=mode,
        degraded=bool(safety.degraded) if safety else packet.source == "corpus",
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


def _avoid_card(packet, **kwargs) -> DecisionCard:
    return _hold_card(packet, action=Action.AVOID, **kwargs)


def _hold_card(
    packet: MarketPacket,
    *,
    mode: DecideMode,
    horizon: int,
    timeframe: str,
    reason: str,
    warnings: list[str],
    gates_passed: list[str],
    gates_failed: list[str],
    started: float,
    safety: Optional[SafetyReport],
    action: Action = Action.HOLD,
) -> DecisionCard:
    ens = _empty_ensemble(horizon, timeframe)
    card = DecisionCard(
        token={
            "address": packet.address,
            "mint": packet.mint,
            "name": packet.name,
            "source": packet.source,
        },
        horizon_bars=horizon,
        timeframe=timeframe,
        action=action,
        action_confidence=0.9 if action is Action.AVOID else 0.5,
        confidence_basis="gate_failure",
        direction=Direction.SIDEWAYS,
        expected_return_pct=ens.calibrated,
        interval_coverage_target=COVERAGE_TARGET,
        cost_adjusted_edge_pct=0.0,
        price_targets=PriceTargets(entry=packet.price),
        position=PositionPlan(max_size_usd=0.0, size_basis="blocked"),
        safety=_safety_dict(safety),
        risk=RiskBlock(risk_pass=False, max_loss_pct=0.0, veto_reasons=[reason]),
        forecast=ens,
        drivers=[Driver(source="gate", claim=reason, weight=1.0)],
        warnings=warnings + [reason],
        gates_passed=gates_passed,
        gates_failed=gates_failed,
        mode=mode,
        degraded=True,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    return _finalize(card, packet, mode=mode, started=started)


def _finalize(
    card: DecisionCard,
    packet: MarketPacket,
    *,
    mode: DecideMode,
    started: float,
) -> DecisionCard:
    """Gate 3 desk (full/deep) + latency + JSONL log."""
    card.mode = mode
    if mode in (DecideMode.FULL, DecideMode.DEEP):
        review = vibe_mod.review_card(card, packet, mode=mode)
        card = vibe_mod.apply_review(card, review)
        card.warnings.append(f"vibe backend={review.backend}")
        pipeline_log.emit(
            "gate3",
            "pass" if review.risk_pass else "fail",
            level="info" if review.risk_pass else "warning",
            address=packet.address,
            mint=packet.mint,
            symbol=packet.name,
            risk_pass=review.risk_pass,
            action_override=getattr(review.action_override, "value", None)
            if review.action_override
            else None,
            veto=list(review.veto_reasons or [])[:5] or None,
            backend=review.backend,
            specialists=len(review.opinions or []),
        )
    card.latency_ms = int((time.perf_counter() - started) * 1000)
    # Gate 4 portfolio limits checked at paper execute time.
    if "gate4" not in card.gates_passed and "gate4" not in card.gates_failed:
        card.gates_passed = list(card.gates_passed) + ["gate4_deferred_to_paper"]
    _log(card)
    return card


def _log(card: DecisionCard) -> None:
    try:
        decision_store.append("decisions", card)
    except OSError as exc:
        pipeline_log.emit(
            "decide",
            "log_error",
            level="error",
            reason=str(exc),
            address=(card.token or {}).get("address"),
        )
