"""Pump.fun create/trade helpers for the paper sniper.

Public logs only — the create/trade transaction has already landed. No
mempool read, no Jito create+buy bundle, no pending-swap sandwich.
"""

from __future__ import annotations

import base64
import struct
import time
from typing import Any, Optional

from decision.config import _env_float, _env_int

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
# Initial virtual reserves on a standard Pump.fun curve (token decimals = 6).
VIRTUAL_SOL_LAMPORTS = 30_000_000_000
VIRTUAL_TOKEN_RAW = 1_073_000_000_000_000
TOKEN_DECIMALS = 6
# Anchor event: sha256("event:TradeEvent")[:8]
TRADE_EVENT_DISC = bytes.fromhex("bddb7fd34ee661ee")

MAX_CREATES_PER_HOUR = _env_int("SNIPER_MAX_CREATES_PER_HOUR", 3)
CREATOR_WINDOW_SEC = _env_float("SNIPER_CREATOR_WINDOW_SEC", 3600.0)

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out: list[str] = []
    while n:
        n, r = divmod(n, 58)
        out.append(_B58[r])
    pad = 0
    for byte in data:
        if byte == 0:
            pad += 1
        else:
            break
    return (_B58[0] * pad) + ("".join(reversed(out)) if out else "")


def b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        idx = _B58.find(ch)
        if idx < 0:
            raise ValueError("invalid base58")
        n = n * 58 + idx
    raw = n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")
    pad = 0
    for ch in text:
        if ch == _B58[0]:
            pad += 1
        else:
            break
    return b"\x00" * pad + raw


def initial_price_sol() -> float:
    tokens = VIRTUAL_TOKEN_RAW / (10**TOKEN_DECIMALS)
    sol = VIRTUAL_SOL_LAMPORTS / 1e9
    return sol / tokens


def initial_price_usd(sol_usd: float) -> float:
    if sol_usd <= 0:
        return 0.0
    return initial_price_sol() * sol_usd


def curve_price_usd(virt_sol: int, virt_token: int, sol_usd: float) -> float:
    """USD mid from Pump virtual reserves (constant-product curve)."""
    if virt_sol <= 0 or virt_token <= 0 or sol_usd <= 0:
        return 0.0
    tokens = virt_token / (10**TOKEN_DECIMALS)
    sol = virt_sol / 1e9
    if tokens <= 0:
        return 0.0
    return (sol / tokens) * sol_usd


def parse_trade_event(raw: bytes) -> Optional[dict[str, Any]]:
    """Decode an Anchor TradeEvent (121-byte body after the discriminator)."""
    if len(raw) < 8 + 121:
        return None
    if raw[:8] != TRADE_EVENT_DISC:
        return None
    mint = b58encode(raw[8:40])
    sol_amount = int.from_bytes(raw[40:48], "little")
    token_amount = int.from_bytes(raw[48:56], "little")
    is_buy = raw[56] == 1
    user = b58encode(raw[57:89])
    ts = int.from_bytes(raw[89:97], "little", signed=True)
    virt_sol = int.from_bytes(raw[97:105], "little")
    virt_token = int.from_bytes(raw[105:113], "little")
    real_sol = int.from_bytes(raw[113:121], "little")
    real_token = int.from_bytes(raw[121:129], "little")
    if len(mint) < 32 or len(user) < 32:
        return None
    return {
        "mint": mint,
        "user": user,
        "is_buy": is_buy,
        "sol_amount": sol_amount,
        "token_amount": token_amount,
        "timestamp": ts,
        "virt_sol": virt_sol,
        "virt_token": virt_token,
        "real_sol": real_sol,
        "real_token": real_token,
        "real_sol_ui": real_sol / 1e9,
    }


def encode_trade_event(
    *,
    mint: bytes,
    user: bytes,
    is_buy: bool,
    sol_amount: int = 1_000_000_000,
    token_amount: int = 1_000_000,
    timestamp: int = 1_700_000_000,
    virt_sol: int = VIRTUAL_SOL_LAMPORTS,
    virt_token: int = VIRTUAL_TOKEN_RAW,
    real_sol: int = 1_000_000_000,
    real_token: int = VIRTUAL_TOKEN_RAW,
) -> bytes:
    """Test helper: Borsh-encode a TradeEvent body."""
    return (
        TRADE_EVENT_DISC
        + mint
        + struct.pack("<QQ", sol_amount, token_amount)
        + bytes([1 if is_buy else 0])
        + user
        + struct.pack("<qQQQQ", timestamp, virt_sol, virt_token, real_sol, real_token)
    )


def parse_trade_logs(logs: list[str]) -> list[dict[str, Any]]:
    """Pull every Pump.fun TradeEvent out of ``logsSubscribe`` log lines."""
    out: list[dict[str, Any]] = []
    for line in logs:
        if not isinstance(line, str):
            continue
        marker = "Program data: "
        if marker not in line:
            continue
        payload = line.split(marker, 1)[-1].strip()
        try:
            raw = base64.b64decode(payload)
        except (ValueError, TypeError):
            continue
        parsed = parse_trade_event(raw)
        if parsed:
            out.append(parsed)
    return out


