"""JEV System One chair for Gate 3 — typed decisions, not prose.

TypeSafe JEV is a decision model: it takes a state (text or JSON) plus
named typed questions and returns JSON only — no text to parse. Three
primitives (see https://docs.typesafe.ai/primitives):

  * ``choice`` — pick one option + probability on every option + confidence
  * ``noul``   — yes/no as a single 0..1 value (exactly 0.5 = unsure)
  * ``score``  — continuous position on an ordered low→high scale you supply

One call carries six questions over a shared compact state (batching is
~free per the parallel-questions cookbook): an ``action`` choice, four
atomic veto Nouls (one condition each — never pack several judgments
into one Noul), and an entry-``quality`` score. Code combines the
answers: veto is the max of the four Nouls; a middle band only blocks
new entries instead of forcing avoid.

Native endpoint is ``POST https://api.typesafe.ai/v1/systemone`` with
``{"model": "jev-latest", "state": ..., "questions": {...}}``. The Jevx
proxy (``POST https://jevx.org/api/v1/decisions``) uses the same
``state`` + ``questions`` shape without the ``model`` field.

This module is the cheap/fast tier for the risk chair. It never replaces
the time-series forecast (stat/chronos/timesfm), Gate 0 on-chain safety,
or paper execution — it only answers "given this evidence, what should
the book do?" in ~0.3s instead of a 90s chat-LLM JSON scrape.

Missing key degrades cleanly to the heuristic desk — callers must treat
:class:`JevUnavailable` as skip, never as retry-hard.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

from decision.config import _env_float

ENDPOINT = os.getenv("JEV_ENDPOINT", "https://api.typesafe.ai/v1/systemone").strip() or (
    "https://api.typesafe.ai/v1/systemone"
)
MODEL = os.getenv("JEV_MODEL", "jev-latest").strip() or "jev-latest"
ENABLED = os.getenv("JEV_ENABLED", "0").strip() == "1"
TIMEOUT_S = _env_float("JEV_TIMEOUT_S", 8.0)
MIN_CONFIDENCE = _env_float("JEV_MIN_CONFIDENCE", 0.55)
VETO_THRESHOLD = _env_float("JEV_VETO_THRESHOLD", 0.70)
# Middle band: a veto Noul in [VETO_CAUTION, VETO_THRESHOLD) only blocks
# new entries (buy→hold) instead of forcing avoid. Per the docs' three
# paths: act / proceed-with-caution / don't act.
VETO_CAUTION = _env_float("JEV_VETO_CAUTION", 0.40)
# Exit monitor on open lots: force-exit when the chair says get out.
# Missing a rug costs more than an early exit, so this sits at 0.70.
EXIT_THRESHOLD = _env_float("JEV_EXIT_THRESHOLD", 0.70)
MAX_STATE_CHARS = int(_env_float("JEV_MAX_STATE_CHARS", 4000))

# Atomic veto conditions — one yes/no judgment each. Combined in code as
# their max; see build_questions() and chair_review().
VETO_QUESTIONS = ("no_exit", "safety_block", "chase", "bad_band")


class JevUnavailable(RuntimeError):
    """JEV cannot be used right now. Callers should degrade, not retry hard."""


@dataclass(frozen=True)
class JevStatus:
    enabled: bool
    configured: bool
    endpoint: str
    model: str
    detail: str

    @property
    def ready(self) -> bool:
        return self.enabled and self.configured


@dataclass(frozen=True)
class JevVerdict:
    action: str  # buy | hold | avoid
    action_probs: dict[str, float] = field(default_factory=dict)
    action_confidence: float = 0.5
    veto_prob: float = 0.5
    quality01: float = 0.5
    quality_confidence: float = 0.5
    latency_ms: Optional[int] = None
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def _api_key() -> str:
    for name in (
        "TYPESAFE_API_KEY",
        "JEV_API_KEY",
        "JEVX_API_KEY",
        "OPENCODE_GO_API_KEY",
        "OPENCODE_API_KEY",
    ):
        key = os.getenv(name, "").strip()
        if key:
            return key
    return ""


def _is_jevx(endpoint: str) -> bool:
    return "jevx.org" in (endpoint or "").lower()


def _is_native_typesafe(endpoint: str) -> bool:
    return "api.typesafe.ai" in (endpoint or "").lower()


def _wrap_questions(questions: dict[str, Any]) -> dict[str, Any]:
    """Map our state+questions shape onto official typesafe-sdk questions."""
    from typesafe_sdk import Choice, Noul, Score

    wrapped: dict[str, Any] = {}
    for name, spec in (questions or {}).items():
        if not isinstance(spec, dict):
            continue
        t = str(spec.get("type") or "").lower()
        instructions = str(spec.get("instructions") or "")
        if t == "choice" and isinstance(spec.get("criteria"), dict):
            wrapped[name] = Choice(
                instructions=instructions, criteria=dict(spec["criteria"])
            )
        elif t == "noul":
            kw: dict[str, Any] = {"instructions": instructions}
            if isinstance(spec.get("criteria"), dict):
                kw["criteria"] = dict(spec["criteria"])
            wrapped[name] = Noul(**kw)
        elif t == "score" and isinstance(spec.get("criteria"), list):
            wrapped[name] = Score(
                instructions=instructions, criteria=list(spec["criteria"])
            )
    return wrapped


def _post_via_sdk(
    state: dict[str, Any],
    questions: dict[str, Any],
    *,
    key: str,
    model: str,
    timeout_s: float,
) -> tuple[dict[str, Any], int]:
    """Official typesafe-sdk transport for the native endpoint.

    Same wire protocol as the raw httpx path; the response is converted
    back into the ``{"model", "answers"}`` shape :func:`_parse` expects.
    Raises :class:`JevUnavailable` (degrades to heuristic) on any failure.
    """
    import time

    from typesafe_sdk import TypeSafeClient

    wrapped = _wrap_questions(questions)
    if not wrapped:
        raise JevUnavailable("jev: no wrappable questions for classifier")
    clf = TypeSafeClient(api_key=key, model=model, timeout=float(timeout_s))
    started = time.perf_counter()
    try:
        resp = clf.system_one(state=state, questions=wrapped)
    except Exception as exc:  # noqa: BLE001 — degraded to heuristic by callers
        raise JevUnavailable(
            f"jev request failed: {type(exc).__name__}: {exc}"
        ) from exc
    latency_ms = int((time.perf_counter() - started) * 1000)
    answers: dict[str, Any] = {}
    for name, ans in dict(getattr(resp, "answers", {}) or {}).items():
        kind = getattr(ans, "type", None)
        if kind == "choice":
            answers[name] = {
                "choice": getattr(ans, "choice", None),
                "probabilities": dict(getattr(ans, "probabilities", None) or {}),
                "confidence": getattr(ans, "confidence", 0.5),
            }
        elif kind == "noul":
            answers[name] = {"noul": getattr(ans, "noul", 0.5)}
        elif kind == "score":
            answers[name] = {
                "score": getattr(ans, "score", 0.5),
                "confidence": getattr(ans, "confidence", 0.5),
            }
    return {"model": getattr(resp, "model", None) or model, "answers": answers}, latency_ms


def preflight() -> JevStatus:
    """Cheap, side-effect-free readiness check. Never touches the network."""
    if not ENABLED:
        return JevStatus(
            enabled=False,
            configured=bool(_api_key()),
            endpoint=ENDPOINT,
            model=MODEL,
            detail="JEV_ENABLED != 1. Set JEV_ENABLED=1 + key (OPENCODE_GO_API_KEY for Zen free jev-1.13-free, or TYPESAFE_API_KEY) to use the JEV chair.",
        )
    key = _api_key()
    if not key:
        return JevStatus(
            enabled=True,
            configured=False,
            endpoint=ENDPOINT,
            model=MODEL,
            detail="JEV enabled but no key. Set OPENCODE_GO_API_KEY (Zen) or TYPESAFE_API_KEY (or JEVX_API_KEY for the jevx proxy) in .env.",
        )
    return JevStatus(
        enabled=True, configured=True, endpoint=ENDPOINT, model=MODEL, detail="ready"
    )


def _indicator_context(card: Any, packet: Any) -> Optional[dict[str, Any]]:
    """Tape agreement, computed in code — never raw numbers for the model.

    Reads the card timeframe's indicator snapshot off the packet, scores it
    with the same ``score_indicators`` Gate 2 uses, and reduces it to named
    buckets (docs: semantic beats numeric; filter to what the question
    needs). Returns ``None`` when no snapshot exists (cluster path, short
    series) so the questions judge without it.
    """

    def _f(v: Any) -> Optional[float]:
        try:
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None

    try:
        from decision import indicators_signal as ind_mod
    except Exception:  # noqa: BLE001
        return None
    try:
        tfs = getattr(packet, "timeframes", None) or {}
        tf_name = getattr(card, "timeframe", None)
        tfp = tfs.get(tf_name) if isinstance(tfs, dict) else None
        snap = getattr(tfp, "indicators", None)
    except Exception:  # noqa: BLE001
        snap = None
    if not isinstance(snap, dict) or not snap or snap.get("error"):
        return None
    try:
        sig = ind_mod.score_indicators(snap)
    except Exception:  # noqa: BLE001
        return None
    if not sig.available:
        return None
    ctx: dict[str, Any] = {"score": round(sig.score, 2)}
    rsi = _f(snap.get("rsi"))
    if rsi is not None:
        ctx["rsi"] = "overbought" if rsi >= 70 else ("oversold" if rsi <= 30 else "neutral")
    macd_hist = _f(snap.get("macd_hist"))
    if macd_hist is not None:
        ctx["macd"] = "positive" if macd_hist > 0 else ("negative" if macd_hist < 0 else "flat")
    plus_di, minus_di, adx = _f(snap.get("plus_di")), _f(snap.get("minus_di")), _f(snap.get("adx"))
    if plus_di is not None and minus_di is not None:
        spread = plus_di - minus_di
        trend = "up" if spread > 0 else ("down" if spread < 0 else "flat")
        if adx is not None and adx >= 25 and trend != "flat":
            trend += "-strong"
        ctx["trend"] = trend
    ctx["note"] = str(sig.reason or "")[:120]
    return ctx


def build_state(card: Any, packet: Any, opinions: Optional[list[Any]] = None) -> dict[str, Any]:
    """Compact evidence bundle. Small on purpose — state is the token bill."""
    band = getattr(card, "expected_return_pct", None)
    forecast = getattr(card, "forecast", None)
    safety = getattr(card, "safety", None) or {}
    market = getattr(packet, "market", None)
    orderflow = getattr(packet, "orderflow", None) or {}

    liq = None
    price_impact = None
    try:
        liq = getattr(market, "liquidity_usd_total", None) or getattr(market, "liquidity_usd", None)
    except Exception:
        liq = None

    safety_summary: Any = None
    safety_verdict: Any = None
    safety_score: Any = None
    try:
        if hasattr(packet, "safety") and getattr(packet, "safety", None) is not None:
            rep = packet.safety
            safety_verdict = getattr(getattr(rep, "verdict", None), "value", None) or str(
                getattr(rep, "verdict", "")
            )
            safety_score = getattr(rep, "risk_score", None)
            try:
                safety_summary = rep.summary()
            except Exception:
                safety_summary = None
            sell = getattr(rep, "sellability", None)
            if sell is not None:
                price_impact = getattr(sell, "price_impact_pct", None)
    except Exception:
        pass
    if safety_verdict is None and isinstance(safety, dict):
        safety_verdict = safety.get("verdict")
        safety_score = safety.get("score")
        safety_summary = None

    specs: list[dict[str, Any]] = []
    for o in opinions or []:
        try:
            specs.append(
                {
                    "desk": getattr(o, "name", "?"),
                    "vote": getattr(o, "vote", "?"),
                    "conf": round(float(getattr(o, "confidence", 0.5)), 2),
                    "claim": str(getattr(o, "claim", ""))[:120],
                }
            )
        except Exception:
            continue

    state: dict[str, Any] = {
        "card": str(getattr(card, "summary", lambda: "")()),
        "action": getattr(getattr(card, "action", None), "value", None)
        or str(getattr(card, "action", "")),
        "action_confidence": getattr(card, "action_confidence", None),
        "direction": getattr(getattr(card, "direction", None), "value", None)
        or str(getattr(card, "direction", "")),
        "edge_pct": getattr(card, "cost_adjusted_edge_pct", None),
        "p10": getattr(band, "p10", None),
        "p50": getattr(band, "p50", None),
        "p90": getattr(band, "p90", None),
        "agreement": getattr(forecast, "agreement", None),
        "forecast_backend": getattr(forecast, "backend", None),
        "timeframe": getattr(card, "timeframe", None),
        "token": getattr(card, "token", None),
        "safety_verdict": safety_verdict,
        "safety_score": safety_score,
        "safety_summary": str(safety_summary or "")[:220],
        "liquidity_usd": liq,
        "price_impact_pct": price_impact,
        "orderflow": {
            "buys": orderflow.get("buys_5m", orderflow.get("buys_h1")),
            "sells": orderflow.get("sells_5m", orderflow.get("sells_h1")),
            "ratio": orderflow.get("buy_sell_ratio"),
            "source": orderflow.get("source"),
        },
        "specialists": specs[:6],
        "momentum_entry": bool((getattr(card, "notes", None) or {}).get("momentum_entry")),
        "indicators": _indicator_context(card, packet),
    }
    return state


def build_questions() -> dict[str, Any]:
    """One call carries every type — they share one state, one charge.

    Follows https://docs.typesafe.ai/primitives: one snap judgment per
    question, literal single-condition Nouls (a high value always means
    yes), instructions naming the state fields they judge, and the full
    option list on the Choice. Option order is fixed (buy/hold/avoid) so
    answers stay comparable run to run.
    """
    questions: dict[str, Any] = {
        "action": {
            "type": "choice",
            "instructions": (
                "As risk chair for a Solana memecoin scalp desk (long-only, "
                "fake-money practice book), what should the book do with "
                "`token` given `edge_pct`, `liquidity_usd`, "
                "`safety_verdict`, and `specialists`? Prefer avoid when unsure."
            ),
            "criteria": {
                "buy": (
                    "Clear long edge after costs with a safe exit route, "
                    "healthy liquidity, no holder-concentration flag, "
                    "`indicators` showing the tape agreeing (bullish score, "
                    "no overbought exhaustion), and specialist buy consensus. "
                    "Only when evidence supports an immediate entry."
                ),
                "hold": (
                    "Unclear, sideways forecast, or weak/mixed specialist "
                    "consensus. Watch, do not enter."
                ),
                "avoid": (
                    "Unsafe or negative edge: no exit route, blocking safety "
                    "flag, blowoff/extended chase, downside median, or a "
                    "riskless-looking interval. Stay out."
                ),
            },
        },
        "no_exit": {
            "type": "noul",
            "instructions": (
                "Is there no safe exit route for `token` — honeypot, sells "
                "fail, or `price_impact_pct` so high the position cannot be closed?"
            ),
            "criteria": {
                "true": "Cannot reliably sell: honeypot, failed sells, or exit wipes the position.",
                "false": "A normal market sell can close the position.",
            },
        },
        "safety_block": {
            "type": "noul",
            "instructions": (
                "Does `safety_verdict` or `safety_summary` contain a blocking "
                "safety flag — honeypot, freeze/mint authority abuse, or "
                "extreme holder concentration?"
            ),
            "criteria": {
                "true": "A blocking on-chain safety flag is present.",
                "false": "No blocking safety flag.",
            },
        },
        "chase": {
            "type": "noul",
            "instructions": (
                "Is entering now chasing — a vertical blowoff, over-extended "
                "move, or singleton spike with no base, per `safety_summary` "
                "and `orderflow`?"
            ),
            "criteria": {
                "true": "Price already ran; entry would chase the top.",
                "false": "No blowoff; entry is not chasing.",
            },
        },
        "bad_band": {
            "type": "noul",
            "instructions": (
                "Is the forecast band implausible or downside — `p50` at or "
                "below zero, or a riskless-looking interval promising profit "
                "with no downside?"
            ),
            "criteria": {
                "true": "Downside median or implausible risk-free band.",
                "false": "Band is plausible with real upside and downside.",
            },
        },
        "quality": {
            "type": "score",
            "instructions": (
                "Entry quality from stay-out to high-quality, judging "
                "`edge_pct`, `liquidity_usd`, `agreement`, `indicators`, "
                "and `specialists`."
            ),
            "criteria": [
                "Stay out: unsafe or no edge.",
                "Marginal: weak edge or mixed signals, watch.",
                "High quality: clear edge, safe exit, consensus.",
            ],
        },
    }
    return questions


def _post(
    state: dict[str, Any], questions: dict[str, Any], *, timeout_s: float
) -> tuple[dict[str, Any], int]:
    import time

    import httpx

    status = preflight()
    if not status.ready:
        raise JevUnavailable(status.detail)

    key = _api_key()
    body: dict[str, Any] = {"state": state, "questions": questions}
    if not _is_jevx(status.endpoint):
        body["model"] = status.model

    if _is_native_typesafe(status.endpoint):
        try:
            return _post_via_sdk(
                state,
                questions,
                key=key,
                model=status.model,
                timeout_s=float(timeout_s),
            )
        except ImportError:
            pass  # SDK not installed — fall through to raw httpx

    started = time.perf_counter()
    try:
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        session_id = os.getenv("OPENCODE_SESSION_ID", "").strip()
        if session_id or "opencode.ai" in (status.endpoint or ""):
            headers["x-opencode-session"] = session_id or "paper-desk"
        with httpx.Client(timeout=httpx.Timeout(timeout_s)) as client:
            resp = client.post(
                status.endpoint,
                headers=headers,
                json=body,
            )
    except Exception as exc:  # noqa: BLE001 — network failure degrades to heuristic
        raise JevUnavailable(f"jev request failed: {type(exc).__name__}: {exc}") from exc
    latency_ms = int((time.perf_counter() - started) * 1000)

    if resp.status_code == 401:
        raise JevUnavailable("jev: authentication_error — missing or bad key")
    if resp.status_code == 402:
        raise JevUnavailable("jev: insufficient_credits — top up, degrading to heuristic")
    if resp.status_code == 403:
        raise JevUnavailable(
            "jev: forbidden — free-tier key used outside OpenCode or model not enabled, degrading to heuristic"
        )
    if resp.status_code == 400:
        detail = (resp.text or "")[:300]
        raise JevUnavailable(f"jev: invalid_request — {detail}")
    if resp.status_code >= 500:
        raise JevUnavailable(f"jev: upstream_error (refunded) — HTTP {resp.status_code}")
    if resp.status_code != 200:
        raise JevUnavailable(f"jev: HTTP {resp.status_code} — {(resp.text or '')[:200]}")

    try:
        payload = resp.json()
    except Exception as exc:
        raise JevUnavailable(f"jev: invalid JSON response: {exc}") from exc
    if not isinstance(payload, dict):
        raise JevUnavailable("jev: unexpected response shape")
    return payload, latency_ms


def _veto_max(answers: dict[str, Any]) -> tuple[float, str]:
    """Combine the atomic veto Nouls in code: their max wins.

    Missing answers count as 0.5 (unsure), never as a veto. Returns the
    winning probability and which question produced it.
    """
    best_p = 0.0
    best_q = ""
    for name in VETO_QUESTIONS:
        ans = answers.get(name)
        if not isinstance(ans, dict):
            continue
        try:
            p = float(ans.get("noul", 0.5))
        except (TypeError, ValueError):
            continue
        p = max(0.0, min(1.0, p))
        if p >= best_p:
            best_p, best_q = p, name
    if not best_q:
        return 0.5, ""
    return round(best_p, 3), best_q


def _parse(payload: dict[str, Any], *, latency_ms: int, model: str) -> JevVerdict:
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        answers = payload

    action_ans = answers.get("action") if isinstance(answers.get("action"), dict) else {}
    quality_ans = (
        answers.get("quality") if isinstance(answers.get("quality"), dict) else {}
    )

    action = str(action_ans.get("choice") or "hold").lower()
    if action not in ("buy", "hold", "avoid"):
        action = "hold"
    try:
        probs = {str(k).lower(): float(v) for k, v in (action_ans.get("probabilities") or {}).items()}
    except Exception:
        probs = {}
    try:
        action_conf = float(action_ans.get("confidence", 0.5))
    except (TypeError, ValueError):
        action_conf = 0.5

    veto_prob, _veto_source = _veto_max(answers)

    # Score scale has 3 steps; normalize to 0..1 regardless of 0..1 or 0..2 return.
    quality01 = 0.5
    try:
        raw_score = quality_ans.get("score", quality_ans.get("value", 0.5))
        raw_score = float(raw_score)
        quality01 = raw_score / 2.0 if raw_score > 1.0 else raw_score
        quality01 = max(0.0, min(1.0, quality01))
    except (TypeError, ValueError):
        quality01 = 0.5
    try:
        quality_conf = float(quality_ans.get("confidence", action_conf))
    except (TypeError, ValueError):
        quality_conf = action_conf

    reported_model = str(payload.get("model") or model or "")
    return JevVerdict(
        action=action,
        action_probs=probs,
        action_confidence=round(max(0.0, min(1.0, action_conf)), 3),
        veto_prob=round(max(0.0, min(1.0, veto_prob)), 3),
        quality01=round(quality01, 3),
        quality_confidence=round(max(0.0, min(1.0, quality_conf)), 3),
        latency_ms=latency_ms,
        model=reported_model,
        raw=payload,
    )


def evaluate(
    card: Any,
    packet: Any,
    opinions: Optional[list[Any]] = None,
    *,
    timeout_s: Optional[float] = None,
) -> JevVerdict:
    """Run the JEV chair once. Raises :class:`JevUnavailable` to degrade."""
    import time as _time

    from decision import pipeline_log

    started = _time.perf_counter()
    state = build_state(card, packet, opinions)
    # Bound the token bill: state is what we pay for.
    try:
        import json as _json

        text = _json.dumps(state, default=str)
        if len(text) > MAX_STATE_CHARS:
            state = {"summary": text[:MAX_STATE_CHARS]}
    except Exception:
        pass
    questions = build_questions()
    try:
        payload, latency_ms = _post(
            state, questions, timeout_s=float(timeout_s or TIMEOUT_S)
        )
        verdict = _parse(payload, latency_ms=latency_ms, model=MODEL)
        pipeline_log.emit(
            "jev",
            "pass",
            address=getattr(card, "token", {}).get("address")
            if isinstance(getattr(card, "token", None), dict)
            else None,
            symbol=getattr(card, "token", {}).get("name")
            if isinstance(getattr(card, "token", None), dict)
            else None,
            action=verdict.action,
            action_confidence=verdict.action_confidence,
            veto_prob=verdict.veto_prob,
            quality=verdict.quality01,
            latency_ms=verdict.latency_ms,
            model=verdict.model,
            duration_ms=pipeline_log.timed_ms(started),
        )
        return verdict
    except JevUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 — logging must not break trading
        try:
            pipeline_log.emit(
                "jev",
                "error",
                level="warning",
                reason=f"{type(exc).__name__}: {exc}",
                duration_ms=pipeline_log.timed_ms(started),
            )
        except Exception:
            pass
        raise JevUnavailable(f"jev error: {exc}") from exc


def chair_review(
    card: Any, packet: Any, opinions: Optional[list[Any]] = None
) -> Optional[dict[str, Any]]:
    """JEV chair in the ``_deep_agy_review`` dict shape (veto/action/claim).

    Returns ``None`` when JEV is disabled/unconfigured so callers fall back
    to the heuristic desk. Thresholds gate the proposal so a low-confidence
    model cannot force trades on its own — the docs' three paths
    (act / caution / don't act):

    * veto (force avoid) only when max veto Noul ``>= JEV_VETO_THRESHOLD``
      (0.5 unsure never vetoes)
    * caution: a veto Noul in ``[JEV_VETO_CAUTION, JEV_VETO_THRESHOLD)``
      blocks new entries (buy→hold) without forcing avoid
    * buy only when ``action_confidence >= JEV_MIN_CONFIDENCE``
    """
    from decision import pipeline_log

    status = preflight()
    if not status.ready:
        return None
    try:
        verdict = evaluate(card, packet, opinions)
    except JevUnavailable as exc:
        try:
            pipeline_log.emit("jev", "skip", level="info", reason=str(exc)[:200])
        except Exception:
            pass
        return None

    raw_answers = verdict.raw.get("answers", {}) if isinstance(verdict.raw, dict) else {}
    _, veto_source = _veto_max(raw_answers if isinstance(raw_answers, dict) else {})

    abstained = verdict.veto_prob == 0.5
    veto = (not abstained) and verdict.veto_prob >= VETO_THRESHOLD
    caution = (
        not veto
        and verdict.action == "buy"
        and VETO_CAUTION <= verdict.veto_prob < VETO_THRESHOLD
    )

    if veto:
        proposed = "avoid"
        reason = (
            f"jev veto[{veto_source or 'veto'}] "
            f"p={verdict.veto_prob:.2f} (threshold {VETO_THRESHOLD:.2f})"
        )
    elif caution:
        proposed = "hold"
        reason = (
            f"jev caution[{veto_source or 'veto'}] "
            f"p={verdict.veto_prob:.2f} — entry blocked, watch"
        )
    elif verdict.action == "buy" and verdict.action_confidence < MIN_CONFIDENCE:
        proposed = "hold"
        reason = (
            f"jev buy under confidence floor "
            f"({verdict.action_confidence:.2f} < {MIN_CONFIDENCE:.2f}) → hold"
        )
    else:
        proposed = verdict.action
        reason = (
            f"jev {verdict.action} conf={verdict.action_confidence:.2f} "
            f"veto_p={verdict.veto_prob:.2f} q={verdict.quality01:.2f}"
        )

    claim = (
        f"jev {verdict.action} ({verdict.action_confidence:.0%}) "
        f"veto_p={verdict.veto_prob:.2f} q={verdict.quality01:.2f}"
    )
    return {
        "veto": bool(veto),
        "action": proposed,
        "claim": claim,
        "reason": reason,
        "backend": "jev",
        "confidence": verdict.action_confidence,
        "veto_prob": verdict.veto_prob,
        "quality": verdict.quality01,
        "probs": dict(verdict.action_probs),
        "latency_ms": verdict.latency_ms,
        "model": verdict.model,
    }


def build_exit_questions() -> dict[str, Any]:
    """One Noul: should the book exit this open lot now?

    The state carries everything computed in code (pnl, peak giveback,
    hold time, distance to stop/target). The model contributes the one
    judgment code cannot make: does this tape look dead or rugging.
    """
    return {
        "exit_now": {
            "type": "noul",
            "instructions": (
                "Should the book exit `symbol` now at market — momentum "
                "dead with no recovery (negative `pnl_usd`, never green), "
                "rug signs (deep red `pnl_pct`, large giveback from "
                "`peak_usd`), or capital better redeployed? Judge `pnl_usd`, "
                "`peak_usd`, `held_sec`, `dist_to_stop_usd`, and "
                "`dist_to_target_usd`."
            ),
            "criteria": {
                "true": "Exit now: dead tape, rug forming, or the loss will only grow.",
                "false": "Hold: still constructive — green, recovering, or too early to tell.",
            },
        },
    }


def exit_review(state: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Ask the chair whether to force-exit an open lot.

    Returns ``{"exit", "p", "latency_ms", "model"}``; ``None`` when the
    chair is disabled/unavailable so the normal stop/trail logic owns the
    lot. Callers throttle per lot — this makes one API call per review.
    """
    try:
        payload, latency_ms = _post(
            state, build_exit_questions(), timeout_s=float(TIMEOUT_S)
        )
    except JevUnavailable:
        return None
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        answers = {}
    ans = answers.get("exit_now")
    p = 0.5
    if isinstance(ans, dict):
        try:
            p = max(0.0, min(1.0, float(ans.get("noul", 0.5))))
        except (TypeError, ValueError):
            p = 0.5
    return {
        "exit": p >= EXIT_THRESHOLD,
        "p": round(p, 3),
        "latency_ms": latency_ms,
        "model": str(payload.get("model") or MODEL),
    }
