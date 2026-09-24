"""JEV System One chair for Gate 3 — typed decisions, not prose.

TypeSafe JEV (released 2026-09-15) is a decision model: it takes a state
(text or JSON) plus named typed questions and returns JSON only — no text
to parse. Three primitives:

  * ``choice`` — pick one option + probability on every option + confidence
  * ``noul``   — yes/no as a single 0..1 value (exactly 0.5 = abstain)
  * ``score``  — continuous value on an ordered low→high scale you supply

Native endpoint is ``POST https://api.typesafe.ai/v1/systemone`` with
``{"model": "jev-latest", "state": ..., "questions": {...}}``. The Jevx
proxy (``POST https://jevx.org/api/v1/decisions``) uses the same
``state`` + ``questions`` shape without the ``model`` field.

This module is the cheap/fast tier for the risk chair. It never replaces
the time-series forecast (stat/chronos/timesfm), Gate 0 on-chain safety,
or paper execution — it only answers "given this evidence, what should
the book do?" in ~0.5s for ~$0.00002 instead of a 90s chat-LLM JSON scrape.

Disabled by default (``JEV_ENABLED=0``). Missing key degrades cleanly to
the heuristic desk — callers must treat :class:`JevUnavailable` as skip,
never as retry-hard.
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
MAX_STATE_CHARS = int(_env_float("JEV_MAX_STATE_CHARS", 4000))


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
    for name in ("TYPESAFE_API_KEY", "JEV_API_KEY", "JEVX_API_KEY"):
        key = os.getenv(name, "").strip()
        if key:
            return key
    return ""


def _is_jevx(endpoint: str) -> bool:
    return "jevx.org" in (endpoint or "").lower()


def preflight() -> JevStatus:
    """Cheap, side-effect-free readiness check. Never touches the network."""
    if not ENABLED:
        return JevStatus(
            enabled=False,
            configured=bool(_api_key()),
            endpoint=ENDPOINT,
            model=MODEL,
            detail="JEV_ENABLED != 1. Set JEV_ENABLED=1 + TYPESAFE_API_KEY to use the JEV chair.",
        )
    key = _api_key()
    if not key:
        return JevStatus(
            enabled=True,
            configured=False,
            endpoint=ENDPOINT,
            model=MODEL,
            detail="JEV enabled but no key. Set TYPESAFE_API_KEY (or JEVX_API_KEY for the jevx proxy) in .env.",
        )
    return JevStatus(
        enabled=True, configured=True, endpoint=ENDPOINT, model=MODEL, detail="ready"
    )


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
    }
    return state


def build_questions() -> dict[str, Any]:
    """One call carries all three types — they share one state, one charge."""
    return {
        "action": {
            "type": "choice",
            "instructions": (
                "As risk chair for a Solana memecoin scalp desk (long-only, "
                "fake-money practice book), what should the book do? "
                "Prefer avoid when unsure."
            ),
            "criteria": {
                "buy": (
                    "Clear long edge after costs with a safe exit route, "
                    "healthy liquidity, no holder-concentration flag, and "
                    "specialist buy consensus. Only when evidence supports "
                    "an immediate entry."
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
        "veto": {
            "type": "noul",
            "instructions": "Should the risk committee veto any entry (force avoid)?",
            "criteria": {
                "true": (
                    "Veto: honeypot or no exit, blocking safety, blowoff chase, "
                    "implausible riskless band, or strong avoid consensus."
                ),
                "false": "No veto: entry may proceed under normal sizing rules.",
            },
        },
        "quality": {
            "type": "score",
            "instructions": "Entry quality from stay-out to high-quality.",
            "criteria": [
                "Stay out: unsafe or no edge.",
                "Marginal: weak edge or mixed signals, watch.",
                "High quality: clear edge, safe exit, consensus.",
            ],
        },
    }


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

    started = time.perf_counter()
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout_s)) as client:
            resp = client.post(
                status.endpoint,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
    except Exception as exc:  # noqa: BLE001 — network failure degrades to heuristic
        raise JevUnavailable(f"jev request failed: {type(exc).__name__}: {exc}") from exc
    latency_ms = int((time.perf_counter() - started) * 1000)

    if resp.status_code == 401:
        raise JevUnavailable("jev: authentication_error — missing or bad key")
    if resp.status_code == 402:
        raise JevUnavailable("jev: insufficient_credits — top up, degrading to heuristic")
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


def _parse(payload: dict[str, Any], *, latency_ms: int, model: str) -> JevVerdict:
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        answers = payload

    action_ans = answers.get("action") if isinstance(answers.get("action"), dict) else {}
    veto_ans = answers.get("veto") if isinstance(answers.get("veto"), dict) else {}
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

    try:
        veto_prob = float(veto_ans.get("noul", 0.5))
    except (TypeError, ValueError):
        veto_prob = 0.5

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
    model cannot force trades on its own:

    * veto only when ``veto_prob >= JEV_VETO_THRESHOLD`` (0.5 abstains never veto)
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

    abstained = verdict.veto_prob == 0.5
    veto = (not abstained) and verdict.veto_prob >= VETO_THRESHOLD

    if veto:
        proposed = "avoid"
        reason = f"jev veto p={verdict.veto_prob:.2f} (threshold {VETO_THRESHOLD:.2f})"
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
