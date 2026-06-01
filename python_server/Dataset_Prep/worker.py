import sys
import gc
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Dataset_Prep.pipeline.loader import load_token_frame
from Dataset_Prep.pipeline.dataset import create_forward_label_dataset_stream
from Dataset_Prep.pipeline.core import list_to_rows
from Dataset_Prep.pipeline.utils import move_processed_files

from Dataset_Prep.config import (
    output_dir,
    chunk_size,
    max_flush_chunk_size,
    window_size,
    step_size,
    up_threshold,
    down_threshold,
    stable_threshold,
    timeframe_minutes,
    target_samples,
    timeframe_key,
    history_bars,
    prediction_horizons,
    token_log_dir,
    use_stats,
)

import psutil
import os
import pandas as pd

from Dataset_Prep.progress import update_token_status, utc_now_iso


def get_memory():
    return psutil.Process(os.getpid()).memory_info().rss / (1024 ** 3)


def process_token(file_number, token_id):
    df_final = None
    token_name = None
    total_rows = 0
    progress_dir = os.getenv("DATASET_PREP_PROGRESS_DIR")

    short_id = token_id[:6]

    def publish(status, message, **extra):
        if not progress_dir:
            return
        payload = {
            "file_number": file_number,
            "token_id": token_id,
            "token_name": token_name,
            "status": status,
            "message": message,
            "rows_written": total_rows,
            "memory_gb": get_memory(),
            "updated_at": utc_now_iso(),
        }
        payload.update(extra)
        update_token_status(progress_dir, token_id, payload)

    log_path = token_log_dir / f"{token_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def write_log(message):
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"{utc_now_iso()} {message}\n")

    publish("running", "Loading token")
    write_log("Starting token processing")

    try:
        # --- Load data ---
        token_name, df_final, diagnostics = load_token_frame(token_id, timeframe_key, use_stats)
        write_log(
            f"Loaded token data name={token_name} "
            f"raw_candles={diagnostics['raw_candle_rows']} invalid_candles={diagnostics['invalid_candle_rows']} "
            f"raw_stats={diagnostics['raw_stats_rows']} invalid_stats={diagnostics['invalid_stats_rows']} "
            f"merged_before_dropna={diagnostics['merged_rows_before_dropna']} "
            f"merged_after_dropna={diagnostics['merged_rows_after_dropna']}"
        )
        publish("running", "Loaded token data")

        if df_final is None or df_final.empty:
            write_log("Empty input dataframe after loading")
            move_processed_files(token_id)
            publish("empty", "Empty input dataframe", processed_steps=0, total_steps=0)
            return

        out_path = output_dir / f"memecoin_training_dataset_{file_number}.csv"

        flush_chunk_size = min(chunk_size, max_flush_chunk_size)
        buffer = []
        header_written = False

        total_steps = max(len(df_final) - max(prediction_horizons) - history_bars, 0)
        publish("running", "Generating dataset rows", processed_steps=0, total_steps=total_steps)

        def handle_progress(processed_steps, total_progress_steps):
            publish(
                "running",
                "Generating dataset rows",
                processed_steps=processed_steps,
                total_steps=total_progress_steps,
            )

        # --- STREAM processing ---
        for row in create_forward_label_dataset_stream(
            df=df_final,
            token_id=token_id,
            window_size=window_size,
            step_size=step_size,
            up_threshold=up_threshold,
            down_threshold=down_threshold,
            stable_threshold=stable_threshold,
            timeframe=timeframe_minutes,
            history_bars=history_bars,
            target_samples=target_samples,
            prediction_horizons=prediction_horizons,
            progress_callback=handle_progress,
            show_progress=False,
        ):
            buffer.append(row)

            # --- Flush chunk ---
            if len(buffer) >= flush_chunk_size:
                chunk_df = pd.DataFrame(buffer)

                chunk_df = list_to_rows(chunk_df, sequence_mode="latest")
                chunk_df = chunk_df.dropna(how="any").reset_index(drop=True)

                if not chunk_df.empty:
                    chunk_df["token_id"] = token_id
                    chunk_df["token_name"] = token_name

                    chunk_df.to_csv(
                        out_path,
                        mode="a",
                        header=not header_written,
                        index=False
                    )

                    total_rows += len(chunk_df)
                    header_written = True
                    write_log(f"Flushed chunk rows={len(chunk_df)} total_rows={total_rows}")
                    publish("running", "Writing chunk to CSV", rows_written=total_rows, total_steps=total_steps)

                buffer.clear()
                del chunk_df

        # --- Final flush ---
        if buffer:
            chunk_df = pd.DataFrame(buffer)

            chunk_df = list_to_rows(chunk_df, sequence_mode="latest")
            chunk_df = chunk_df.dropna(how="any").reset_index(drop=True)

            if not chunk_df.empty:
                chunk_df["token_id"] = token_id
                chunk_df["token_name"] = token_name

                chunk_df.to_csv(
                    out_path,
                    mode="a",
                    header=not header_written,
                    index=False
                )

                total_rows += len(chunk_df)
                write_log(f"Flushed final chunk rows={len(chunk_df)} total_rows={total_rows}")
                publish("running", "Writing final chunk to CSV", rows_written=total_rows, total_steps=total_steps)

            buffer.clear()

        # --- Handle empty output ---
        if total_rows == 0:
            write_log("No dataset rows written after processing")
            move_processed_files(token_id)
            publish("empty", "Empty dataset after processing", processed_steps=total_steps, total_steps=total_steps)
            return

        move_processed_files(token_id)
        write_log(f"Completed token processing rows_written={total_rows}")
        publish(
            "done",
            "Completed",
            processed_steps=total_steps,
            total_steps=total_steps,
            rows_written=total_rows,
        )

    except Exception as e:
        import traceback

        write_log(f"ERROR {type(e).__name__}: {str(e)}")
        write_log(traceback.format_exc())
        publish("error", str(e))
        print(f"[{short_id}] ERROR → {str(e)}")

    finally:
        del df_final
        gc.collect()


if __name__ == "__main__":
    file_number = int(sys.argv[1])
    token_id = sys.argv[2]

    process_token(file_number, token_id)
