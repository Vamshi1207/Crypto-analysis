"""Helius logsSubscribe → Pump.fun create + trade tape for paper sniping.

Landed txs only. Creates start a watch; TradeEvents update the bonding-curve
mid so we buy/mark from the live curve instead of spraying every create.
"""

from __future__ import annotations

import json
import queue
import ssl
import struct
import threading
import time
import urllib.parse
from base64 import b64encode
from hashlib import sha1
from os import urandom
from typing import Any, Optional

from decision import pipeline_log
from decision.config import SETTINGS
from decision.pumpfun import (
    PUMP_PROGRAM,
    VIRTUAL_SOL_LAMPORTS,
    VIRTUAL_TOKEN_RAW,
    curve_price_usd,
    is_dev_sell,
    parse_create_logs,
    parse_trade_logs,
)

_TAPE_TTL_SEC = 180.0

_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=256)
_tapes: dict[str, dict[str, Any]] = {}
_stats = {
    "running": False,
    "connected": False,
    "creates": 0,
    "trades": 0,
    "last_error": None,
    "last_create_at": None,
    "last_trade_at": None,
}


def status() -> dict[str, Any]:
    with _lock:
        return {
            **_stats,
            "queued": _events.qsize(),
            "watching": len(_tapes),
            "program": PUMP_PROGRAM,
            "has_helius": bool(SETTINGS.helius_api_key),
        }


def tapes(*, sol_usd: float) -> dict[str, dict[str, Any]]:
    """Live curve tape keyed by mint. Virtual reserves → USD mid."""
    now = time.time()
    with _lock:
        stale = [
            mint
            for mint, row in _tapes.items()
            if now - float(row.get("updated_at") or 0.0) > _TAPE_TTL_SEC
        ]
        for mint in stale:
            _tapes.pop(mint, None)
        out: dict[str, dict[str, Any]] = {}
        for mint, row in _tapes.items():
            virt_sol = int(row.get("virt_sol") or 0)
            virt_token = int(row.get("virt_token") or 0)
            px = curve_price_usd(virt_sol, virt_token, sol_usd)
            buyers = row.get("buyers") or set()
            out[mint] = {
                "mint": mint,
                "symbol": row.get("symbol"),
                "creator": row.get("creator"),
                "bonding_curve": row.get("bonding_curve"),
                "virt_sol": virt_sol,
                "virt_token": virt_token,
                "real_sol": float(row.get("real_sol") or 0.0),
                "peak_real_sol": float(row.get("peak_real_sol") or 0.0),
                "dev_sold": bool(row.get("dev_sold")),
                "buys": int(row.get("buys") or 0),
                "sells": int(row.get("sells") or 0),
                "unique_buyers": len(buyers),
                "last_px": px,
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            }
        return out


def clear_tape() -> None:
    with _lock:
        _tapes.clear()
        _stats["trades"] = 0
        _stats["last_trade_at"] = None


def drain(*, limit: int = 16) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while len(out) < limit:
        try:
            out.append(_events.get_nowait())
        except queue.Empty:
            break
    return out


def start() -> dict[str, Any]:
    global _thread
    if not SETTINGS.helius_api_key:
        with _lock:
            _stats["last_error"] = "no HELIUS_API_KEY"
        return status()
    with _lock:
        if _thread and _thread.is_alive():
            return status()
        _stop.clear()
        _stats["running"] = True
        _stats["last_error"] = None
        _thread = threading.Thread(target=_run, name="pump-create-feed", daemon=True)
        _thread.start()
    pipeline_log.emit("snipe_feed", "start")
    return status()


def stop() -> dict[str, Any]:
    _stop.set()
    with _lock:
        _stats["running"] = False
        _stats["connected"] = False
    pipeline_log.emit("snipe_feed", "stop")
    return status()


def _ws_url() -> str:
    return f"wss://mainnet.helius-rpc.com/?api-key={SETTINGS.helius_api_key}"


def _run() -> None:
    backoff = 1.0
    while not _stop.is_set():
        try:
            _listen_once()
            backoff = 1.0
        except Exception as exc:  # noqa: BLE001
            with _lock:
                _stats["connected"] = False
                _stats["last_error"] = str(exc)
            pipeline_log.emit(
                "snipe_feed", "ws_error", level="warning", reason=type(exc).__name__
            )
            _stop.wait(backoff)
            backoff = min(backoff * 2.0, 30.0)
    with _lock:
        _stats["running"] = False
        _stats["connected"] = False


def _listen_once() -> None:
    parsed = urllib.parse.urlparse(_ws_url())
    host = parsed.hostname or "mainnet.helius-rpc.com"
    port = parsed.port or 443
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    sock = ssl.create_default_context().wrap_socket(
        __import__("socket").create_connection((host, port), timeout=20),
        server_hostname=host,
    )
    sock.settimeout(20)
    key = b64encode(urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(req.encode())
    header = b""
    while b"\r\n\r\n" not in header:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("ws handshake closed")
        header += chunk
    if b"101" not in header.split(b"\r\n", 1)[0]:
        raise ConnectionError("ws handshake rejected")
    expect = b64encode(sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
    if expect.encode() not in header:
        raise ConnectionError("ws accept mismatch")
    leftover = header.split(b"\r\n\r\n", 1)[1]
    _ws_send(sock, json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "logsSubscribe",
        "params": [
            {"mentions": [PUMP_PROGRAM]},
            {"commitment": "processed"},
        ],
    }))
    with _lock:
        _stats["connected"] = True
        _stats["last_error"] = None
    pipeline_log.emit("snipe_feed", "connected")
    buf = leftover
    sock.settimeout(15)
    try:
        while not _stop.is_set():
            try:
                chunk = sock.recv(8192)
            except (TimeoutError, OSError) as exc:
                if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
                    _ws_send(sock, "", opcode=0x9)
                    continue
                raise
            if not chunk:
                raise ConnectionError("ws closed")
            buf += chunk
            messages, buf = _ws_take(buf)
            for msg in messages:
                _on_message(msg)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _ws_send(sock: Any, text: str, *, opcode: int = 0x1) -> None:
    payload = text.encode() if text else b""
    header = bytes([0x80 | opcode])
    n = len(payload)
    mask = urandom(4)
    if n < 126:
        header += bytes([0x80 | n])
    elif n < 65536:
        header += bytes([0x80 | 126]) + struct.pack("!H", n)
    else:
        header += bytes([0x80 | 127]) + struct.pack("!Q", n)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(header + mask + masked)


