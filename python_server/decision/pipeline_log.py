"""Structured pipeline logging for every stage of discover → decide → paper.

Writes append-only JSONL under ``store/pipeline/`` (DuckDB-friendly) and mirrors
a one-line summary to the process logger so ``docker compose logs`` stays useful.

Correlation: wrap work in ``with run(stage):`` (or pass ``run_id=``) so a discover
scan or swarm tick can be joined later:

    SELECT * FROM read_json_auto('store/pipeline/*.jsonl')
    WHERE run_id = '…' ORDER BY logged_at;
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional

from decision import store as decision_store

PIPELINE_KIND = "pipeline"
JSONL_ENABLED = os.getenv("PIPELINE_LOG_JSONL", "1").strip() != "0"
CONSOLE_ENABLED = os.getenv("PIPELINE_LOG_CONSOLE", "1").strip() != "0"
# When 0, skip high-frequency hold/skip decide events (still log buy/avoid/errors).
LOG_HOLDS = os.getenv("PIPELINE_LOG_HOLDS", "1").strip() != "0"

_logger = logging.getLogger("pipeline")
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [pipeline] %(message)s")
    )
    _logger.addHandler(_handler)
    _logger.setLevel(os.getenv("PIPELINE_LOG_LEVEL", "INFO").upper())
    _logger.propagate = False

_run_id: ContextVar[Optional[str]] = ContextVar("pipeline_run_id", default=None)
_seq_lock = threading.Lock()
_seq = 0


def current_run_id() -> Optional[str]:
    return _run_id.get()


def new_run_id(prefix: str = "") -> str:
    stem = uuid.uuid4().hex[:10]
    return f"{prefix}{stem}" if prefix else stem


@contextmanager
def run(prefix: str = "", *, run_id: Optional[str] = None) -> Iterator[str]:
    """Bind a correlation id for the duration of a scan/tick/request."""
    rid = run_id or new_run_id(prefix)
    token = _run_id.set(rid)
    try:
        yield rid
    finally:
        _run_id.reset(token)


def emit(
    stage: str,
    event: str,
    *,
    level: str = "info",
    run_id: Optional[str] = None,
    address: Optional[str] = None,
    mint: Optional[str] = None,
    symbol: Optional[str] = None,
    duration_ms: Optional[float] = None,
    **fields: Any,
) -> dict[str, Any]:
    """Record one pipeline event. Never raises — logging must not break trading."""
    global _seq
    with _seq_lock:
        _seq += 1
        seq = _seq

    rid = run_id if run_id is not None else _run_id.get()
    record: dict[str, Any] = {
        "stage": stage,
        "event": event,
        "level": level,
        "seq": seq,
    }
    if rid:
        record["run_id"] = rid
    if address:
        record["address"] = address
    if mint:
        record["mint"] = mint
    if symbol:
        record["symbol"] = symbol
    if duration_ms is not None:
        record["duration_ms"] = round(float(duration_ms), 2)
    # Drop Nones from optional detail fields.
    for key, value in fields.items():
        if value is not None:
            record[key] = value

    if CONSOLE_ENABLED:
        _console(record, level)

    if JSONL_ENABLED:
        try:
            decision_store.append(PIPELINE_KIND, record)
        except OSError as exc:
            _logger.warning("pipeline jsonl write failed: %s", exc)

    return record


def emit_decide_card(card: Any, *, run_id: Optional[str] = None) -> None:
    """Compact decide outcome — full card already lives in store/decisions/."""
    action = getattr(card.action, "value", None) or str(getattr(card, "action", ""))
    if not LOG_HOLDS and action in ("hold",):
        # Still log holds that failed a named gate for analysis.
        failed = list(getattr(card, "gates_failed", None) or [])
        if not failed:
            return

    tok = getattr(card, "token", None) or {}
    if not isinstance(tok, dict):
        tok = {}
    band = getattr(card, "expected_return_pct", None)
    emit(
        "decide",
        "card",
        level="info" if action != "avoid" else "warning",
        run_id=run_id,
        address=tok.get("address"),
        mint=tok.get("mint"),
        symbol=tok.get("name"),
        action=action,
        edge=getattr(card, "cost_adjusted_edge_pct", None),
        confidence=getattr(card, "action_confidence", None),
        gates_passed=list(getattr(card, "gates_passed", None) or []),
        gates_failed=list(getattr(card, "gates_failed", None) or []),
        p10=getattr(band, "p10", None) if band is not None else None,
        p50=getattr(band, "p50", None) if band is not None else None,
        p90=getattr(band, "p90", None) if band is not None else None,
        latency_ms=getattr(card, "latency_ms", None),
        mode=getattr(getattr(card, "mode", None), "value", None)
        or str(getattr(card, "mode", "") or "")
        or None,
        summary=(card.summary() if hasattr(card, "summary") else None),
        veto=(getattr(getattr(card, "risk", None), "veto_reasons", None) or None),
        source=tok.get("source"),
    )


def recent(limit: int = 100, *, stage: Optional[str] = None) -> list[dict[str, Any]]:
    """Latest pipeline events for today (newest last)."""
    rows = list(decision_store.read(PIPELINE_KIND))
    if stage:
        rows = [r for r in rows if r.get("stage") == stage]
    if limit > 0:
        rows = rows[-limit:]
    return rows


def count_today() -> int:
    return decision_store.count(PIPELINE_KIND)


def timed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def _console(record: dict[str, Any], level: str) -> None:
    stage = record.get("stage")
    event = record.get("event")
    bits = [f"{stage}.{event}"]
    if record.get("run_id"):
        bits.append(f"run={record['run_id']}")
    if record.get("symbol"):
        bits.append(str(record["symbol"]))
    elif record.get("address"):
        bits.append(str(record["address"])[:10])
    for key in ("action", "reason", "status", "edge", "verdict", "bars", "watchlist_n"):
        if key in record and record[key] is not None:
            bits.append(f"{key}={record[key]}")
    if record.get("duration_ms") is not None:
        bits.append(f"{record['duration_ms']:.0f}ms")
    msg = " ".join(bits)
    log_fn = {
        "debug": _logger.debug,
        "info": _logger.info,
        "warning": _logger.warning,
        "error": _logger.error,
    }.get(level, _logger.info)
    log_fn(msg)