def snipe_entry(
    *,
    create_px: float,
    last_px: float,
    unique_buyers: int,
    buys: int,
    sells: int,
    real_sol: float,
    age_sec: float,
    watch_sec: float = 45.0,
    min_buyers: int = 2,
    min_lift_pct: float = 5.0,
    fast_lift_pct: float = 12.0,
    min_real_sol: float = 2.5,
) -> tuple[bool, str]:
    """Buy only when the curve shows demand — never on a bare create."""
    if create_px <= 0:
        return False, "no_create_px"
    if age_sec > watch_sec:
        return False, "watch_expired"
    if sells > buys:
        return False, "net_selling"
    lift = ((last_px / create_px) - 1.0) * 100.0 if last_px > 0 else 0.0
    if unique_buyers >= min_buyers and lift >= min_lift_pct:
        return True, "tape_lift"
    if unique_buyers >= 1 and real_sol >= min_real_sol and lift >= fast_lift_pct:
        return True, "curve_demand"
    return False, "waiting_tape"


def snipe_size(
    base: float,
    *,
    lift_pct: float,
    unique_buyers: int,
    real_sol: float,
) -> float:
    """Smaller clip on a thin tape; full size only when the curve is crowded."""
    if base <= 0:
        return 0.0
    if lift_pct >= 25.0 and unique_buyers >= 4:
        return round(base, 2)
    if lift_pct >= 12.0 or unique_buyers >= 3 or real_sol >= 5.0:
        return round(base * 0.7, 2)
    return round(base * 0.5, 2)


def snipe_lift_pct(create_px: float, last_px: float) -> float:
    if create_px <= 0 or last_px <= 0:
        return 0.0
    return ((last_px / create_px) - 1.0) * 100.0


def _borsh_string(buf: bytes, offset: int) -> tuple[str, int]:
    if offset + 4 > len(buf):
        raise ValueError("short string len")
    n = int.from_bytes(buf[offset : offset + 4], "little")
    offset += 4
    if n > 512 or offset + n > len(buf):
        raise ValueError("short string body")
    return buf[offset : offset + n].decode("utf-8", errors="replace"), offset + n


def parse_create_event(raw: bytes) -> Optional[dict[str, str]]:
    """Decode an Anchor CreateEvent after the 8-byte discriminator."""
    if len(raw) < 8 + 12 + 96:
        return None
    try:
        offset = 8
        name, offset = _borsh_string(raw, offset)
        symbol, offset = _borsh_string(raw, offset)
        uri, offset = _borsh_string(raw, offset)
        if offset + 96 > len(raw):
            return None
        mint = b58encode(raw[offset : offset + 32])
        curve = b58encode(raw[offset + 32 : offset + 64])
        user = b58encode(raw[offset + 64 : offset + 96])
    except (ValueError, UnicodeError):
        return None
    if len(mint) < 32 or len(curve) < 32 or len(user) < 32:
        return None
    return {
        "name": name[:64],
        "symbol": (symbol or name or mint[:6])[:16],
        "uri": uri[:200],
        "mint": mint,
        "bonding_curve": curve,
        "creator": user,
    }


def encode_create_event(
    *,
    name: str,
    symbol: str,
    uri: str,
    mint: bytes,
    bonding_curve: bytes,
    creator: bytes,
    disc: bytes = b"\x00" * 8,
) -> bytes:
    """Test helper: Borsh-encode a CreateEvent body."""

    def pack(text: str) -> bytes:
        body = text.encode()
        return len(body).to_bytes(4, "little") + body

    return disc + pack(name) + pack(symbol) + pack(uri) + mint + bonding_curve + creator


def parse_create_logs(logs: list[str]) -> Optional[dict[str, str]]:
    """Pull a Pump.fun CreateEvent out of ``logsSubscribe`` log lines."""
    create = False
    for line in logs:
        if not isinstance(line, str):
            continue
        low = line.lower()
        if "instruction: create" in low and "idempotent" not in low:
            create = True
            continue
        if not create:
            continue
        marker = "Program data: "
        if marker not in line:
            continue
        payload = line.split(marker, 1)[-1].strip()
        try:
            raw = base64.b64decode(payload)
        except (ValueError, TypeError):
            continue
        parsed = parse_create_event(raw)
        if parsed:
            return parsed
    return None


class CreatorBook:
    """Skip wallets that spray many creates in one window (serial rugs)."""

    def __init__(
        self,
        *,
        max_per_window: int = MAX_CREATES_PER_HOUR,
        window_sec: float = CREATOR_WINDOW_SEC,
    ) -> None:
        self.max_per_window = max_per_window
        self.window_sec = window_sec
        self._hits: dict[str, list[float]] = {}

    def too_hot(self, creator: str, *, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        cutoff = now - self.window_sec
        times = [t for t in self._hits.get(creator, []) if t >= cutoff]
        self._hits[creator] = times
        return len(times) >= self.max_per_window

    def note(self, creator: str, *, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        self._hits.setdefault(creator, []).append(now)

    def clear(self) -> None:
        self._hits.clear()


def logs_are_create(logs: list[Any]) -> bool:
    for line in logs:
        if isinstance(line, str) and "instruction: create" in line.lower():
            if "idempotent" in line.lower():
                continue
            return True
    return False
