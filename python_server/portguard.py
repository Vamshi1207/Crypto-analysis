"""Reclaim the API port from a previous run before binding it.

The dev workflow restarts this server by hand, so a stale instance routinely
still owns the port and Flask dies with "Address already in use". The container
image is python:3.11-slim, which ships no `ps`, `pkill` or `fuser`, so process
discovery reads /proc directly.

Linux-only by design; on any platform without /proc this degrades to a no-op so
the server still starts normally.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from typing import Iterator

PROC = Path("/proc")
# State 0A is LISTEN in /proc/net/tcp{,6}.
_LISTEN = "0A"


def _argv(pid: int) -> list[str]:
    """Argument vector for `pid`, or [] if it is gone or unreadable."""
    try:
        raw = (PROC / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _ppid(pid: int) -> int:
    """Parent pid from /proc/<pid>/stat, or 0 when unavailable.

    The comm field is parenthesised and may itself contain spaces, so fields are
    read relative to the closing parenthesis rather than by naive splitting.
    """
    try:
        stat = (PROC / str(pid) / "stat").read_text(encoding="utf-8")
    except OSError:
        return 0
    close = stat.rfind(")")
    if close < 0:
        return 0
    fields = stat[close + 2 :].split()
    # After comm: state, ppid, ...
    if len(fields) < 2 or not fields[1].isdigit():
        return 0
    return int(fields[1])


def _ancestors() -> set[int]:
    """This process and every parent up to init.

    A wrapper shell such as `sh -c 'exec python server.py'` carries both
    "python" and "server.py" on its own command line, so it must never be
    treated as a stale instance — signalling it kills the very startup that is
    trying to reclaim the port.
    """
    chain: set[int] = set()
    pid = os.getpid()
    while pid > 0 and pid not in chain:
        chain.add(pid)
        pid = _ppid(pid)
    return chain


def _is_interpreter_running(argv: list[str], script: str) -> bool:
    """True only for a real interpreter invocation of `script`.

    Matches argv structure, not a substring of the whole line: argv[0] must be a
    python binary and some later argument must be the script itself.
    """
    if not argv:
        return False
    if not os.path.basename(argv[0]).startswith("python"):
        return False
    return any(os.path.basename(arg) == script for arg in argv[1:])


def stale_pids(script: str = "server.py") -> list[int]:
    """Other python processes running `script`, excluding self and ancestors."""
    if not PROC.is_dir():
        return []
    skip = _ancestors()
    found: list[int] = []
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in skip:
            continue
        if _is_interpreter_running(_argv(pid), script):
            found.append(pid)
    return sorted(found)


def port_is_listening(port: int) -> bool:
    """True when any socket holds `port` in LISTEN, over IPv4 or IPv6.

    Checked in addition to process discovery so a socket lingering without an
    owning process still gets waited out rather than failing the bind.
    """
    want = f"{port:04X}"
    for name in ("net/tcp", "net/tcp6"):
        try:
            lines = (PROC / name).read_text(encoding="utf-8").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 4 or parts[3] != _LISTEN:
                continue
            if parts[1].rsplit(":", 1)[-1].upper() == want:
                return True
    return False


def _alive(pids: list[int]) -> list[int]:
    live = []
    for pid in pids:
        try:
            os.kill(pid, 0)
        except OSError:
            continue
        live.append(pid)
    return live


def reclaim(
    port: int,
    *,
    script: str = "server.py",
    timeout_s: float = 8.0,
    escalate_after_s: float = 3.0,
) -> list[int]:
    """Signal stale instances and wait for `port` to clear.

    Returns the pids that were signalled. SIGTERM first so the old process can
    close its listener; SIGKILL only if it ignores that.
    """
    targets = stale_pids(script)
    if not targets:
        return []

    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    started = time.monotonic()
    escalated = False
    while time.monotonic() - started < timeout_s:
        live = _alive(targets)
        if not live and not port_is_listening(port):
            break
        if not escalated and time.monotonic() - started > escalate_after_s:
            for pid in live:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            escalated = True
        time.sleep(0.2)
    return targets
