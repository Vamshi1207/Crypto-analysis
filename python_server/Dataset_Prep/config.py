from pathlib import Path

base = Path("/app/")

candles_dir = base / "ML_Training_datasets" / "CandleData" / "Candles"
stats_dir = base / "ML_Training_datasets" / "CandleData" / "Stats"
completed_candles_dir = candles_dir / "completed"
completed_stats_dir = stats_dir / "completed"
tmp_output_dir = output_dir / "tmp_chunks"

output_dir.mkdir(parents=True, exist_ok=True)
tmp_output_dir.mkdir(parents=True, exist_ok=True)
completed_candles_dir.mkdir(parents=True, exist_ok=True)
completed_stats_dir.mkdir(parents=True, exist_ok=True)

history_bars = 5000
chunk_size = history_bars
max_workers = 8
dashboard_host = "0.0.0.0"
dashboard_port = 8765
prediction_horizons = (10, 20, 30, 40, 50)
timeframe_key = "15S"

output_dir = base / "ML_Training_datasets" / "Datasets" / timeframe_key

SUM_COLS = ["buyCount", "sellCount", "buyVolumeSol", "sellVolumeSol"]
MEAN_COLS = ["priceSol"]
WINDOW = 5
