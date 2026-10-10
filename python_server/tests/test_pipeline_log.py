"""Structured pipeline logging across discover → gates → paper."""

from __future__ import annotations

from decision import pipeline_log
from decision.schema import (
    Action,
    DecideMode,
    DecisionCard,
    Direction,
    ForecastEnsemble,
    ReturnBand,
    RiskBlock,
)


def test_emit_writes_jsonl(tmp_path, monkeypatch):
    from decision import store as decision_store

    monkeypatch.setattr(decision_store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(pipeline_log, "JSONL_ENABLED", True)
    monkeypatch.setattr(pipeline_log, "CONSOLE_ENABLED", False)

    with pipeline_log.run(prefix="t-") as run_id:
        pipeline_log.emit("discover", "scan_start", scanned=3)
        pipeline_log.emit("gate2", "fail", edge=-4.0, reason="below target")

    rows = list(decision_store.read("pipeline"))
    assert len(rows) >= 2
    assert any(r.get("run_id") == run_id for r in rows)
    assert any(r["stage"] == "discover" and r["event"] == "scan_start" for r in rows)
    assert any(r["event"] == "fail" and r["stage"] == "gate2" for r in rows)


def test_emit_decide_card_compacts(tmp_path, monkeypatch):
    from decision import store as decision_store

    monkeypatch.setattr(decision_store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(pipeline_log, "JSONL_ENABLED", True)
    monkeypatch.setattr(pipeline_log, "CONSOLE_ENABLED", False)
    monkeypatch.setattr(pipeline_log, "LOG_HOLDS", True)

    rb = ReturnBand(p10=-2, p50=1, p90=5)
    card = DecisionCard(
        token={"address": "PoolX", "mint": "MintX", "name": "TEST", "source": "live"},
        horizon_bars=6,
        timeframe="1",
        action=Action.HOLD,
        action_confidence=0.5,
        confidence_basis="test",
        direction=Direction.DOWN,
        expected_return_pct=rb,
        cost_adjusted_edge_pct=-4.5,
        position={"max_size_usd": 40, "size_basis": "test"},
        risk=RiskBlock(risk_pass=True),
        forecast=ForecastEnsemble(
            horizon_bars=6,
            timeframe="1",
            raw=rb,
            calibrated=rb,
            agreement=0.8,
            models=[],
            residual_count=10,
            backend="stat",
        ),
        gates_failed=["gate2"],
        mode=DecideMode.FAST,
        latency_ms=12,
    )
    pipeline_log.emit_decide_card(card)
    rows = list(decision_store.read("pipeline"))
    assert rows[-1]["stage"] == "decide"
    assert rows[-1]["action"] == "hold"
    assert rows[-1]["edge"] == -4.5
    assert "gate2" in rows[-1]["gates_failed"]


def test_recent_filters_by_stage(tmp_path, monkeypatch):
    from decision import store as decision_store

    monkeypatch.setattr(decision_store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(pipeline_log, "JSONL_ENABLED", True)
    monkeypatch.setattr(pipeline_log, "CONSOLE_ENABLED", False)

    pipeline_log.emit("swarm", "tick_done", token_n=2)
    pipeline_log.emit("paper", "opened", address="a")
    only_paper = pipeline_log.recent(limit=50, stage="paper")
    assert only_paper and all(r["stage"] == "paper" for r in only_paper)
