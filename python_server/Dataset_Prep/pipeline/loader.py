import json
import pandas as pd
from Dataset_Prep.config import candles_dir, stats_dir, SUM_COLS, MEAN_COLS, WINDOW, merge_direction, use_stats, row_limit

PRICE_COLS = ["open", "high", "low", "close", "volume"]
STAT_SOURCE_COLS = ["createdAt", *SUM_COLS, *MEAN_COLS]


def _load_candle_frame(candles, token_id, timeframe_key):
    if not candles:
        raise ValueError(f"No {timeframe_key} candles found for {token_id}")

    df = pd.DataFrame(candles)
    missing_candle_cols = [col for col in ["timestamp", *PRICE_COLS] if col not in df.columns]
    if missing_candle_cols:
        raise ValueError(f"Candles data missing columns: {missing_candle_cols}")
    df = df[["timestamp", *PRICE_COLS]]

    df["low"] = df[["low", "open", "close"]].min(axis=1)
    df["high"] = df[["high", "open", "close"]].max(axis=1)

    if row_limit and len(df) > row_limit:
        df = df.tail(row_limit).reset_index(drop=True)

    df["timestamp"] = pd.to_datetime(
        df["timestamp"],
        unit="ms",
        utc=True
    ).astype("datetime64[ns, UTC]")

    for col in PRICE_COLS:
        df[col] = pd.to_numeric(df[col], errors="coerce", downcast="float")

    df = df.sort_values("timestamp").reset_index(drop=True)

    raw_candle_rows = len(df)
    invalid_candles = (
        (df["open"] <= 0) | (df["high"] <= 0) | (df["low"] <= 0) | (df["close"] <= 0) | (df["volume"] < 0) |
        (df["low"] > df["high"])
    )
    invalid_candle_rows = int(invalid_candles.sum())
    if invalid_candle_rows:
        df = df[~invalid_candles]
        if df.empty:
            raise ValueError(f"All candle data invalid for {token_id}")

    return df, raw_candle_rows, invalid_candle_rows


def load_token_frame(token_id, timeframe_key="5S", use_stats_flag=use_stats):
    # --- Load data ---
    with (candles_dir / f"{token_id}_candles.json").open() as f:
        candle_data = json.load(f)

    token_name = candle_data.get("name", token_id)
    candles = candle_data.get("timeframes", {}).get(timeframe_key, [])

    if not use_stats_flag:
        df, raw_candle_rows, invalid_candle_rows = _load_candle_frame(candles, token_id, timeframe_key)
        return token_name, df, {
            "raw_candle_rows": raw_candle_rows,
            "invalid_candle_rows": invalid_candle_rows,
            "clean_candle_rows": len(df),
            "raw_stats_rows": 0,
            "invalid_stats_rows": 0,
            "clean_stats_rows": 0,
            "merged_rows_before_dropna": len(df),
            "merged_rows_after_dropna": len(df),
            "dropped_rows_missing_stats": 0,
            "stats_disabled": True,
        }

    with (stats_dir / f"{token_id}_stats.json").open() as f:
        stats_data = json.load(f)

    df, raw_candle_rows, invalid_candle_rows = _load_candle_frame(candles, token_id, timeframe_key)

    # --- Stats dataframe ---
    stats_records = stats_data.get("stats", [])
    if not stats_records:
        raise ValueError(f"Stats data is empty for {token_id}")

    stats_df = pd.DataFrame(stats_records)
    missing_stats_cols = [col for col in STAT_SOURCE_COLS if col not in stats_df.columns]
    if missing_stats_cols:
        raise ValueError(f"Stats data missing columns: {missing_stats_cols}")
    stats_df = stats_df[STAT_SOURCE_COLS]

    stats_df["timestamp"] = pd.to_datetime(
        stats_df["createdAt"],
        utc=True
    ).astype("datetime64[ns, UTC]")   # ✅ force same dtype

    for col in SUM_COLS + MEAN_COLS:
        stats_df[col] = pd.to_numeric(stats_df[col], errors="coerce", downcast="float")

    stats_df = stats_df.sort_values("timestamp").reset_index(drop=True)

    raw_stats_rows = len(stats_df)
    invalid_stats = (
        (stats_df["buyCount"] < 0) | (stats_df["sellCount"] < 0) |
        (stats_df["buyVolumeSol"] < 0) | (stats_df["sellVolumeSol"] < 0) | (stats_df["priceSol"] <= 0)
    )
    invalid_stats_rows = int(invalid_stats.sum())
    if invalid_stats_rows:
        stats_df = stats_df[~invalid_stats]
        if stats_df.empty:
            raise ValueError(f"All stats data invalid for {token_id}")

    # --- Rolling aggregations ---
    stats_df[SUM_COLS] = stats_df[SUM_COLS].rolling(WINDOW, min_periods=1).sum()
    stats_df[MEAN_COLS] = stats_df[MEAN_COLS].rolling(WINDOW, min_periods=1).mean()

    # --- Rename columns ---
    stats_df = stats_df.rename(
        columns={c: f"last_5m_{c}" for c in SUM_COLS + MEAN_COLS}
    )

    # --- Ensure merge safety ---
    if df["timestamp"].dtype != stats_df["timestamp"].dtype:
        raise ValueError(
            f"Timestamp dtype mismatch: {df['timestamp'].dtype} vs {stats_df['timestamp'].dtype}"
        )

    # --- Merge ---
    merged = pd.merge_asof(
        df,
        stats_df[["timestamp", *[f"last_5m_{c}" for c in SUM_COLS + MEAN_COLS]]],
        on="timestamp",
        direction=merge_direction
    )

    raw_merged_rows = len(merged)
    stats_cols = [f"last_5m_{c}" for c in SUM_COLS + MEAN_COLS]
    merged = merged.dropna(subset=stats_cols)
    dropped_missing_stats = raw_merged_rows - len(merged)

    if merged.empty:
        raise ValueError(f"No valid data after merging candles and stats for {token_id}")

    diagnostics = {
        "raw_candle_rows": raw_candle_rows,
        "invalid_candle_rows": invalid_candle_rows,
        "clean_candle_rows": len(df),
        "raw_stats_rows": raw_stats_rows,
        "invalid_stats_rows": invalid_stats_rows,
        "clean_stats_rows": len(stats_df),
        "merged_rows_before_dropna": raw_merged_rows,
        "merged_rows_after_dropna": len(merged),
        "dropped_rows_missing_stats": dropped_missing_stats,
    }

    return token_name, merged, diagnostics
