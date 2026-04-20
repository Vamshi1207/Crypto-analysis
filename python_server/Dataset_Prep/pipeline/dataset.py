import pandas as pd
from tqdm import tqdm
from indicators import IncrementalIndicatorEngine
from Dataset_Prep.pipeline.monitor import get_memory

def add_common_features(base_flattened, first_open, first_high, first_low, first_close, volume_sum, window_len, high_window, low_window, current_close, ts_i):
    base_flattened["volume_sum"] = volume_sum
    base_flattened["volume_avg"] = volume_sum / window_len if window_len else 0.0
    base_flattened["range_pct"] = (high_window.max() - low_window.min()) / current_close * 100

    base_flattened["open_rel"] = first_open / current_close
    base_flattened["high_rel"] = first_high / current_close
    base_flattened["low_rel"] = first_low / current_close
    base_flattened["close_rel"] = first_close / current_close

    base_flattened["hour"] = ts_i.hour
    base_flattened["minute"] = ts_i.minute
    base_flattened["weekday"] = ts_i.weekday()
    base_flattened["timestamp"] = ts_i


def create_forward_label_dataset_stream(
    df,
    token_id,
    window_size,
    step_size,
    up_threshold,
    down_threshold,
    stable_threshold,
    timeframe,
    history_bars=300,
    target_samples=20,
    prediction_horizons=(10, 20, 30),
    progress_callback=None,
    progress_every=100,
    show_progress=True,
):
    opens = df["open"].to_numpy(copy=False)
    highs = df["high"].to_numpy(copy=False)
    lows = df["low"].to_numpy(copy=False)
    closes = df["close"].to_numpy(copy=False)
    volumes = df["volume"].to_numpy(copy=False)
    timestamps = df["timestamp"].to_numpy(copy=False)
    max_horizon = max(prediction_horizons)

    start = history_bars
    end = len(df) - max_horizon

    short_id = token_id[:6]
    indicator_engine = IncrementalIndicatorEngine()

    preload_end = min(start, len(df))
    for i in range(preload_end):
        indicator_engine.update(
            {
                "open": opens[i],
                "high": highs[i],
                "low": lows[i],
                "close": closes[i],
                "volume": volumes[i],
            }
        )

    if start < end:
        initial_window_start = max(0, start - history_bars + 1)
        volume_sum = float(volumes[initial_window_start:start + 1].sum())
    else:
        volume_sum = 0.0

    progress_iter = tqdm(
        total=end - start,
        desc=f"[{short_id}]",
        position=0,
        leave=True,
        disable=not show_progress,
    )
    with progress_iter as pbar:
        for i in range(start, end):
            current_close = closes[i]
            ts_i = pd.Timestamp(timestamps[i])
            indicator_engine.update(
                {
                    "open": opens[i],
                    "high": highs[i],
                    "low": lows[i],
                    "close": closes[i],
                    "volume": volumes[i],
                }
            )

            window_start = max(0, i - history_bars + 1)
            if i > start:
                volume_sum += float(volumes[i]) - float(volumes[window_start - 1])

            high_window = highs[window_start:i + 1]
            low_window = lows[window_start:i + 1]
            window_len = i - window_start + 1

            base = indicator_engine.snapshot()

            add_common_features(
                base,
                opens[window_start],
                highs[window_start],
                lows[window_start],
                closes[window_start],
                volume_sum,
                window_len,
                high_window,
                low_window,
                current_close,
                ts_i,
            )

            row = base.copy()
            row["current_close"] = current_close

            for h in prediction_horizons:
                future = closes[i + h]
                row[f"future_close_{h}"] = future
                row[f"future_return_pct_{h}"] = ((future - current_close) / current_close) * 100

            yield row  # 🔥 KEY CHANGE

            pbar.update(1)
            if progress_callback and (pbar.n % progress_every == 0 or pbar.n == (end - start)):
                progress_callback(pbar.n, end - start)
            if pbar.n % 100 == 0:
                mem = get_memory()
                pbar.set_postfix({
                    "mem": f"{mem:.2f}G",
                    "row": pbar.n
                })
