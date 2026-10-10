"""Per-symbol cooldown, ranked rotation, multi-lane closes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from decision import paper


def _lot(**kw):
    base = dict(
        id="bk1",
        address="PoolB",
        mint="MintB",
        name="REBUY",
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


def test_symbol_cooldown_blocks_same_ticker_new_mint(monkeypatch):
    monkeypatch.setattr(paper, "SYMBOL_COOLDOWN_SEC", 3600.0)
    paper.reset()
    paper._symbol_cooldown.clear()
    paper._note_symbol_close("FOMO")
    out = paper.execute_signal(
        address="PoolNew",
        mint="MintNew",
        name="FOMO",
        mark_price=1.0,
        size_usd=40.0,
        strategy="cluster",
    )
    assert out["status"] == "skipped"
    assert "symbol_cooldown" in out["reason"]


def test_symbol_cooldown_off_allows_reentry(monkeypatch):
    monkeypatch.setattr(paper, "SYMBOL_COOLDOWN_SEC", 0.0)
    paper.reset()
    paper._note_symbol_close("FOMO")
    out = paper.execute_signal(
        address="PoolNew",
        mint="MintNew",
        name="FOMO",
        mark_price=1.0,
        size_usd=40.0,
        strategy="cluster",
    )
    assert out["status"] in ("opened", "pending")


def test_rotation_displaces_loser_at_cap(monkeypatch):
    monkeypatch.setattr(paper, "MAX_OPEN_POSITIONS", 1)
    monkeypatch.setattr(paper, "ROTATION_ENABLED", True)
    monkeypatch.setattr(paper, "SYMBOL_COOLDOWN_SEC", 0.0)
    paper.reset()
    paper._state.open.append(
        _lot(last_mark_pnl_usd=-1.0, peak_pnl_usd=-1.0, stop_loss_usd=1.6)
    )
    out = paper.execute_signal(
        address="PoolFresh",
        mint="MintFresh",
        name="FRESH",
        mark_price=1.0,
        size_usd=40.0,
        strategy="cluster",
    )
    assert out["status"] in ("opened", "pending")
    assert paper._state.closed[0].close_reason.startswith("rotation_displace")


def test_rotation_keeps_flat_lot_at_cap(monkeypatch):
    monkeypatch.setattr(paper, "MAX_OPEN_POSITIONS", 1)
    monkeypatch.setattr(paper, "ROTATION_ENABLED", True)
    paper.reset()
    paper._state.open.append(
        _lot(last_mark_pnl_usd=0.2, peak_pnl_usd=0.5, stop_loss_usd=1.6)
    )
    out = paper.execute_signal(
        address="PoolFresh",
        mint="MintFresh",
        name="FRESH",
        mark_price=1.0,
        size_usd=40.0,
        strategy="cluster",
    )
    assert out["status"] == "refused"
    assert len(paper._state.open) == 1


def test_close_by_mint_hits_all_lanes():
    paper.reset()
    with paper.use_lane("cluster"):
        paper.reset()
        paper._state.open.append(_lot(id="c1", address="PoolC"))
    with paper.use_lane("main"):
        paper._state.open.append(_lot(id="m1", address="PoolM"))
    closed = paper.close_by_mint(mint="MintB", mark_price=1.0, reason="t")
    assert len(closed) == 2
    with paper.use_lane("cluster"):
        assert len(paper._state.open) == 0
    with paper.use_lane("main"):
        assert len(paper._state.open) == 0
