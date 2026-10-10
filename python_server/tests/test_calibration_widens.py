"""Calibration must only ever widen an interval, never narrow it.

The regression these pin down: a fixed conformal radius was *replacing* the raw
ensemble spread, so a wildly disagreeing forecast (p10 -33.8%, p90 +100%) became
a confident all-positive band (p10 +5.7%, p90 +15.3%), driving max_loss_pct to
0.0 and letting the risk committee wave a blowoff-top entry through.
"""

from __future__ import annotations

import pytest

from decision import calibrate
from decision.schema import ForecastEnsemble, ReturnBand


@pytest.fixture(autouse=True)
def _residuals(monkeypatch):
    """Pin a 4.8% conformal radius, matching the live residual store."""
    errors = [4.8] * 360
    store = {
        "coverage_target": 0.8,
        "horizon": 6,
        "timeframe": "1",
        "errors": errors,
        "n": len(errors),
    }
    monkeypatch.setattr(calibrate, "load_residuals", lambda: store)
    return store


def _ensemble(p10: float, p50: float, p90: float, *, timeframe: str = "1", agreement: float = 0.5):
    band = ReturnBand(p10=p10, p50=p50, p90=p90)
    return ForecastEnsemble(
        horizon_bars=5,
        timeframe=timeframe,
        raw=band,
        calibrated=band,
        agreement=agreement,
    )


def test_wide_raw_spread_is_preserved_not_collapsed():
    """The exact WINNIE forecast that produced the bad buy."""
    out = calibrate.apply_conformal(_ensemble(-33.7619, 10.5077, 100.0484))

    # Must not narrow to p50 +/- 4.8 (which gave p10=+5.71, p90=+15.31).
    assert out.calibrated.p10 == pytest.approx(-33.7619, abs=1e-3)
    assert out.calibrated.p90 == pytest.approx(100.0484, abs=1e-3)
    assert out.calibrated.p50 == pytest.approx(10.5077, abs=1e-3)
    # Downside must stay downside: this is what feeds max_loss_pct.
    assert out.calibrated.p10 < 0


def test_narrow_raw_spread_is_widened_to_conformal():
    """A confident model still gets the empirical residual floor imposed."""
    out = calibrate.apply_conformal(_ensemble(-0.115, 0.4115, 1.0952))

    assert out.calibrated.p10 == pytest.approx(0.4115 - 4.8, abs=1e-3)
    assert out.calibrated.p90 == pytest.approx(0.4115 + 4.8, abs=1e-3)


@pytest.mark.parametrize(
    "p10,p50,p90",
    [
        (-33.7619, 10.5077, 100.0484),
        (-0.115, 0.4115, 1.0952),
        (-5.0, 0.0, 5.0),
        (2.0, 8.0, 9.0),
        (-80.0, -20.0, 10.0),
    ],
)
def test_calibration_never_narrows(p10, p50, p90):
    out = calibrate.apply_conformal(_ensemble(p10, p50, p90))
    assert out.calibrated.p10 <= p10 + 1e-9
    assert out.calibrated.p90 >= p90 - 1e-9
    raw_width = p90 - p10
    assert (out.calibrated.p90 - out.calibrated.p10) >= raw_width - 1e-9


def test_timeframe_mismatch_is_disclosed():
    out = calibrate.apply_conformal(_ensemble(-1.0, 0.0, 1.0, timeframe="5S"))
    assert "residuals fit on tf=1" in out.calibration_basis
    assert "not 5S" in out.calibration_basis


def test_too_few_residuals_passes_raw_through(monkeypatch):
    monkeypatch.setattr(calibrate, "load_residuals", lambda: {"errors": [1.0] * 5})
    out = calibrate.apply_conformal(_ensemble(-2.0, 1.0, 4.0))
    assert out.calibrated.p10 == pytest.approx(-2.0)
    assert out.calibrated.p90 == pytest.approx(4.0)
    assert out.residual_count == 5
    assert "raw_passthrough" in out.calibration_basis
