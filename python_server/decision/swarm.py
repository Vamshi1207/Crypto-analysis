"""Continuous decide → paper-execute → mark-exit swarm loop."""

from __future__ import annotations

import threading
import time
from collections import Counter
from typing import Any, Callable, Optional

from decision import paper
from decision import pipeline_log
from decision.decide import decide
from decision.schema import DecideMode

try:
    from decision import discover as discover_mod
except Exception:  # pragma: no cover
    discover_mod = None  # type: ignore

_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_status: dict[str, Any] = {"running": False, "ticks": 0, "last_error": None, "last_tick_at": None}


def status() -> dict[str, Any]:
    with _lock:
        return dict(_status)


def start(
    get_token_data: Callable[[], dict[str, Any]],
    *,
    interval_s: float = 15.0,
    mode: str = "full",
) -> dict[str, Any]:
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return status()
        _stop.clear()
        _status.update({"running": True, "ticks": 0, "last_error": None})
        _thread = threading.Thread(
            target=_run,
            args=(get_token_data, interval_s, mode),
            name="decision-swarm",
            daemon=True,
        )
        _thread.start()
    pipeline_log.emit("swarm", "start", interval_s=interval_s, mode=mode)
    return status()


def stop() -> dict[str, Any]:
    _stop.set()
    with _lock:
        _status["running"] = False
    pipeline_log.emit("swarm", "stop")
    return status()


def _run(get_token_data: Callable[[], dict[str, Any]], interval_s: float, mode: str) -> None:
    try:
        decide_mode = DecideMode(mode)
    except ValueError:
        decide_mode = DecideMode.FULL
    while not _stop.is_set():
        started = time.perf_counter()
        with pipeline_log.run(prefix="swm-") as run_id:
            actions: Counter[str] = Counter()
            paper_status: Counter[str] = Counter()
            token_n = 0
            try:
                tokens = get_token_data() or {}
                token_n = len(tokens)
                pipeline_log.emit("swarm", "tick_start", token_n=token_n)
                for address, live_token in list(tokens.items()):
                    if _stop.is_set():
                        break
                    if str(address).startswith("0x"):
                        continue
                    # Mark exits first (open paper may sit on observe/cooled rows).
                    mark = _last_close(live_token)
                    if mark:
                        closed = paper.mark_and_maybe_exit(address=address, mark_price=mark)
                        if closed:
                            paper_status["closed"] += len(closed)
                    # Observe/cooled tokens stay on the dashboard but do not
                    # burn decide/Gate-0 budget — only the trade roster decides.
                    if live_token.get("discover") and live_token.get("tradeable") is False:
                        paper_status["observe_skip"] += 1
                        continue
                    # Prefer densest available TF (5S/15S) — memecoins move in
                    # seconds, not only on 1m Gecko bars.
                    tf = _pick_tf(live_token)
                    try:
                        card = decide(
                            address=address,
                            live_token=live_token,
                            mode=decide_mode,
                            timeframe=tf,
                            run_safety=True,
                        )
                    except Exception as exc:  # noqa: BLE001
                        with _lock:
                            _status["last_error"] = f"decide {address[:8]}: {exc}"
                        pipeline_log.emit(
                            "swarm",
                            "decide_error",
                            level="error",
                            address=address,
                            symbol=(live_token or {}).get("name"),
                            reason=str(exc),
                            timeframe=tf,
                        )
                        continue
                    action = getattr(card.action, "value", str(card.action))
                    actions[action] += 1
                    if discover_mod is not None:
                        try:
                            discover_mod.note_decision(card)
                        except Exception:  # noqa: BLE001
                            pass
                    exec_result = paper.execute_decision(card, mark_price=mark)
                    paper_status[str(exec_result.get("status") or "unknown")] += 1
                with _lock:
                    _status["ticks"] = int(_status.get("ticks") or 0) + 1
                    _status["last_tick_at"] = time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                    )
                    _status["last_error"] = None
                pipeline_log.emit(
                    "swarm",
                    "tick_done",
                    run_id=run_id,
                    duration_ms=pipeline_log.timed_ms(started),
                    token_n=token_n,
                    actions=dict(actions),
                    paper=dict(paper_status),
                    tick=_status.get("ticks"),
                )
            except Exception as exc:  # noqa: BLE001
                with _lock:
                    _status["last_error"] = str(exc)
                pipeline_log.emit(
                    "swarm",
                    "tick_error",
                    level="error",
                    reason=str(exc),
                    duration_ms=pipeline_log.timed_ms(started),
                )
        _stop.wait(interval_s)
    with _lock:
        _status["running"] = False


def _last_close(live_token: dict[str, Any]) -> Optional[float]:
    tfs = live_token.get("timeframes") or {}
    for key in ("5S", "15S", "30S", "1", "3", "5"):
        rows = tfs.get(key) or []
        if rows:
            try:
                return float(rows[-1].get("close"))
            except (TypeError, ValueError, AttributeError):
                continue
    return None


def _pick_tf(live_token: dict[str, Any]) -> str:
    """Densest TF with enough bars for Gate 1 (~16). Falls back to 1m."""
    from decision.packet import MIN_BARS_BY_TF, SCALP_TF_PRIORITY

    tfs = live_token.get("timeframes") or {}
    for key in SCALP_TF_PRIORITY:
        rows = tfs.get(key) or []
        need = int(MIN_BARS_BY_TF.get(key, 16))
        if len(rows) >= need:
            return key
    return "1"
