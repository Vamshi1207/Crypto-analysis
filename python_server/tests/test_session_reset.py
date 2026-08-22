"""Session board reset — archive today + clear in-memory book."""

from __future__ import annotations

from decision import arb, discover, paper, session
from decision import store as decision_store


def test_archive_today_moves_jsonl(isolated_store):
    decision_store.append("decisions", {"event": "old"})
    decision_store.append("paper", {"event": "fill"})
    path = decision_store._path_for("decisions")
    assert path.exists() and path.stat().st_size > 0

    moved = decision_store.archive_today(("decisions", "paper", "pipeline"))
    kinds = {m["kind"] for m in moved}
    assert "decisions" in kinds
    assert "paper" in kinds
    assert not path.exists() or path.stat().st_size == 0
    assert decision_store.count("decisions") == 0


def test_reset_board_clears_paper_and_cooloffs(isolated_store, monkeypatch):
    # Seed paper + cool-off state without hitting network on scan.
    paper.reset(starting_cash_usd=500.0)
    snap = paper.snapshot()
    assert snap["cash_usd"] == 500.0

    discover._cool_until["mintA"] = 9e12
    discover._no_edge_streak["mintA"] = 3
    discover._safety_ok["mintA"] = 1.0

    monkeypatch.setattr(discover, "scan_once", lambda: {"status": "ok", "trade_n": 0, "observe_n": 0, "watchlist": []})

    out = session.reset_board(reason="test")
    assert out["status"] == "ok"
    assert paper.snapshot()["cash_usd"] == 1000.0
    assert paper.snapshot()["open_count"] == 0
    assert discover._cool_until == {}
    assert discover._no_edge_streak == {}
    assert discover._safety_ok == {}
    assert out["discover_cleared"]["cool_cleared"] == 1
    assert arb.status()["paper_fills"] == 0

    # New activity writes into a fresh today file.
    decision_store.append("pipeline", {"event": "post_reset"})
    assert decision_store.count("pipeline") >= 1
