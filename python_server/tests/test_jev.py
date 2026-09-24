"""JEV System One chair — typed decisions for Gate 3.

No network in tests: HTTP is faked, and vibe integration uses a stubbed
chair_review. Guards under test are the load-bearing ones: veto→avoid,
never upgrade hold/avoid→buy, low-confidence buy→hold.
"""

from __future__ import annotations

import pytest

from decision import jev
from decision import vibe
from decision.schema import Action, DecideMode
from tests.test_risk_guards import _card, _packet_with_run


def test_preflight_disabled_by_default(monkeypatch):
    monkeypatch.setattr(jev, "ENABLED", False)
    status = jev.preflight()
    assert status.ready is False
    assert "JEV_ENABLED" in status.detail


def test_preflight_enabled_needs_key(monkeypatch):
    monkeypatch.setattr(jev, "ENABLED", True)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    monkeypatch.delenv("JEVX_API_KEY", raising=False)
    status = jev.preflight()
    assert status.enabled is True
    assert status.ready is False


def test_build_questions_shapes_validate():
    """The one strict thing in the JEV API: criteria shapes per type."""
    q = jev.build_questions()
    assert set(q) == {"action", "veto", "quality"}

    assert q["action"]["type"] == "choice"
    assert isinstance(q["action"]["criteria"], dict)
    assert set(q["action"]["criteria"]) >= {"buy", "hold", "avoid"}

    assert q["veto"]["type"] == "noul"
    assert set(q["veto"]["criteria"]) == {"true", "false"}

    assert q["quality"]["type"] == "score"
    assert isinstance(q["quality"]["criteria"], list)
    assert len(q["quality"]["criteria"]) >= 2

    for name, qq in q.items():
        assert qq["type"] in ("choice", "noul", "score"), name
        assert qq.get("instructions"), name
        assert qq.get("criteria") is not None, name


def test_parse_typesafe_payload():
    payload = {
        "model": "typesafe/jev-1.13.0",
        "answers": {
            "action": {
                "choice": "avoid",
                "probabilities": {"buy": 0.1, "hold": 0.25, "avoid": 0.65},
                "confidence": 0.72,
            },
            "veto": {"noul": 0.83},
            "quality": {"score": 0.2, "confidence": 0.6},
        },
    }
    v = jev._parse(payload, latency_ms=594, model=jev.MODEL)
    assert v.action == "avoid"
    assert v.action_probs["avoid"] == pytest.approx(0.65)
    assert v.action_confidence == pytest.approx(0.72)
    assert v.veto_prob == pytest.approx(0.83)
    assert v.quality01 == pytest.approx(0.2)
    assert v.model == "typesafe/jev-1.13.0"


def test_parse_score_normalizes_wide_scale():
    """A 3-step score may return 0..2 — normalize to 0..1."""
    payload = {"answers": {"quality": {"score": 1.6}}}
    v = jev._parse(payload, latency_ms=1, model="jev-latest")
    assert v.quality01 == pytest.approx(0.8)


def _stub_verdict(monkeypatch, **kw):
    verdict = jev.JevVerdict(
        action=kw.get("action", "hold"),
        action_probs=kw.get("action_probs", {}),
        action_confidence=kw.get("action_confidence", 0.8),
        veto_prob=kw.get("veto_prob", 0.5),
        quality01=kw.get("quality01", 0.5),
        quality_confidence=0.7,
        latency_ms=12,
        model="jev-latest",
        raw={},
    )
    monkeypatch.setattr(jev, "ENABLED", True)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(jev, "evaluate", lambda *a, **k: verdict)
    return verdict


def test_chair_veto_forces_avoid(monkeypatch):
    _stub_verdict(monkeypatch, action="hold", veto_prob=0.9)
    out = jev.chair_review(_card(), _packet_with_run(5.0), [])
    assert out is not None
    assert out["veto"] is True
    assert out["action"] == "avoid"


