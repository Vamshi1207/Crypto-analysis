"""Session-scoped store reads."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from decision import session, store as decision_store


def test_read_since_filters_by_logged_at(isolated_store):
    session.begin_session("2026-08-23T10:00:00+00:00")
    decision_store.append("paper", {"event": "open", "logged_at": "2026-08-23T09:59:00+00:00"})
    decision_store.append("paper", {"event": "close", "logged_at": "2026-08-23T10:01:00+00:00"})
    rows = session.read_since("paper")
    assert len(rows) == 1
    assert rows[0]["event"] == "close"


def test_reset_board_starts_new_session_window(isolated_store):
    session.bootstrap(reason="test")
    t0 = session.started_at_iso()
    decision_store.append("decisions", {"action": "hold", "logged_at": datetime.now(timezone.utc).isoformat()})
    assert session.count_since("decisions") == 1

    out = session.reset_board(reason="test")
    assert out["status"] == "ok"
    assert session.started_at_iso() != t0
    assert session.count_since("decisions") == 0

    decision_store.append("decisions", {"action": "buy", "logged_at": datetime.now(timezone.utc).isoformat()})
    assert session.count_since("decisions") == 1


def test_read_since_spans_midnight(isolated_store, monkeypatch):
    session.begin_session("2026-08-22T23:30:00+00:00")
    day = datetime(2026, 8, 22, tzinfo=timezone.utc).date()

    path22 = decision_store._path_for("paper", day)
    path22.parent.mkdir(parents=True, exist_ok=True)
    path22.write_text(
        '{"event":"old","logged_at":"2026-08-22T23:00:00+00:00"}\n'
        '{"event":"in","logged_at":"2026-08-22T23:45:00+00:00"}\n',
        encoding="utf-8",
    )

    day23 = day + timedelta(days=1)
    path23 = decision_store._path_for("paper", day23)
    path23.write_text('{"event":"next","logged_at":"2026-08-23T00:15:00+00:00"}\n', encoding="utf-8")

    monkeypatch.setattr(
        "decision.store.datetime",
        type(
            "T",
            (),
            {
                "now": staticmethod(lambda tz=None: datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)),
                "fromisoformat": datetime.fromisoformat,
            },
        ),
    )

    rows = session.read_since("paper")
    events = {r["event"] for r in rows}
    assert "old" not in events
    assert "in" in events
    assert "next" in events
