"""Read-only access to the Axiom-extracted candle corpus.

The files under `ML_Training_datasets/CandleData/` are **historical OHLCV only**.
Timestamps and on-chain identity are stale — pools may be gone, mints may not
resolve — so this module never assumes a live mint. Use it for:

  * indicator / schema checks
  * offline forecast calibration (Phase 2)
  * forward-return labeling against realized closes

Do not feed these addresses into Gate 0 or Jupiter. Live screening belongs on
tokens the extension is streaming right now.

Layout (written by the extension ingest path in `server.py`):

    CandleData/
      Candles/{address}_candles.json   name, address, timeframes, updated
      Stats/{address}_stats.json       buy/sell bucket stats

Environment override: `CANDLE_DATA_DIR` (same as the Flask server).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

DEFAULT_CANDLE_DATA_DIR = (
    Path(__file__).resolve().parents[1]
    / "ML_Training_datasets"
    / "CandleData"
    / "archive_historical"
)

TIMEFRAMES = ("5S", "15S", "30S", "1", "3", "5", "15", "30", "60")

# Full JSON parse is fine below this. Above it, prefer `load_timeframe` with
# `tail=` so tests never hold 87 MB of candles in memory for longer than needed.
FULL_LOAD_BUDGET_BYTES = 30_000_000


def candle_data_dir() -> Path:
    override = os.getenv("CANDLE_DATA_DIR", "").strip()
    return Path(override) if override else DEFAULT_CANDLE_DATA_DIR


def candles_dir(root: Optional[Path] = None) -> Path:
    return (root or candle_data_dir()) / "Candles"


def stats_dir(root: Optional[Path] = None) -> Path:
    return (root or candle_data_dir()) / "Stats"


@dataclass(frozen=True)
class TokenRef:
    """Lightweight index entry. Built from the filename, no JSON parse.

    `address` is whatever Axiom put in the URL (often a pool). Treat it as a
    file key only — not a live mint.
    """

    address: str
    candles_path: Path
    stats_path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        return self.size_bytes / 1_000_000.0

    @property
    def has_stats(self) -> bool:
        return self.stats_path.exists()


def list_tokens(root: Optional[Path] = None) -> list[TokenRef]:
    """Index every candle file. Cheap: no JSON parsing."""
    directory = candles_dir(root)
    if not directory.is_dir():
        return []

    stats = stats_dir(root)
    refs: list[TokenRef] = []
    for path in sorted(directory.glob("*_candles.json")):
        address = path.name[: -len("_candles.json")]
        if not address:
            continue
        refs.append(
            TokenRef(
                address=address,
                candles_path=path,
                stats_path=stats / f"{address}_stats.json",
                size_bytes=path.stat().st_size,
            )
        )
    return refs


def load_header(path: Path) -> dict[str, Any]:
    """Name / address / updated without loading candle bodies.

    The writer emits `name` and `address` first and `updated` last, with the
    entire timeframe payload in between. A small head + tail read is enough.
    """
    header: dict[str, Any] = {"name": None, "address": None, "updated": None}
    size = path.stat().st_size
    with path.open("r", encoding="utf-8") as handle:
        head = handle.read(2_048)
        if size > 4_096:
            handle.seek(max(0, size - 512))
            tail = handle.read()
        else:
            tail = head

    for key, chunk in (("name", head), ("address", head), ("updated", tail)):
        marker = f'"{key}"'
        start = chunk.find(marker)
        if start < 0:
            continue
        colon = chunk.find(":", start)
        if colon < 0:
            continue
        remainder = chunk[colon + 1 :].lstrip()
        try:
            header[key], _ = json.JSONDecoder().raw_decode(remainder)
        except json.JSONDecodeError:
            continue
    return header


def load_timeframe(
    path: Path,
    timeframe: str = "1",
    *,
    limit: Optional[int] = None,
    tail: Optional[int] = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load one timeframe's candles. Returns `(header, candles)`.

    `limit` keeps the first N; `tail` keeps the last N. Prefer `tail` for
    decision/forecast tests — the interesting window is the most recent.
    """
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"unknown timeframe {timeframe!r}; expected one of {TIMEFRAMES}")

    payload = json.loads(path.read_text(encoding="utf-8"))
    header = {
        "name": payload.get("name"),
        "address": payload.get("address"),
        "updated": payload.get("updated"),
    }
    candles = list(payload.get("timeframes", {}).get(timeframe) or [])
    if tail is not None and tail > 0:
        candles = candles[-tail:]
    if limit is not None and limit > 0:
        candles = candles[:limit]
    return header, candles


