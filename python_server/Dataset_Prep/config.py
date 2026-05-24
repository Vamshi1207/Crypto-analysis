from pathlib import Path

base = Path("/app/")
if not base.exists():
    base = Path(__file__).resolve().parent.parent
timeframe_key = "5S"

output_dir = base / "ML_Training_datasets" / "Datasets" / timeframe_key
candles_dir = base / "ML_Training_datasets" / "CandleData" / "Candles"
stats_dir = base / "ML_Training_datasets" / "CandleData" / "Stats"
completed_candles_dir = candles_dir / "completed"
completed_stats_dir = stats_dir / "completed"
tmp_output_dir = output_dir / "tmp_chunks"
token_log_dir = output_dir / "token_logs"

output_dir.mkdir(parents=True, exist_ok=True)
tmp_output_dir.mkdir(parents=True, exist_ok=True)
completed_candles_dir.mkdir(parents=True, exist_ok=True)
completed_stats_dir.mkdir(parents=True, exist_ok=True)
token_log_dir.mkdir(parents=True, exist_ok=True)

history_bars = 5000
chunk_size = history_bars
max_workers = 8
dashboard_host = "0.0.0.0"
dashboard_port = 8765
prediction_horizons = (10, 20, 30, 40, 50)

use_stats = False  # Disabled: stats timestamps (May 2-3) don't align with candle timestamps (April 30)
row_limit = 50000
move_input_files = False

SUM_COLS = ["buyCount", "sellCount", "buyVolumeSol", "sellVolumeSol"]
MEAN_COLS = ["priceSol"]
WINDOW = 5

# Dataset generation parameters
window_size = 400
step_size = 50
up_threshold = 20.0
down_threshold = 15.0
stable_threshold = 10.0
timeframe_minutes = 5  # for time_to_extreme calculation
target_samples = 100

# Worker parameters
max_flush_chunk_size = 512

# Merge parameters
merge_direction = "backward"
