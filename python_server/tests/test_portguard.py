"""Port reclaim must find a stale instance without ever targeting itself."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

import pytest

import portguard

pytestmark = pytest.mark.skipif(
    not portguard.PROC.is_dir(), reason="needs /proc (Linux)"
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_port_is_listening_detects_an_open_listener():
    port = _free_port()
    assert not portguard.port_is_listening(port)
    with socket.socket() as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        assert portguard.port_is_listening(port)
    # Closed again.
    assert not portguard.port_is_listening(port)


def test_stale_pids_never_includes_self():
    assert os.getpid() not in portguard.stale_pids("server.py")
    # This test process is python running pytest, not server.py.
    assert os.getpid() not in portguard.stale_pids("pytest")


def test_stale_pids_never_includes_an_ancestor(tmp_path):
    """A wrapper shell mentioning the script must not be mistaken for it.

    `sh -c 'exec python server.py'` carries both "python" and "server.py" on its
    own command line; signalling it kills the startup doing the reclaiming.
    """
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, os, portguard\n"
        "print(json.dumps({'me': os.getpid(), 'parent': os.getppid(),\n"
        "                  'stale': portguard.stale_pids('server.py')}))\n",
        encoding="utf-8",
    )
    app_dir = os.path.dirname(os.path.abspath(portguard.__file__))
    # The parent shell's command line deliberately contains both tokens. Asserted
    # by exclusion rather than emptiness: a genuine server may be running here.
    out = subprocess.check_output(
        ["sh", "-c", f"exec {sys.executable} -u {probe} # python server.py"],
        env={**os.environ, "PYTHONPATH": app_dir},
        text=True,
        timeout=30,
    )
    seen = json.loads(out.strip())
    assert seen["me"] not in seen["stale"]
    assert seen["parent"] not in seen["stale"]


def test_shell_wrapper_is_not_an_interpreter_match():
    assert not portguard._is_interpreter_running(
        ["sh", "-c", "exec python -u server.py"], "server.py"
    )
    assert not portguard._is_interpreter_running(["timeout", "15", "python", "server.py"], "server.py")
    assert portguard._is_interpreter_running(["python", "-u", "server.py"], "server.py")
    assert portguard._is_interpreter_running(["python3.11", "/app/server.py"], "server.py")
    assert not portguard._is_interpreter_running(["python", "-u", "other.py"], "server.py")
    assert not portguard._is_interpreter_running([], "server.py")


def test_reclaim_is_a_noop_when_nothing_is_stale():
    # A script name no process can be running.
    assert portguard.reclaim(_free_port(), script="no_such_script_xyz.py") == []


# Deliberately NOT "server.py": reclaim() matches by basename, so a probe named
# server.py would make these tests kill the developer's actually-running server.
PROBE = "portguard_probe_server.py"


def _spawn(tmp_path, body: str) -> subprocess.Popen:
    script = tmp_path / PROBE
    script.write_text(body, encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(script)])
    for _ in range(60):
        if proc.pid in portguard.stale_pids(PROBE):
            return proc
        time.sleep(0.05)
    proc.kill()
    pytest.fail(f"probe {proc.pid} never appeared in stale_pids")


def test_reclaim_terminates_a_matching_process(tmp_path):
    proc = _spawn(tmp_path, "import time\nwhile True: time.sleep(0.05)\n")
    try:
        signalled = portguard.reclaim(_free_port(), script=PROBE, timeout_s=5.0)
        assert proc.pid in signalled
        assert proc.wait(timeout=5) is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_reclaim_escalates_to_sigkill(tmp_path):
    """A process ignoring SIGTERM must still be removed."""
    proc = _spawn(
        tmp_path,
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "while True: time.sleep(0.05)\n",
    )
    try:
        portguard.reclaim(_free_port(), script=PROBE, timeout_s=6.0, escalate_after_s=0.5)
        assert proc.wait(timeout=6) is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
