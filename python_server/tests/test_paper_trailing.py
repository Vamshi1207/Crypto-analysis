"""Percent-scaled stops and trailing take-profit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from decision import paper


@pytest.fixture()
def trailing(monkeypatch):
    monkeypatch.setattr(paper, "TRAIL_ENABLED", True)
    monkeypatch.setattr(paper, "TRAIL_ARM_USD", 1.0)
    monkeypatch.setattr(paper, "TRAIL_GIVEBACK_PCT", 0.5)
    return paper


def _position(size: float = 150.0, target: float = 1.0) -> paper.PaperPosition:
    return paper.PaperPosition(
        id="p1",
        address="Pool1",
        mint="Mint1",
        name="MEME",
        entry_price=1.0,
        size_usd=size,
        qty=size,
        opened_at=datetime.now(timezone.utc).isoformat(),
        target_profit_usd=target,
        stop_loss_usd=paper.stop_loss_for_size(size),
    )


def test_percent_stop_scales_with_size(monkeypatch):
    monkeypatch.setattr(paper, "STOP_LOSS_PCT", 2.0)
    assert paper.stop_loss_for_size(150.0) == 3.0
    assert paper.stop_loss_for_size(40.0) == 0.8


def test_flat_stop_used_when_percent_disabled(monkeypatch):
    monkeypatch.setattr(paper, "STOP_LOSS_PCT", 0.0)
    monkeypatch.setattr(paper, "STOP_LOSS_USD", 1.5)
    assert paper.stop_loss_for_size(150.0) == 1.5


def test_trail_arms_at_target_and_lets_the_winner_run(trailing):
    pos = _position()
    # Below the arm amount the trail is inert.
    assert trailing._trail_exit(pos, 0.4) is None
    assert pos.trail_armed is False

    # Arming does not close: the position is held for more upside.
    assert trailing._trail_exit(pos, 1.2) is None
    assert pos.trail_armed is True
    assert pos.peak_pnl_usd == 1.2

    # Still running — peak tracks up, no exit.
    assert trailing._trail_exit(pos, 4.0) is None
    assert pos.peak_pnl_usd == 4.0


def test_trail_banks_after_giveback_from_peak(trailing):
    pos = _position()
    trailing._trail_exit(pos, 4.0)
    # Floor is the looser of 0.5% of size ($0.75) and 35% of peak ($1.40),
    # then clamped to the $1 arm → $2.60. A $0.50 dip still runs.
    assert trailing._trail_exit(pos, 3.5) is None
    assert trailing._trail_exit(pos, 3.2) is None
    exited = trailing._trail_exit(pos, 2.50)
    assert exited is not None
    pnl, reason = exited
    assert pnl == 2.50
    assert "trail_take_profit" in reason
    assert "peak $4.00" in reason


def test_armed_trail_survives_a_dip_above_the_floor(trailing):
    pos = _position()
    trailing._trail_exit(pos, 1.5)
    # Peak $1.50, size giveback $0.75, floor clamped to the $1 arm.
    assert trailing._trail_exit(pos, 1.20) is None
    assert pos.trail_armed is True


def test_ghost_spike_does_not_arm_the_trail(trailing):
    pos = _position(size=80.0)
    # +$45 on an $80 clip is a stale Dex mid, not a fillable print.
    assert trailing._trail_exit(pos, 45.0) is None
    assert pos.trail_armed is False
    assert pos.peak_pnl_usd == 0.0
    # A later honest mark still arms normally.
    assert trailing._trail_exit(pos, 2.0) is None
    assert pos.trail_armed is True
    assert pos.peak_pnl_usd == 2.0


def test_trail_never_closes_below_the_arm(trailing):
    pos = _position(size=80.0)
    trailing._trail_exit(pos, 8.0)
    assert pos.trail_armed is True
    # Snap-back through the floor to a loss is a stop's job, not a take-profit.
    assert trailing._trail_exit(pos, -1.04) is None
    assert pos.trail_armed is False


def test_launch_grace_ignores_first_tick_venue_gap(trailing, monkeypatch):
    monkeypatch.setattr(paper, "STOP_LOSS_PCT", 2.0)
    monkeypatch.setattr(paper, "STOP_FILL_MODE", "barrier")
    monkeypatch.setattr(paper, "STOP_GRACE_SEC", 12.0)
    monkeypatch.setattr(paper, "ENTRY_CONFIRM_PCT", 8.0)
    paper.reset()
    pos = _position(size=80.0)
    pos.entry_reason = "launch"
    pos.stop_loss_usd = 1.60
    with paper._lock:
        paper._state.open.append(pos)
        paper._state.cash_usd = 920.0
    # Same-tick Dex print 50% below the Gecko fill — used to book -$1.60.
    assert paper.mark_and_maybe_exit(address="Pool1", mark_price=0.50) == []
    assert pos.status == "open"
    paper.reset()


def test_max_hold_still_fires_when_mark_is_an_unconfirmed_spike(trailing, monkeypatch):
    """A 2x Dex mid must not pin a lot open past its clock — gamerfaroe bug."""
    monkeypatch.setattr(paper, "STOP_FILL_MODE", "barrier")
    paper.reset()
    pos = _position(size=80.0)
    pos.entry_reason = "cluster"
    pos.max_hold_sec = 300.0
    pos.stop_loss_usd = 1.60
    pos.last_mark_pnl_usd = -1.04
    pos.opened_at = (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat()
    with paper._lock:
        paper._state.open.append(pos)
        paper._state.cash_usd = 920.0
    closed = paper.mark_and_maybe_exit(address="Pool1", mark_price=1.97)
    assert closed
    assert "max_hold" in (closed[0].get("close_reason") or "")
    assert "unconfirmed" in (closed[0].get("close_reason") or "")
    # Book the last accepted print, not the +97% ghost.
    assert closed[0]["realized_pnl_usd"] == pytest.approx(-1.04, abs=0.02)
    paper.reset()


def test_up_jumps_ratchet_so_a_real_runner_can_arm(trailing, monkeypatch):
    monkeypatch.setattr(paper, "TRAIL_MAX_JUMP_PCT", 15.0)
    paper.reset()
    pos = _position(size=80.0)
    pos.entry_reason = "cluster"
    pos.max_hold_sec = 300.0
    pos.last_mark_pnl_usd = -1.0
    pos.opened_at = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
    with paper._lock:
        paper._state.open.append(pos)
        paper._state.cash_usd = 920.0
    assert paper.mark_and_maybe_exit(address="Pool1", mark_price=1.97) == []
    assert pos.status == "open"
    assert pos.trail_armed is True
    assert pos.last_mark_pnl_usd == pytest.approx(11.0, abs=0.05)
    paper.reset()


def test_snipe_banks_the_dollar_and_leaves(trailing, monkeypatch):
    paper.reset()
    opened = paper.execute_signal(
        address="Pool1",
        mint="Mint1",
        name="SNIPE",
        mark_price=1.0,
        size_usd=40.0,
        strategy="snipe",
        max_hold_sec=45.0,
        bank_at_target=True,
        target_profit_usd=1.0,
    )
    assert opened["status"] == "opened"
    pos = paper.snapshot()["open"][0]
    # ~6% up on $40 clears the $1 target after exit costs.
    closed = paper.mark_and_maybe_exit(address="Pool1", mark_price=1.07)
    assert closed
    assert closed[0]["realized_pnl_usd"] == pytest.approx(1.0, abs=0.02)
    assert "snipe_target" in (closed[0].get("close_reason") or "")
    paper.reset()


def test_snipe_rip_banks_percent_of_size(trailing):
    paper.reset()
    opened = paper.execute_signal(
        address="PoolRip",
        mint="MintRip",
        name="RIP",
        mark_price=1.0,
        size_usd=40.0,
        strategy="snipe",
        max_hold_sec=12.0,
        bank_at_target=True,
        target_profit_usd=1.0,
        take_profit_pct=40.0,
    )
    assert opened["status"] == "opened"
    # ~50% up clears the 40% rip ($16) before the $1 clip.
    closed = paper.mark_and_maybe_exit(address="PoolRip", mark_price=1.55)
    assert closed
    assert "snipe_rip" in (closed[0].get("close_reason") or "")
    assert closed[0]["realized_pnl_usd"] == pytest.approx(16.0, abs=0.05)
    paper.reset()


def test_snipe_dead_tape_scratches_early(trailing):
    paper.reset()
    opened = paper.execute_signal(
        address="PoolDead",
        mint="MintDead",
        name="DEAD",
        mark_price=1.0,
        size_usd=20.0,
        strategy="snipe",
        max_hold_sec=45.0,
        bank_at_target=True,
        target_profit_usd=1.0,
        dead_after_sec=1.0,
        abs_hold_sec=90.0,
    )
    assert opened["status"] == "opened"
    pos = next(p for p in paper._state.open if p.address == "PoolDead")
    pos.opened_at = (datetime.now(timezone.utc) - timedelta(seconds=3)).isoformat()
    closed = paper.mark_and_maybe_exit(address="PoolDead", mark_price=1.0)
    assert closed
    assert "dead_tape" in (closed[0].get("close_reason") or "")
    paper.reset()


def test_snipe_dump_hits_stop_not_a_twenty_dollar_scratch(trailing, monkeypatch):
    monkeypatch.setattr(paper, "STOP_FILL_MODE", "barrier")
    monkeypatch.setattr(paper, "STOP_GRACE_SEC", 12.0)
    monkeypatch.setattr(paper, "ENTRY_CONFIRM_PCT", 8.0)
    paper.reset()
    opened = paper.execute_signal(
        address="PoolDump",
        mint="MintDump",
        name="DUMP",
        mark_price=1.0,
        size_usd=40.0,
        strategy="snipe",
        max_hold_sec=45.0,
        bank_at_target=True,
        target_profit_usd=1.0,
        stop_loss_usd=0.80,
        dead_after_sec=6.0,
        abs_hold_sec=90.0,
    )
    assert opened["status"] == "opened"
    closed = paper.mark_and_maybe_exit(address="PoolDump", mark_price=0.48)
    assert closed
    assert "stop_loss" in (closed[0].get("close_reason") or "")
    assert closed[0]["realized_pnl_usd"] == pytest.approx(-0.80, abs=0.02)
    paper.reset()


def test_snipe_green_tape_skips_the_clock(trailing):
    paper.reset()
    opened = paper.execute_signal(
        address="PoolRun",
        mint="MintRun",
        name="RUN",
        mark_price=1.0,
        size_usd=20.0,
        strategy="snipe",
        max_hold_sec=4.0,
        bank_at_target=True,
        target_profit_usd=1.0,
        take_profit_pct=40.0,
        dead_after_sec=2.0,
        abs_hold_sec=90.0,
    )
    assert opened["status"] == "opened"
    # First tick: enough lift to go green after fees, not enough to bank $1.
    assert paper.mark_and_maybe_exit(address="PoolRun", mark_price=1.03) == []
    pos = next(p for p in paper._state.open if p.address == "PoolRun")
    assert pos.peak_pnl_usd > 0
    pos.opened_at = (datetime.now(timezone.utc) - timedelta(seconds=12)).isoformat()
    still = paper.mark_and_maybe_exit(address="PoolRun", mark_price=1.035)
    assert still == []
    paper.reset()


def test_ghost_then_dump_closes_as_stop_not_trail(trailing, monkeypatch):
    monkeypatch.setattr(paper, "STOP_LOSS_PCT", 2.0)
    monkeypatch.setattr(paper, "STOP_FILL_MODE", "barrier")
    paper.reset()
    pos = _position(size=80.0)
    pos.stop_loss_usd = 1.60
    with paper._lock:
        paper._state.open.append(pos)
        paper._state.cash_usd = 920.0
    assert paper.mark_and_maybe_exit(address="Pool1", mark_price=1.56) == []
    closed = paper.mark_and_maybe_exit(address="Pool1", mark_price=0.97)
    assert closed
    assert "trail" not in (closed[0].get("close_reason") or "")
    paper.reset()
