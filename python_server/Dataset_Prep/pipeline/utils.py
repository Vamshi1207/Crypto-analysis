import shutil
from Dataset_Prep.config import candles_dir, stats_dir, completed_candles_dir, completed_stats_dir, move_input_files


def move_processed_files(token_id):
    if not move_input_files:
        return

    c = candles_dir / f"{token_id}_candles.json"
    s = stats_dir / f"{token_id}_stats.json"

    if c.exists():
        shutil.move(str(c), str(completed_candles_dir / c.name))

    if s.exists():
        shutil.move(str(s), str(completed_stats_dir / s.name))
