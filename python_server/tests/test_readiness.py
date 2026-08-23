"""Go/no-go readiness scorecard."""

from __future__ import annotations

from decision import readiness
from decision import store as decision_store


def test_readiness_no_go_on_empty_day(tmp_path, monkeypatch):
    monkeypatch.setattr(decision_store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setenv("LIVE_TRADING", "0")
    out = readiness.evaluate()
    assert out["ready_for_live"] is False
    assert out["score"] < 75
    rec = out["recommendation"].lower()
    assert "practicing" in rec or "getting there" in rec or "looks ready" in rec
    assert "plain_english" in out
    assert "session" in out
    assert out["session"] == out["today"]
    ids = {c["id"] for c in out["checks"]}
    assert "scalp_sample" in ids
    assert "live_flag_off" in ids


def test_readiness_counts_paper_arb_and_scalp(tmp_path, monkeypatch):
    monkeypatch.setattr(decision_store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setenv("LIVE_TRADING", "0")
    monkeypatch.setattr(readiness, "MIN_SCALP_CLOSES", 2)
    monkeypatch.setattr(readiness, "MIN_ARB_FILLS", 1)
    monkeypatch.setattr(readiness, "MIN_DECISIONS", 1)
    monkeypatch.setattr(readiness, "MIN_SCOREBOARD_N", 0)

    decision_store.append(
        "decisions",
        {"action": "buy", "card": {"action": "buy"}},
    )
    decision_store.append(
        "paper",
        {
            "event": "close",
            "position": {"realized_pnl_usd": 0.5},
        },
    )
    decision_store.append(
        "paper",
        {
            "event": "close",
            "position": {"realized_pnl_usd": 0.25},
        },
    )
    decision_store.append(
        "paper",
        {"event": "paper_arb", "realized_pnl_usd": 1.2},
    )

    out = readiness.evaluate()
    assert out["today"]["scalp_closes"] == 2
    assert out["today"]["arb_fills"] == 1
    assert out["today"]["scalp_ev"] == 0.375
    assert any(c["id"] == "scalp_sample" and c["pass"] for c in out["checks"])
    assert any(c["id"] == "arb_sample" and c["pass"] for c in out["checks"])
