"""Refit conformal residuals from live paper/decision outcomes.

    python -m tools.fit_live_calibration
    python -m tools.fit_live_calibration --days 3 --no-merge --force
"""

from __future__ import annotations

import argparse
import json

from decision import calibrate


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7, help="how many UTC days of outcomes to scan")
    ap.add_argument(
        "--min-errors",
        type=int,
        default=30,
        help="need this many live residuals before replacing a rich corpus fit",
    )
    ap.add_argument(
        "--no-merge",
        action="store_true",
        help="use only live residuals (drop corpus floor)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="fit even when live_n < min-errors (still merges unless --no-merge)",
    )
    args = ap.parse_args(argv)

    min_errors = 1 if args.force else args.min_errors
    result = calibrate.fit_from_outcomes(
        days=args.days,
        min_errors=min_errors,
        merge_with_existing=not args.no_merge,
    )
    print(json.dumps(result, indent=2))
    return 0 if result.get("status") in ("fitted", "insufficient") else 1


if __name__ == "__main__":
    raise SystemExit(main())