def test_chair_abstain_never_vetoes(monkeypatch):
    _stub_verdict(monkeypatch, action="avoid", veto_prob=0.5)
    out = jev.chair_review(_card(), _packet_with_run(5.0), [])
    assert out is not None
    assert out["veto"] is False


def test_chair_low_confidence_buy_downgrades_to_hold(monkeypatch):
    monkeypatch.setattr(jev, "MIN_CONFIDENCE", 0.55)
    _stub_verdict(monkeypatch, action="buy", action_confidence=0.4, veto_prob=0.2)
    out = jev.chair_review(_card(), _packet_with_run(5.0), [])
    assert out is not None
    assert out["veto"] is False
    assert out["action"] == "hold"
    assert "confidence floor" in out["reason"]


def test_chair_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(jev, "ENABLED", False)
    assert jev.chair_review(_card(), _packet_with_run(5.0), []) is None


def test_vibe_full_without_jev_stays_heuristic(monkeypatch):
    """Default path unchanged: disabled JEV adds no warnings, no backend."""
    monkeypatch.setattr(jev, "ENABLED", False)
    card = _card()
    review = vibe.review_card(card, _packet_with_run(5.0), mode=DecideMode.FULL)
    assert review.backend == "heuristic"
    assert not any("jev" in w.lower() for w in review.warnings)


def test_vibe_full_jev_veto_forces_avoid(monkeypatch):
    monkeypatch.setattr(
        jev,
        "chair_review",
        lambda *a, **k: {
            "veto": True,
            "action": "avoid",
            "claim": "jev avoid (90%) veto_p=0.90 q=0.10",
            "reason": "jev veto p=0.90",
            "backend": "jev",
            "confidence": 0.9,
            "veto_prob": 0.9,
            "quality": 0.1,
            "probs": {"avoid": 0.9},
            "latency_ms": 12,
            "model": "jev-latest",
        },
    )
    card = _card()
    review = vibe.review_card(card, _packet_with_run(5.0), mode=DecideMode.FULL)
    out = vibe.apply_review(card, review)
    assert out.action is Action.AVOID
    assert review.backend == "heuristic+jev"
    assert any("veto" in r for r in out.risk.veto_reasons)


def test_vibe_jev_buy_on_hold_is_ignored(monkeypatch):
    """Fast path did not clear Gate 2 — JEV cannot create a BUY."""
    monkeypatch.setattr(
        jev,
        "chair_review",
        lambda *a, **k: {
            "veto": False,
            "action": "buy",
            "claim": "jev buy",
            "reason": "jev buy",
            "backend": "jev",
            "confidence": 0.9,
            "veto_prob": 0.1,
            "quality": 0.9,
            "probs": {"buy": 0.9},
            "latency_ms": 5,
            "model": "jev-latest",
        },
    )
    card = _card(action=Action.HOLD, edge=0.0)
    review = vibe.review_card(card, _packet_with_run(5.0), mode=DecideMode.FULL)
    out = vibe.apply_review(card, review)
    assert out.action is not Action.BUY
    assert any("did not clear Gate 2" in w for w in review.warnings)


def test_vibe_jev_never_upgrades_avoid(monkeypatch):
    monkeypatch.setattr(
        jev,
        "chair_review",
        lambda *a, **k: {
            "veto": False,
            "action": "hold",
            "claim": "jev hold",
            "reason": "jev hold",
            "backend": "jev",
            "confidence": 0.8,
            "veto_prob": 0.2,
            "quality": 0.5,
            "probs": {"hold": 0.8},
            "latency_ms": 5,
            "model": "jev-latest",
        },
    )
    card = _card(action=Action.AVOID, edge=-3.0)
    review = vibe.review_card(card, _packet_with_run(5.0), mode=DecideMode.FULL)
    out = vibe.apply_review(card, review)
    assert out.action is Action.AVOID
    assert any("avoid stands" in w for w in review.warnings)
