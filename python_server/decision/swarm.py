"""Continuous decide → paper-execute → mark-exit swarm loop."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from decision.decide import decide
from decision import paper
from decision.schema import DecideMode

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
    return status()


def stop() -> dict[str, Any]:
    _stop.set()
    with _lock:
        _status["running"] = False
    return status()


def _run(get_token_data: Callable[[], dict[str, Any]], interval_s: float, mode: str) -> None:
    try:
        decide_mode = DecideMode(mode)
    except ValueError:
        decide_mode = DecideMode.FULL
    while not _stop.is_set():
        try:
            tokens = get_token_data() or {}
            for address, live_token in list(tokens.items()):
                if _stop.is_set():
                    break
                if str(address).startswith("0x"):
                    continue
                # Mark exits first.
                mark = _last_close(live_token)
                if mark:
                    paper.mark_and_maybe_exit(address=address, mark_price=mark)
                try:
                    card = decide(
                        address=address,
                        live_token=live_token,
                        mode=decide_mode,
                        timeframe="1",
                        run_safety=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    with _lock:
                        _status["last_error"] = f"decide {address[:8]}: {exc}"
                    continue
                paper.execute_decision(card, mark_price=mark)
            with _lock:
                _status["ticks"] = int(_status.get("ticks") or 0) + 1
                _status["last_tick_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                _status["last_error"] = None
        except Exception as exc:  # noqa: BLE001
            with _lock:
                _status["last_error"] = str(exc)
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
