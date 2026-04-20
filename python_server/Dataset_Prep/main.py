import subprocess
import sys
import os
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Dataset_Prep.config import (
    candles_dir,
    stats_dir,
    tmp_output_dir,
    max_workers,
    dashboard_host,
    dashboard_port,
)
from Dataset_Prep.progress import (
    reset_run_dir,
    utc_now_iso,
    write_overview,
)
from tqdm import tqdm
from concurrent.futures import ProcessPoolExecutor, as_completed

package_root = Path(__file__).resolve().parent.parent
progress_dir = tmp_output_dir / "progress_ui"
dashboard_process = None

def get_token_ids():
    candle_tokens = {
        path.name.replace("_candles.json", "")
        for path in candles_dir.glob("*_candles.json")
    }
    stats_tokens = {
        path.name.replace("_stats.json", "")
        for path in stats_dir.glob("*_stats.json")
    }
    return sorted(candle_tokens & stats_tokens)


def run_token(job):
    file_number, token_id = job
    child_env = os.environ.copy()
    child_env["DATASET_PREP_PROGRESS_DIR"] = str(progress_dir)

    subprocess.run(
        [sys.executable, "-u", "-m", "Dataset_Prep.worker", str(file_number), token_id],
        text=True,
        check=False,
        cwd=package_root,
        env=child_env,
    )

    return token_id


if __name__ == "__main__":
    token_ids = get_token_ids()
    started_at = utc_now_iso()
    print(f"Found {len(token_ids)} tokens")
    reset_run_dir(progress_dir)
    write_overview(
        progress_dir,
        {
            "status": "running",
            "total_tokens": len(token_ids),
            "started_at": started_at,
        },
    )

    dashboard_env = os.environ.copy()
    dashboard_process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-m",
            "Dataset_Prep.progress_dashboard",
            str(progress_dir),
            dashboard_host,
            str(dashboard_port),
        ],
        cwd=package_root,
        env=dashboard_env,
    )
    print(f"Dashboard: http://{dashboard_host}:{dashboard_port}")

    jobs = list(enumerate(token_ids, 1))

    try:
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(run_token, job) for job in jobs]

            for _ in tqdm(as_completed(futures), total=len(futures), desc="Tokens"):
                pass
    finally:
        write_overview(
            progress_dir,
            {
                "status": "completed",
                "total_tokens": len(token_ids),
                "started_at": started_at,
                "completed_at": utc_now_iso(),
            },
        )
        if dashboard_process is not None:
            dashboard_process.terminate()
            try:
                dashboard_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                dashboard_process.kill()
