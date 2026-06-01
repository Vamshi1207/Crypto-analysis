import json
from pathlib import Path
from datetime import datetime, timezone

# Try to use standard Docker container path, fallback to workspace directory if not present
BASE_DATA_DIR = Path("/app/ML_Training_datasets/CandleData")
if not BASE_DATA_DIR.exists():
    BASE_DATA_DIR = Path(__file__).resolve().parents[1] / "ML_Training_datasets" / "CandleData"

CANDLES_DIR = BASE_DATA_DIR / "Candles"
STATS_DIR = BASE_DATA_DIR / "Stats"

print("CANDLES_DIR:", CANDLES_DIR.resolve())
print("Exists:", CANDLES_DIR.exists())
if CANDLES_DIR.exists():
    print("Files:", list(CANDLES_DIR.glob("*"))[:5])
else:
    print("Files: []")

MAX_CANDLES = 50000


# =========================
# 🔧 Timestamp Normalizer
# =========================
def to_timestamp(val):
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    if isinstance(val, str):
        try:
            # Handle ISO format (with or without Z)
            return int(datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp())
        except Exception:
            return 0
    return 0


# =========================
# 🟢 Candle Trimming
# =========================
def trim_candles(timeframes):
    trimmed = {}
    latest_ts = None

    for tf, candles in timeframes.items():
        if not candles:
            trimmed[tf] = []
            continue

        # sort by timestamp
        candles = sorted(candles, key=lambda x: x.get("timestamp", 0))

        # Keep all candles (no MAX_CANDLES limit)
        trimmed[tf] = candles

        # track latest timestamp
        if candles:
            tf_latest = candles[-1].get("timestamp", 0)
            if latest_ts is None or tf_latest > latest_ts:
                latest_ts = tf_latest

    return trimmed, latest_ts


# =========================
# 🔵 Token Processing
# =========================
def process_token(candle_file, dry_run=True):
    if not candle_file.is_file():
        return f"SKIP (not a file): {candle_file.name}"

    address = candle_file.name.replace("_candles.json", "")
    stats_file = STATS_DIR / f"{address}_stats.json"

    if not stats_file.exists():
        return f"SKIP (no stats): {address}"

    try:
        # load candles
        with candle_file.open() as f:
            candle_data = json.load(f)

        timeframes = candle_data.get("timeframes", {})
        
        # count original candles
        orig_counts = {tf: len(c) for tf, c in timeframes.items()}

        # trim candles
        trimmed_timeframes, latest_ts = trim_candles(timeframes)

        if latest_ts is None:
            return f"SKIP (no candle data): {address}"

        # 🔥 compute earliest timestamp
        earliest_ts = min(
            c.get("timestamp", 0)
            for tf in trimmed_timeframes.values()
            for c in tf
            if c
        )

        # load stats
        with stats_file.open() as f:
            stats_data = json.load(f)

        stats = stats_data.get("stats", [])

        # 🔥 FIXED FILTER (both bounds + ms conversion)
        filtered_stats = [
            s for s in stats
            if earliest_ts <= to_timestamp(s.get("createdAt")) * 1000 <= latest_ts
        ]

        # Calculate time ranges for display in UTC
        earliest_dt = datetime.fromtimestamp(earliest_ts / 1000, timezone.utc).isoformat()
        latest_dt = datetime.fromtimestamp(latest_ts / 1000, timezone.utc).isoformat()
        
        trimmed_info = []
        for tf in timeframes.keys():
            orig = orig_counts.get(tf, 0)
            trim = len(trimmed_timeframes.get(tf, []))
            if orig != trim:
                trimmed_info.append(f"{tf}: {orig}→{trim}")
        
        trim_desc = f" (trimmed candles: {', '.join(trimmed_info)})" if trimmed_info else ""

        status_prefix = "DRY RUN - WOULD UPDATE" if dry_run else "UPDATED"

        # 🔥 WRITE ONLY AFTER SUCCESS
        if not dry_run:
            # overwrite candles
            candle_data["timeframes"] = trimmed_timeframes
            with candle_file.open("w") as f:
                json.dump(candle_data, f, indent=2)

            # overwrite stats
            stats_data["stats"] = filtered_stats
            with stats_file.open("w") as f:
                json.dump(stats_data, f, indent=2)

        return (
            f"{status_prefix} {address}:\n"
            f"  Candle range: {earliest_dt} to {latest_dt}{trim_desc}\n"
            f"  Stats count : {len(stats)} → {len(filtered_stats)} (dropped {len(stats) - len(filtered_stats)})"
        )

    except Exception as e:
        return f"ERROR {address}: {str(e)}"


# =========================
# 🚀 Main
# =========================
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Trim candle history and align stats time bounds.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually perform the updates on candle and stats files (default is dry-run)."
    )
    args = parser.parse_args()

    dry_run = not args.execute
    if dry_run:
        print("=== DRY RUN MODE: No files will be modified ===")
    else:
        print("=== EXECUTE MODE: Overwriting files ===")

    print("Processing candle files...")

    files = list(CANDLES_DIR.glob("*_candles.json"))
    print(f"Found {len(files)} candle files")

    for candle_file in files:
        print(process_token(candle_file, dry_run=dry_run))


if __name__ == "__main__":
    main()