def _ws_take(buf: bytes) -> tuple[list[str], bytes]:
    out: list[str] = []
    while len(buf) >= 2:
        b1, b2 = buf[0], buf[1]
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        n = b2 & 0x7F
        idx = 2
        if n == 126:
            if len(buf) < 4:
                break
            n = struct.unpack("!H", buf[2:4])[0]
            idx = 4
        elif n == 127:
            if len(buf) < 10:
                break
            n = struct.unpack("!Q", buf[2:10])[0]
            idx = 10
        if masked:
            if len(buf) < idx + 4 + n:
                break
            mask = buf[idx : idx + 4]
            idx += 4
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(buf[idx : idx + n]))
        else:
            if len(buf) < idx + n:
                break
            payload = buf[idx : idx + n]
        buf = buf[idx + n :]
        if opcode == 0x8:
            raise ConnectionError("ws close frame")
        if opcode == 0x9:
            continue
        if opcode == 0x1:
            out.append(payload.decode("utf-8", errors="replace"))
    return out, buf


def _note_create(parsed: dict[str, Any]) -> None:
    mint = parsed.get("mint") or ""
    if not mint:
        return
    now = time.time()
    with _lock:
        row = _tapes.get(mint) or {}
        row.update(
            {
                "symbol": parsed.get("symbol") or row.get("symbol"),
                "creator": parsed.get("creator") or row.get("creator"),
                "bonding_curve": parsed.get("bonding_curve") or row.get("bonding_curve"),
                "virt_sol": int(row.get("virt_sol") or VIRTUAL_SOL_LAMPORTS),
                "virt_token": int(row.get("virt_token") or VIRTUAL_TOKEN_RAW),
                "real_sol": float(row.get("real_sol") or 0.0),
                "peak_real_sol": float(row.get("peak_real_sol") or 0.0),
                "dev_sold": bool(row.get("dev_sold")),
                "buys": int(row.get("buys") or 0),
                "sells": int(row.get("sells") or 0),
                "buyers": row.get("buyers") if isinstance(row.get("buyers"), set) else set(),
                "created_at": row.get("created_at") or now,
                "updated_at": now,
            }
        )
        _tapes[mint] = row


def _note_trade(trade: dict[str, Any]) -> None:
    mint = trade.get("mint") or ""
    if not mint:
        return
    now = time.time()
    with _lock:
        row = _tapes.get(mint) or {
            "virt_sol": VIRTUAL_SOL_LAMPORTS,
            "virt_token": VIRTUAL_TOKEN_RAW,
            "real_sol": 0.0,
            "peak_real_sol": 0.0,
            "dev_sold": False,
            "buys": 0,
            "sells": 0,
            "buyers": set(),
            "created_at": now,
        }
        buyers = row.get("buyers")
        if not isinstance(buyers, set):
            buyers = set()
        row["virt_sol"] = int(trade.get("virt_sol") or row.get("virt_sol") or 0)
        row["virt_token"] = int(trade.get("virt_token") or row.get("virt_token") or 0)
        real_sol = float(trade.get("real_sol_ui") or row.get("real_sol") or 0.0)
        row["real_sol"] = real_sol
        row["peak_real_sol"] = max(float(row.get("peak_real_sol") or 0.0), real_sol)
        user = trade.get("user") or ""
        if trade.get("is_buy"):
            row["buys"] = int(row.get("buys") or 0) + 1
            if user:
                buyers.add(user)
        else:
            row["sells"] = int(row.get("sells") or 0) + 1
            if is_dev_sell(
                creator=str(row.get("creator") or ""),
                user=user,
                is_buy=False,
            ):
                row["dev_sold"] = True
        row["buyers"] = buyers
        row["updated_at"] = now
        _tapes[mint] = row
        _stats["trades"] = int(_stats["trades"] or 0) + 1
        _stats["last_trade_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _on_message(raw: str) -> None:
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return
    value = ((body.get("params") or {}).get("result") or {}).get("value") or {}
    if value.get("err"):
        return
    logs = value.get("logs") or []
    if not isinstance(logs, list):
        return
    created = parse_create_logs(logs)
    trades = parse_trade_logs(logs)
    if created:
        created["signature"] = value.get("signature") or ""
        created["seen_at"] = time.time()
        created["source"] = "pump_create"
        _note_create(created)
        try:
            _events.put_nowait(created)
        except queue.Full:
            try:
                _events.get_nowait()
            except queue.Empty:
                pass
            try:
                _events.put_nowait(created)
            except queue.Full:
                pass
        with _lock:
            _stats["creates"] = int(_stats["creates"] or 0) + 1
            _stats["last_create_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        pipeline_log.emit(
            "snipe_feed",
            "create",
            mint=created.get("mint"),
            symbol=created.get("symbol"),
            creator=(created.get("creator") or "")[:8],
        )
    for trade in trades:
        _note_trade(trade)