def load_stats(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not path.exists():
        return {"name": None, "address": None, "updated": None}, []
    payload = json.loads(path.read_text(encoding="utf-8"))
    header = {
        "name": payload.get("name"),
        "address": payload.get("address"),
        "updated": payload.get("updated"),
    }
    return header, list(payload.get("stats") or [])


def closes(candles: list[dict[str, Any]]) -> list[float]:
    """Close series in time order — the input TimesFM / Chronos expect."""
    return [
        float(c["close"])
        for c in candles
        if isinstance(c.get("close"), (int, float))
    ]


def ohlcv_rows(candles: list[dict[str, Any]]) -> list[dict[str, float]]:
    """Normalized OHLCV dicts with only the numeric fields forecasting needs."""
    rows: list[dict[str, float]] = []
    for candle in candles:
        try:
            rows.append(
                {
                    "timestamp": float(candle["timestamp"]),
                    "open": float(candle["open"]),
                    "high": float(candle["high"]),
                    "low": float(candle["low"]),
                    "close": float(candle["close"]),
                    "volume": float(candle.get("volume") or 0.0),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return rows


def forward_returns(
    close_series: list[float], horizons: tuple[int, ...] = (1, 5, 10, 30)
) -> dict[int, list[Optional[float]]]:
    """Percent forward returns at each horizon. Last `h` entries are None.

    Used offline to label whether a forecast would have been right — the
    calibration loop Phase 2 needs, with no live chain access.
    """
    n = len(close_series)
    out: dict[int, list[Optional[float]]] = {h: [None] * n for h in horizons}
    for i, price in enumerate(close_series):
        if price == 0:
            continue
        for horizon in horizons:
            j = i + horizon
            if j >= n:
                continue
            future = close_series[j]
            out[horizon][i] = round((future / price - 1.0) * 100.0, 6)
    return out


def iter_closes(candles: list[dict[str, Any]]) -> Iterator[float]:
    yield from closes(candles)


def summarize_candles(candles: list[dict[str, Any]]) -> dict[str, Any]:
    """Cheap numeric summary used by tests and the future MarketPacket builder."""
    if not candles:
        return {
            "count": 0,
            "first_ts": None,
            "last_ts": None,
            "open": None,
            "high": None,
            "low": None,
            "close": None,
            "volume_sum": 0.0,
            "return_pct": None,
        }

    close_series = closes(candles)
    highs = [float(c["high"]) for c in candles if isinstance(c.get("high"), (int, float))]
    lows = [float(c["low"]) for c in candles if isinstance(c.get("low"), (int, float))]
    volumes = [float(c["volume"]) for c in candles if isinstance(c.get("volume"), (int, float))]

    first_close = close_series[0] if close_series else None
    last_close = close_series[-1] if close_series else None
    return_pct = None
    if first_close and last_close and first_close != 0:
        return_pct = round((last_close / first_close - 1.0) * 100.0, 4)

    return {
        "count": len(candles),
        "first_ts": candles[0].get("timestamp"),
        "last_ts": candles[-1].get("timestamp"),
        "open": candles[0].get("open"),
        "high": max(highs) if highs else None,
        "low": min(lows) if lows else None,
        "close": last_close,
        "volume_sum": round(sum(volumes), 6) if volumes else 0.0,
        "return_pct": return_pct,
    }
