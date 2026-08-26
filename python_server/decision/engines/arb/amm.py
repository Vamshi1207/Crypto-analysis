import json
import logging
import ssl
import socket
import threading
import time
from base64 import b64encode
from os import urandom
from typing import Any, Callable, Optional

from decision import pipeline_log
from decision.config import SETTINGS

logger = logging.getLogger(__name__)

_pools: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()
_stop = threading.Event()
_thread: Optional[threading.Thread] = None

# mapping of subscription ID -> vault pubkey
_sub_to_vault: dict[int, str] = {}
_vault_to_pool: dict[str, tuple[str, str]] = {}
_callbacks: list[Callable[[str], None]] = []

def register_callback(cb: Callable[[str], None]) -> None:
    with _lock:
        if cb not in _callbacks:
            _callbacks.append(cb)

def configure_pools(pools_config: list[dict[str, Any]]) -> None:
    with _lock:
        _pools.clear()
        _vault_to_pool.clear()
        for p in pools_config:
            pid = p["pool_id"]
            _pools[pid] = {
                "config": p,
                "base_reserve_raw": 0,
                "quote_reserve_raw": 0,
                "ready": False
            }
            _vault_to_pool[p["base_vault"]] = (pid, "base")
            _vault_to_pool[p["quote_vault"]] = (pid, "quote")

def get_quote(pool_id: str, amount_in_raw: int, is_base_in: bool) -> Optional[int]:
    """
    CPMM Constant Product Math: x * y = k
    Fee is typically 0.25% for Raydium AMM v4.
    """
    with _lock:
        pool = _pools.get(pool_id)
        if not pool or not pool["ready"]:
            return None
            
        base_reserve = pool["base_reserve_raw"]
        quote_reserve = pool["quote_reserve_raw"]
        fee_pct = pool["config"]["fee_pct"]
        
    if base_reserve <= 0 or quote_reserve <= 0:
        return None
        
    fee_amount = int(amount_in_raw * fee_pct / 100)
    amount_in_after_fee = amount_in_raw - fee_amount
    
    if is_base_in:
        reserve_in = base_reserve
        reserve_out = quote_reserve
    else:
        reserve_in = quote_reserve
        reserve_out = base_reserve
        
    numerator = amount_in_after_fee * reserve_out
    denominator = reserve_in + amount_in_after_fee
    
    amount_out = numerator // denominator
    return amount_out

def _ws_send(sock: socket.socket, msg: str, opcode: int = 0x1) -> None:
    payload = msg.encode("utf-8")
    length = len(payload)
    header = bytearray([0x80 | opcode])
    if length <= 125:
        header.append(length | 0x80)
    elif length < 65536:
        header.append(126 | 0x80)
        header.extend(length.to_bytes(2, "big"))
    else:
        header.append(127 | 0x80)
        header.extend(length.to_bytes(8, "big"))
    
    mask = urandom(4)
    header.extend(mask)
    masked = bytearray(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(header + masked)

def _ws_recv(sock: socket.socket) -> str:
    header = sock.recv(2)
    if not header:
        raise ConnectionError("WS closed")
    b1, b2 = header
    opcode = b1 & 0x0F
    is_masked = bool(b2 & 0x80)
    payload_len = b2 & 0x7F
    
    if payload_len == 126:
        payload_len = int.from_bytes(sock.recv(2), "big")
    elif payload_len == 127:
        payload_len = int.from_bytes(sock.recv(8), "big")
        
    if is_masked:
        mask = sock.recv(4)
        
    data = bytearray()
    while len(data) < payload_len:
        chunk = sock.recv(min(8192, payload_len - len(data)))
        if not chunk:
            raise ConnectionError("WS closed mid-frame")
        data.extend(chunk)
        
    if is_masked:
        data = bytearray(b ^ mask[i % 4] for i, b in enumerate(data))
        
    if opcode == 0x9:
        _ws_send(sock, data.decode("utf-8", "ignore"), opcode=0xA)
        return ""
    elif opcode == 0x8:
        raise ConnectionError("WS received close frame")
        
    return data.decode("utf-8", "ignore")

def start():
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_run, name="local-amm", daemon=True)
        _thread.start()

def stop():
    _stop.set()

def _run() -> None:
    while not _stop.is_set():
        try:
            _connect_and_listen()
        except Exception as exc:
            logger.error(f"Local AMM WS error: {exc}")
            time.sleep(1.0)

def _connect_and_listen() -> None:
    host = "mainnet.helius-rpc.com"
    key = SETTINGS.current_helius_key()
    if not key:
        logger.warning("No Helius key for Local AMM WS")
        time.sleep(5)
        return
        
    path = f"/?api-key={key}"
    
    sock = ssl.create_default_context().wrap_socket(
        __import__("socket").create_connection((host, 443), timeout=20),
        server_hostname=host,
    )
    sock.settimeout(20)
    ws_key = b64encode(urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {ws_key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(req.encode())
    
    header = b""
    while b"\r\n\r\n" not in header:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("WS handshake closed")
        header += chunk
        
    with _lock:
        vaults = list(_vault_to_pool.keys())
        
    if not vaults:
        time.sleep(5)
        return
        
    sub_id = 1
    req_to_vault = {}
    for vault in vaults:
        _ws_send(sock, json.dumps({
            "jsonrpc": "2.0",
            "id": sub_id,
            "method": "accountSubscribe",
            "params": [vault, {"encoding": "jsonParsed", "commitment": "confirmed"}]
        }))
        req_to_vault[sub_id] = vault
        sub_id += 1

    pipeline_log.emit("local_amm", "connected", vaults_tracked=len(vaults))
    
    _sub_to_vault.clear()
    
    sock.settimeout(15)
    while not _stop.is_set():
        try:
            msg = _ws_recv(sock)
        except (TimeoutError, OSError) as exc:
            if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
                _ws_send(sock, "", opcode=0x9)
                continue
            raise
            
        if not msg:
            continue
            
        try:
            data = json.loads(msg)
        except json.JSONDecodeError:
            continue
            
        if "id" in data and "result" in data:
            rid = data["id"]
            if rid in req_to_vault:
                _sub_to_vault[data["result"]] = req_to_vault[rid]
                
        if "method" in data and data["method"] == "accountNotification":
            sub_id = data.get("params", {}).get("subscription")
            if sub_id in _sub_to_vault:
                vault = _sub_to_vault[sub_id]
                parsed = data.get("params", {}).get("result", {}).get("value", {}).get("data", {}).get("parsed", {})
                if parsed and parsed.get("type") == "account":
                    ui_amount_string = parsed.get("info", {}).get("tokenAmount", {}).get("amount", "0")
                    raw_amount = int(ui_amount_string)
                    
                    with _lock:
                        pool_id, side = _vault_to_pool[vault]
                        if side == "base":
                            _pools[pool_id]["base_reserve_raw"] = raw_amount
                        else:
                            _pools[pool_id]["quote_reserve_raw"] = raw_amount
                            
                        # If both are > 0, we're ready
                        if _pools[pool_id]["base_reserve_raw"] > 0 and _pools[pool_id]["quote_reserve_raw"] > 0:
                            was_ready = _pools[pool_id]["ready"]
                            _pools[pool_id]["ready"] = True
                            
                            cbs = list(_callbacks)
                            if cbs:
                                # Run callbacks in a separate thread so we don't block the WS reader
                                threading.Thread(
                                    target=lambda p=pool_id, funcs=cbs: [f(p) for f in funcs],
                                    daemon=True
                                ).start()
