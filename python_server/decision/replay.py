"""Helpers to replay contiguous corpus OHLCV as if it were a live token buffer."""

from __future__ import annotations

from typing import Any, Optional

from decision import dataset


def longest_continuous_run(
    candles: list[dict[str, Any]],
    expected_ms: float,
    *,
    tol: tuple[float, float] = (0.5, 2.0),
) -> tuple[int, int, list[dict[str, Any]]]:
    """Return (start_idx, end_idx, slice) for the longest near-regular bar run."""
    if len(candles) < 2:
        return 0, len(candles), list(candles)

    ts = [c["timestamp"] for c in candles]
    gaps = [ts[i] - ts[i - 1] for i in range(1, len(ts))]
    lo, hi = expected_ms * tol[0], expected_ms * tol[1]

    best = (0, 0, 0)  # length, start, end
    start = 0
    for i, gap in enumerate(gaps, start=1):
        if not (lo <= gap <= hi):
            length = i - start
            if length > best[0]:
                best = (length, start, i)
            start = i
    length = len(ts) - start
    if length > best[0]:
        best = (length, start, len(ts))

    _, a, b = best
    return a, b, candles[a:b]


EXPECTED_MS = {
    "5S": 5_000,
    "15S": 15_000,
    "30S": 30_000,
    "1": 60_000,
    "3": 180_000,
    "5": 300_000,
    "15": 900_000,
    "60": 3_600_000,
}


def pick_continuous_live_token(
    *,
    min_bars: int = 128,
    tail: int = 512,
    prefer_tfs: tuple[str, ...] = ("15S", "5S", "30S", "1"),
    scan_limit: int = 24,
) -> dict[str, Any]:
    """Scan the corpus for the densest continuous run and shape it like token_data.

    Returns a dict with address/name/timeframe/bars and a ``live_token`` payload
    suitable for ``decide(live_token=...)``.
    """
    tokens = sorted(dataset.list_tokens(), key=lambda t: t.size_bytes)[:scan_limit]
    best: Optional[tuple[int, dict[str, Any]]] = None

    for token in tokens:
        for tf in prefer_tfs:
            expected = EXPECTED_MS.get(tf)
            if expected is None:
                continue
            try:
                header, candles = dataset.load_timeframe(
                    token.candles_path, tf, tail=max(tail * 4, 2000)
                )
            except Exception:
                continue
            _, _, window = longest_continuous_run(candles, expected)
            if len(window) < min_bars:
                continue
            score = len(window)
            if best is None or score > best[0]:
                live = window[-tail:] if len(window) > tail else window
                best = (
                    score,
                    {
                        "address": token.address,
                        "name": header.get("name"),
                        "timeframe": tf,
                        "continuous_bars": len(window),
                        "live_bars": len(live),
                        "first_ts": live[0]["timestamp"],
                        "last_ts": live[-1]["timestamp"],
                        "live_token": {
                            "name": header.get("name"),
                            "timeframes": {tf: live},
                        },
                    },
                )

    if best is None:
        raise RuntimeError(
            f"no continuous OHLCV run with ≥{min_bars} bars in the first {scan_limit} tokens"
        )
    return best[1]
