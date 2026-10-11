"""On-chain latency fill simulation."""

from __future__ import annotations

import time

from decision import exec_sim


def test_worst_buy_takes_the_high():
    assert exec_sim.worst_buy_price(1.0, [1.0, 1.05, 0.99]) == 1.05


def test_worst_sell_takes_the_low():
    assert exec_sim.worst_sell_price(1.0, [1.0, 1.05, 0.97]) == 0.97


def test_window_and_resolve_buy(monkeypatch):
    monkeypatch.setattr(exec_sim, "ENABLED", True)
    monkeypatch.setattr(exec_sim, "WORST_FILL", True)
    monkeypatch.setattr(exec_sim, "LATENCY_SEC", 0.05)
    monkeypatch.setattr(exec_sim, "ADVERSE_BPS", 0.0)
    exec_sim.reset()
    pending = exec_sim.queue_buy(
        lane="cluster",
        address="PoolA",
        mint="MintA",
        name="TEST",
        signal_price=1.0,
        size_usd=40.0,
        strategy="cluster",
        reserved_usd=40.12,
    )
    # Marks that arrive after the signal (submit→land window).
    exec_sim.observe("MintA", 1.03, ts=pending.signal_at + 0.01)
    exec_sim.observe("MintA", 1.08, ts=pending.signal_at + 0.02)
    pending.fill_at = pending.signal_at  # due immediately
    due = exec_sim.due_buys(now=pending.signal_at + 0.1)
    assert len(due) == 1
    resolved = exec_sim.resolve_buy(due[0], fallback_price=1.02)
    assert resolved["fill_price_raw"] == 1.08
    assert resolved["adverse_pct"] == 8.0


def test_resolve_sell_adverse(monkeypatch):
    monkeypatch.setattr(exec_sim, "LATENCY_SEC", 0.05)
    monkeypatch.setattr(exec_sim, "ADVERSE_BPS", 0.0)
    exec_sim.reset()
    t0 = time.time()
    exec_sim.observe("MintB", 2.0, ts=t0)
    exec_sim.observe("MintB", 1.85, ts=t0 + 0.01)
    out = exec_sim.resolve_sell(
        mint="MintB", signal_price=2.0, signal_at=t0, fallback_price=1.9, now=t0 + 0.05
    )
    assert out["fill_price_raw"] == 1.85
    assert out["latency_sec"] == 0.05


def test_resolve_sell_ignores_marks_after_latency_window(monkeypatch):
    """Late resolve must not widen the adverse window past signal+LATENCY."""
    monkeypatch.setattr(exec_sim, "LATENCY_SEC", 0.05)
    monkeypatch.setattr(exec_sim, "WORST_FILL", True)
    monkeypatch.setattr(exec_sim, "ADVERSE_BPS", 0.0)
    exec_sim.reset()
    t0 = time.time()
    exec_sim.observe("MintC", 2.0, ts=t0)
    exec_sim.observe("MintC", 1.90, ts=t0 + 0.02)
    # Rug print arrives long after the land slot — must be ignored.
    exec_sim.observe("MintC", 0.50, ts=t0 + 30.0)
    out = exec_sim.resolve_sell(
        mint="MintC", signal_price=2.0, signal_at=t0, fallback_price=None, now=t0 + 30.0
    )
    assert out["fill_price_raw"] == 1.90
    assert out["latency_sec"] == 0.05


def test_drain_pending_exits_on_timer(monkeypatch):
    """Pending sells resolve on the latency timer, not the next trenches tick."""
    from datetime import datetime, timezone

    from decision import paper

    monkeypatch.setattr(exec_sim, "ENABLED", True)
    monkeypatch.setattr(exec_sim, "WORST_FILL", True)
    monkeypatch.setattr(exec_sim, "LATENCY_SEC", 0.05)
    monkeypatch.setattr(exec_sim, "ADVERSE_BPS", 0.0)
    exec_sim.reset()
    paper.reset(starting_cash_usd=1000.0)

    now = time.time()
    pos = paper.PaperPosition(
        id="px1",
        address="PoolX",
        mint="MintX",
        name="TESTX",
        entry_price=1.0,
        size_usd=40.0,
        qty=40.0,
        opened_at=datetime.now(timezone.utc).isoformat(),
        target_profit_usd=1.0,
        stop_loss_usd=1.5,
        pending_exit_at=now - 0.1,
        pending_exit_reason="max_hold 90s pnl=$-1.20",
        pending_exit_signal_price=0.96,
    )
    with paper._lock:
        paper._state.open = [pos]
        paper._state.cash_usd = 960.0

    exec_sim.observe("MintX", 0.96, ts=pos.pending_exit_at)
    exec_sim.observe("MintX", 0.90, ts=pos.pending_exit_at + 0.02)

    closed = paper.drain_pending_exits(fallback_price_fn=lambda m, a: 0.92)
    assert len(closed) == 1
    assert closed[0]["status"] == "closed"
    # Worst mid in [signal, signal+LATENCY] (fallback is worse than signal but
    # better than the 0.90 print, so 0.90 wins).
    assert closed[0]["exit_price"] == 0.90
    assert "exec_lag 0.05s" in (closed[0].get("close_reason") or "")
    with paper._lock:
        assert paper._state.open == []
        assert len(paper._state.closed) == 1


def test_adverse_bps_widens_buy_and_sell(monkeypatch):
    """A non-zero adverse-selection adder must move the fill against us."""
    monkeypatch.setattr(exec_sim, "ADVERSE_BPS", 100.0)  # 1%
    exec_sim.reset()
    t0 = time.time()
    exec_sim.observe("MintD", 1.0, ts=t0)
    buy = exec_sim.resolve_sell(mint="MintD", signal_price=1.0, signal_at=t0, fallback_price=None, now=t0)
    assert buy["fill_price_raw"] == 1.0 * (1.0 - 100.0 / 10_000.0)

    monkeypatch.setattr(exec_sim, "ADVERSE_BPS", 100.0)
    pending = exec_sim.queue_buy(
        lane="main", address="PoolD", mint="MintE", name="T", signal_price=1.0,
        size_usd=10.0, strategy="snipe", reserved_usd=10.1,
    )
    pending.fill_at = pending.signal_at
    res = exec_sim.resolve_buy(pending, fallback_price=None)
    assert res["fill_price_raw"] == 1.0 * (1.0 + 100.0 / 10_000.0)
