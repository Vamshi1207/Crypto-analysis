"""Per-token risk sizing — a full stop-out costs risk_usd by construction."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from decision import sizing as sizing_mod


def _plan(**kw):
    base = {
        "strategy": "cluster",
        "base_risk_usd": 1.60,
        "flat_size_usd": 40.0,
        "flat_stop_usd": 1.60,
        "liquidity_usd": 25_000.0,
        "safety_score": 10.0,
        "vol_proxy": 0.06,
        "conviction_mult": 1.0,
    }
    base.update(kw)
    return sizing_mod.plan_size(**base)


def test_full_conviction_safe_token_risks_base():
    p = _plan()
    assert p.risk_usd == 1.60
    assert p.size_usd * p.stop_frac == pytest.approx(p.stop_usd, abs=0.01)
    assert abs(p.stop_usd - p.risk_usd) < 0.05


def test_worse_safety_risks_less():
    assert _plan(safety_score=10.0).risk_usd > _plan(safety_score=50.0).risk_usd
    assert _plan(safety_score=50.0).risk_usd > _plan(safety_score=80.0).risk_usd


def test_thin_liquidity_shrinks_size():
    deep = _plan(liquidity_usd=50_000.0)
    thin = _plan(liquidity_usd=2_000.0)
    assert thin.size_usd < deep.size_usd
    assert thin.size_usd <= 2_000.0 * sizing_mod.MAX_LIQ_FRAC


def test_exit_impact_widens_stop_and_shrinks_size():
    clean = _plan(price_impact_pct=0.2)
    toxic = _plan(price_impact_pct=8.0)
    assert toxic.stop_frac > clean.stop_frac
    assert toxic.size_usd < clean.size_usd


def test_clamps_hold():
    p = _plan(base_risk_usd=100.0, liquidity_usd=10_000_000.0)
    assert p.risk_usd <= sizing_mod.MAX_RISK_USD
    assert p.size_usd <= sizing_mod.MAX_SIZE_USD
    p = _plan(base_risk_usd=0.01, liquidity_usd=100.0, conviction_mult=0.1)
    assert p.risk_usd >= sizing_mod.MIN_RISK_USD
    assert p.size_usd >= 1.0


def test_disabled_returns_flat(monkeypatch):
    monkeypatch.setattr(sizing_mod, "ENABLED", False)
    p = _plan()
    assert (p.size_usd, p.stop_usd) == (40.0, 1.60)
    assert p.why == "sizing_off"


def test_report_features_handles_shapes():
    assert sizing_mod.report_features(None) == (None, None)
    rep = SimpleNamespace(
        risk_score=30, sellability=SimpleNamespace(price_impact_pct=0.5)
    )
    assert sizing_mod.report_features(rep) == (30.0, 0.5)
    assert sizing_mod.report_features(SimpleNamespace()) == (None, None)


def test_conviction_clamped():
    assert _plan(conviction_mult=5.0).risk_usd == _plan(conviction_mult=1.0).risk_usd
    assert _plan(conviction_mult=0.0).risk_usd == _plan(conviction_mult=0.25).risk_usd
