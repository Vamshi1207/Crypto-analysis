import json
import pandas as pd
from Dataset_Prep.config import candles_dir, stats_dir, SUM_COLS, MEAN_COLS, WINDOW

PRICE_COLS = ["open", "high", "low", "close", "volume"]
STAT_SOURCE_COLS = ["createdAt", *SUM_COLS, *MEAN_COLS]


def load_token_frame(token_id, timeframe_key="5S"):
    # --- Load data ---
    with (candles_dir / f"{token_id}_candles.json").open() as f:
        candle_data = json.load(f)

    with (stats_dir / f"{token_id}_stats.json").open() as f:
        stats_data = json.load(f)

    token_name = candle_data.get("name", token_id)
    candles = candle_data.get("timeframes", {}).get(timeframe_key, [])
    if not candles:
        raise ValueError(f"No {timeframe_key} candles found for {token_id}")

    # --- Candles dataframe ---
    df = pd.DataFrame(candles)
    missing_candle_cols = [col for col in ["timestamp", *PRICE_COLS] if col not in df.columns]
    if missing_candle_cols:
        raise ValueError(f"Candles data missing columns: {missing_candle_cols}")
    df = df[["timestamp", *PRICE_COLS]]

    df["timestamp"] = pd.to_datetime(
        df["timestamp"],
        unit="ms",
        utc=True
    ).astype("datetime64[ns, UTC]")   # ✅ force consistent dtype

    for col in PRICE_COLS:
        df[col] = pd.to_numeric(df[col], errors="coerce", downcast="float")

    df = df.sort_values("timestamp").reset_index(drop=True)

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
        direction="backward"
    )

    return token_name, merged
