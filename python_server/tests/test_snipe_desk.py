"""TradingAgents-shaped sniper desk: rank this mint vs recent creates."""

from __future__ import annotations

from decision import snipe_desk


def _review(**kwargs):
    defaults = dict(
        create_px=1e-6,
        last_px=1e-6 * 1.20,
        unique_buyers=5,
        buys=8,
        sells=0,
        real_sol=12.0,
        age_sec=6.0,
        watch_sec=45.0,
        curve_drop_pct=50.0,
    )
    defaults.update(kwargs)
    return snipe_desk.review(**defaults)


def test_warmup_buys_a_crowded_curve_not_a_thin_one():
    strong = _review()
    assert strong.ok is True
    assert strong.why == "desk_warmup"

    thin = _review(unique_buyers=2, last_px=1e-6 * 1.08, real_sol=3.0, buys=3)
    assert thin.ok is False
    assert thin.why == "thin_tape"


def test_one_buyer_never_clears_risk():
    row = _review(unique_buyers=1, real_sol=12.0, last_px=1e-6 * 1.15)
    assert row.ok is False
    assert row.why == "thin_tape"


def test_dev_sell_is_a_risk_veto():
    row = _review(dev_sold=True)
    assert row.ok is False
    assert row.why == "dev_sold"


def test_extended_rip_is_a_risk_veto():
    row = _review(unique_buyers=8, last_px=1e-6 * 1.90, real_sol=14.0)
    assert row.ok is False
    assert row.why == "too_extended"


def test_ranks_against_peers_not_a_fixed_floor():
    for _ in range(20):
        snipe_desk.observe(unique_buyers=2, lift_pct=6.0, real_sol=3.0, age_sec=10.0)
    strong = _review(unique_buyers=8, last_px=1e-6 * 1.18, real_sol=14.0, age_sec=5.0)
    assert strong.ok is True
    assert strong.peers == 20
    assert strong.composite >= 0.7
    assert strong.why.startswith("desk_p")

    weak = _review(unique_buyers=3, last_px=1e-6 * 1.06, real_sol=2.5, buys=3, age_sec=20.0)
    assert weak.ok is False
    assert weak.why == "below_peers"
