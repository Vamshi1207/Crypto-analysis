"""JEV exit monitor on open paper lots."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from decision import jev
from decision import paper
from decision.jev import JevUnavailable


def _lot(**kw):
    base = dict(
        id="exit1",
        address="Pool9",
        mint="Mint9",
        name="EXIT",
        entry_price=1.0,
        size_usd=40.0,
        qty=40.0,
        opened_at=(datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat(),
        target_profit_usd=100.0,
        stop_loss_usd=10.0,
        entry_reason="cluster",
    )
    base.update(kw)
    return paper.PaperPosition(**base)


def _quiet(monkeypatch):
    monkeypatch.setattr(paper, "JEV_EXIT_MONITOR", True)
    monkeypatch.setattr(paper, "JEV_EXIT_MIN_HOLD_SEC", 10.0)
    monkeypatch.setattr(paper, "JEV_EXIT_RECHECK_SEC", 60.0)
    monkeypatch.setattr(paper, "TRAIL_ENABLED", False)
    paper._jev_exit_checked.clear()


def test_exit_review_fires_on_high_p(monkeypatch):
    monkeypatch.setattr(
        jev, "_post", lambda *a, **k: ({"model": "t", "answers": {"exit_now": {"noul": 0.9}}}, 5)
    )
    out = jev.exit_review({"symbol": "X"})
    assert out is not None and out["exit"] is True and out["p"] == 0.9


def test_exit_review_holds_on_low_p(monkeypatch):
    monkeypatch.setattr(
        jev, "_post", lambda *a, **k: ({"model": "t", "answers": {"exit_now": {"noul": 0.2}}}, 5)
    )
    out = jev.exit_review({"symbol": "X"})
    assert out is not None and out["exit"] is False


def test_exit_review_none_when_chair_down(monkeypatch):
    def boom(*a, **k):
        raise JevUnavailable("down")

    monkeypatch.setattr(jev, "_post", boom)
    assert jev.exit_review({"symbol": "X"}) is None


def test_monitor_force_closes_on_exit(monkeypatch):
    from decision import jev as jev_mod

    _quiet(monkeypatch)
    monkeypatch.setattr(
        jev_mod, "exit_review", lambda state: {"exit": True, "p": 0.9, "latency_ms": 5, "model": "t"}
    )
    paper.reset()
    paper._state.open.append(_lot())
    closed = paper.mark_and_maybe_exit(address="Pool9", mark_price=0.99)
    assert len(closed) == 1
    assert "jev_exit" in (closed[0].get("close_reason") or "")


def test_monitor_leaves_lot_when_chair_says_hold(monkeypatch):
    from decision import jev as jev_mod

    _quiet(monkeypatch)
    monkeypatch.setattr(
        jev_mod, "exit_review", lambda state: {"exit": False, "p": 0.1, "latency_ms": 5, "model": "t"}
    )
    paper.reset()
    paper._state.open.append(_lot())
    assert paper.mark_and_maybe_exit(address="Pool9", mark_price=0.99) == []
    assert len(paper._state.open) == 1


def test_monitor_off_makes_no_chair_call(monkeypatch):
    from decision import jev as jev_mod

    _quiet(monkeypatch)
    monkeypatch.setattr(paper, "JEV_EXIT_MONITOR", False)

    def boom(state):
        raise AssertionError("chair must not be called")

    monkeypatch.setattr(jev_mod, "exit_review", boom)
    paper.reset()
    paper._state.open.append(_lot())
    assert paper.mark_and_maybe_exit(address="Pool9", mark_price=0.99) == []
