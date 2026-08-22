"""Append-only JSONL log of everything the decision engine concluded.

This exists before the forecasting layer on purpose. Conformal calibration
cannot invent its own history: to claim an 80% interval really covers 80% of
outcomes we need a durable record of what was predicted, when, and on what
evidence, written at decision time and never edited afterwards.

Daily files, one JSON object per line, so DuckDB can read the whole history with
`SELECT * FROM read_json_auto('store/decisions/*.jsonl')` and a partial write
from a crash costs one line rather than the file.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from pydantic import BaseModel

STORE_DIR = Path(os.getenv("DECISION_STORE_DIR", "/app/store"))

_write_lock = threading.Lock()


def _path_for(kind: str, day: Optional[date] = None) -> Path:
    day = day or datetime.now(timezone.utc).date()
    directory = STORE_DIR / kind
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{day.isoformat()}.jsonl"


def append(kind: str, record: BaseModel | dict[str, Any]) -> Path:
    """Append one record. Returns the file written to."""
    payload = record.model_dump(mode="json") if isinstance(record, BaseModel) else dict(record)
    payload.setdefault("logged_at", datetime.now(timezone.utc).isoformat())

    path = _path_for(kind)
    line = json.dumps(payload, default=str, separators=(",", ":"))

    # One process writes today, but the WebSocket handler and HTTP handlers are
    # separate threads, and interleaved lines would corrupt the log silently.
    with _write_lock:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    return path


def read(kind: str, day: Optional[date] = None) -> Iterator[dict[str, Any]]:
    """Yield records for one day, skipping any torn final line."""
    path = _path_for(kind, day)
    if not path.exists():
        return

    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def count(kind: str, day: Optional[date] = None) -> int:
    return sum(1 for _ in read(kind, day))


def archive_today(kinds: list[str] | tuple[str, ...]) -> list[dict[str, str]]:
    """Rename today's JSONL files aside so a new monitoring window starts empty.

    History is preserved under ``store/<kind>/<day>.pre-<utc-stamp>.jsonl``.
    """
    stamp = datetime.now(timezone.utc).strftime("%H%M%SZ")
    day = datetime.now(timezone.utc).date()
    moved: list[dict[str, str]] = []
    with _write_lock:
        for kind in kinds:
            src = _path_for(kind, day)
            if not src.exists() or src.stat().st_size == 0:
                continue
            dst = src.with_name(f"{day.isoformat()}.pre-{stamp}.jsonl")
            # Avoid clobbering an earlier archive in the same second.
            n = 1
            while dst.exists():
                dst = src.with_name(f"{day.isoformat()}.pre-{stamp}-{n}.jsonl")
                n += 1
            src.rename(dst)
            moved.append({"kind": kind, "from": str(src), "to": str(dst)})
    return moved
