"""Statistical evaluation for the parallel lanes — the honesty layer.

The whole point of the labs is to answer one question with evidence: does a
frozen lane config have a positive expectancy *out of sample*? This module
computes the numbers and the gate that answers it, so a lane can no longer be
"promoted" on in-sample realized PnL alone (selection bias).

Only stdlib — no scipy. The t-statistic p-value uses a normal approximation,
accurate enough for n ≥ ~30 (the default floor).

A result is only significant when:
  * ``n >= min_n`` — enough closed trades to believe the mean
  * ``mean > 0`` — positive expectancy
  * ``t_stat >= z_alpha`` — the mean is statistically distinguishable from zero
"""

from __future__ import annotations

import math
from typing import Any, Optional

MIN_N_DEFAULT = 30
ALPHA_DEFAULT = 0.05


def _z_from_alpha(alpha: float) -> float:
    """One-sided normal critical value for significance level ``alpha``."""
    # z such that P(Z > z) = alpha  →  z = sqrt(2) * erfcinv(2*alpha).
    # math.erfc is available on 3.11+; guard with a small Newton fallback.
    if alpha <= 0 or alpha >= 1:
        raise ValueError("alpha must be in (0, 1)")
    try:
        inv_erfc = _erfcinv(2.0 * alpha)
        return math.sqrt(2.0) * inv_erfc
    except Exception:  # noqa: BLE001
        # Coarse but safe fallback for a few common alphas.
        return {0.05: 1.645, 0.01: 2.326, 0.10: 1.282}.get(round(alpha, 3), 1.96)


def _erfcinv(y: float) -> float:
    """Inverse complementary error function (Newton), accurate to ~1e-6."""
    if y >= 2.0:
        return float("-inf")
    if y <= 0.0:
        return float("inf")
    # Initial guess (Winitzki's approximation).
    a = 0.147
    ln = math.log(2.0 / y)
    x = math.sqrt(max(0.0, -2.0 / (math.pi * a) - ln / 2.0 + math.sqrt((-2.0 / (math.pi * a) - ln / 2.0) ** 2 - (1.0 / a) * ln)))
    # Newton on erfc(x) - y = 0.
    for _ in range(8):
        err = math.erfc(x) - y
        if abs(err) < 1e-12:
            break
        x = x - err / (-2.0 / math.sqrt(math.pi) * math.exp(-x * x))
    return x


def eval_trades(pnls: list[float]) -> dict[str, Any]:
    """Return (n, win_rate, expectancy, stddev, CI, t-stat, p, max drawdown)."""
    xs = [float(p) for p in pnls]
    n = len(xs)
    if n == 0:
        return {
            "n": 0,
            "win_rate": None,
            "expectancy_usd": None,
            "stddev_usd": None,
            "sem_usd": None,
            "ci95_low_usd": None,
            "ci95_high_usd": None,
            "t_stat": None,
            "p_value": None,
            "max_drawdown_usd": 0.0,
            "total_pnl_usd": 0.0,
        }
    total = sum(xs)
    mean = total / n
    wins = sum(1 for x in xs if x > 0)
    losses = sum(1 for x in xs if x < 0)
    win_rate = wins / n
    var = sum((x - mean) ** 2 for x in xs) / (n - 1) if n > 1 else 0.0
    std = math.sqrt(var)
    sem = std / math.sqrt(n) if n > 0 else 0.0
    # Normal-approximation 95% CI of the mean.
    z95 = 1.96
    ci_low = mean - z95 * sem
    ci_high = mean + z95 * sem
    if sem > 0:
        t_stat = mean / sem
        p_value = _normal_p(t_stat)
    else:
        # Zero variance: mean is perfectly stable. Sign decides significance.
        t_stat = float("inf") if mean > 0 else (float("-inf") if mean < 0 else 0.0)
        p_value = 0.0 if mean != 0 else 1.0
    # Max drawdown from the cumulative equity curve, in dollars.
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for x in xs:
        cum += x
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    t_display = 999.0 if math.isinf(t_stat) else round(t_stat, 4)
    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4),
        "expectancy_usd": round(mean, 4),
        "stddev_usd": round(std, 4),
        "sem_usd": round(sem, 4),
        "ci95_low_usd": round(ci_low, 4),
        "ci95_high_usd": round(ci_high, 4),
        "t_stat": t_display,
        "p_value": round(p_value, 6),
        "max_drawdown_usd": round(max_dd, 4),
        "total_pnl_usd": round(total, 4),
    }


def _normal_p(t: float) -> float:
    """Two-sided p-value via the standard normal CDF (normal approximation)."""
    z = abs(t)
    p_one = 0.5 * math.erfc(z / math.sqrt(2.0))
    return 2.0 * p_one


def significant(
    stats: dict[str, Any],
    *,
    min_n: int = MIN_N_DEFAULT,
    alpha: float = ALPHA_DEFAULT,
) -> tuple[bool, str]:
    """Apply the promotion gate to a stats dict. Returns (pass, reason)."""
    n = int(stats.get("n") or 0)
    mean = stats.get("expectancy_usd")
    t_stat = stats.get("t_stat")
    if n < min_n:
        return False, f"n={n} < min_n={min_n}"
    if mean is None or t_stat is None:
        return False, "no data"
    if mean <= 0:
        return False, f"expectancy ${mean:.3f} ≤ 0"
    z_crit = _z_from_alpha(alpha)
    if t_stat < z_crit:
        return False, f"t={t_stat:.2f} < z_crit={z_crit:.2f} (α={alpha})"
    return True, f"significant: n={n} t={t_stat:.2f} mean=${mean:.3f}"


def evaluate_lane(
    closes: list[dict[str, Any]], *, min_n: int = MIN_N_DEFAULT, alpha: float = ALPHA_DEFAULT
) -> dict[str, Any]:
    """Eval a lane from a chronological list of closed-trade records.

    ``closes`` must be time-ordered (oldest first) so max drawdown is real.
    Expects each record to carry ``realized_pnl_usd``.
    """
    pnls: list[float] = []
    for c in closes:
        p = c.get("realized_pnl_usd")
        if p is None:
            continue
        try:
            pnls.append(float(p))
        except (TypeError, ValueError):
            continue
    stats = eval_trades(pnls)
    passed, reason = significant(stats, min_n=min_n, alpha=alpha)
    return {"stats": stats, "significant": passed, "reason": reason}


def configs_agree(hashes: list[str]) -> bool:
    """True when every result carries the same config hash (or no hashes)."""
    seen = {h for h in hashes if h}
    return len(seen) <= 1
