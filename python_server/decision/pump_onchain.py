"""On-chain pump.fun bonding-curve reads (exact reserves, no estimates).

Derives the curve PDA (v1 then v2 seeds) with the ``solders`` library —
never hand-rolled ed25519 — fetches it over the Helius RPC endpoint, and
parses it with the exact IDL-derived 166-byte layout. Every entry point
returns None on any failure (unknown layout version, missing account,
no key, bad input) so callers always fall back to Dex estimates.
"""

from __future__ import annotations

import base64
import os
import struct
import time
from typing import Any, Optional, Tuple

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
ZERO_KEY = "11111111111111111111111111111111"

# IDL BondingCurve account discriminator.
DISCRIMINATOR = bytes([23, 183, 248, 55, 96, 216, 172, 96])
ACCOUNT_LEN = 166

_U64 = struct.Struct("<Q")

# PDA cache: mint -> (ts, address or None). Derivation is local CPU work
# but not free (ed25519 per bump); derivation misses are cached too.
_pda_cache: dict[str, tuple[float, Optional[Tuple[str, str]]]] = {}
PDA_CACHE_SEC = 3600.0


def _derive(mint_b: bytes, prog, seed: bytes) -> Optional[str]:
    try:
        from solders.pubkey import Pubkey

        addr, _bump = Pubkey.find_program_address([seed, mint_b], prog)
        return str(addr)
    except Exception:  # noqa: BLE001
        return None


def curve_pda(mint: str) -> Optional[Tuple[str, str]]:
    """(address, version) for a mint's curve account. Prefers v1.

    Derivation alone cannot tell which version exists on chain (some
    bump always yields an off-curve address), so :func:`fetch_curve`
    probes both. This helper is for display/diagnostics.
    """
    mint = (mint or "").strip()
    if not mint:
        return None
    now = time.time()
    hit = _pda_cache.get(mint)
    if hit and now - hit[0] < PDA_CACHE_SEC:
        return hit[1]
    try:
        from solders.pubkey import Pubkey

        prog = Pubkey.from_string(PUMP_PROGRAM)
        mb = bytes(Pubkey.from_string(mint))
    except Exception:  # noqa: BLE001 — dependency absent or bad address
        return None
    out: Optional[Tuple[str, str]] = None
    for tag, seed in (("v1", b"bonding-curve"), ("v2", b"bonding-curve-v2")):
        addr = _derive(mb, prog, seed)
        if addr:
            out = (addr, tag)
            break
    _pda_cache[mint] = (now, out)
    return out


def _rpc_url() -> Optional[str]:
    raw = os.getenv("HELIUS_API_KEY", "")
    key = (raw.split(",")[0] if raw else "").strip()
    if not key:
        return None
    return f"https://mainnet.helius-rpc.com/?api-key={key}"


def parse_curve_account(raw: bytes) -> Optional[dict[str, Any]]:
    """Parse 166-byte BondingCurve data. None on discriminator/length mismatch."""
    if raw is None or len(raw) != ACCOUNT_LEN:
        return None
    if bytes(raw[:8]) != DISCRIMINATOR:
        return None
    off = 8

    def u64() -> int:
        nonlocal off
        v = _U64.unpack_from(raw, off)[0]
        off += 8
        return v

    def boolean() -> bool:
        nonlocal off
        v = bool(raw[off])
        off += 1
        return v

    def pubkey() -> str:
        nonlocal off
        from solders.pubkey import Pubkey

        v = str(Pubkey(bytes(raw[off:off + 32])))
        off += 32
        return v

    try:
        out = {
            "v_tok": u64(),
            "v_sol": u64(),
            "real_tok": u64(),
            "real_sol": u64(),
            "supply": u64(),
            "complete": boolean(),
            "creator": pubkey(),
            "mayhem": boolean(),
            "cashback": boolean(),
            "quote_mint": pubkey(),
            "creator_fee_bps": u64(),
        }
    except Exception:  # noqa: BLE001
        return None
    out["sol_quoted"] = out["quote_mint"] in (
        ZERO_KEY,
        "So11111111111111111111111111111111111111112",
    )
    return out


def fetch_curve(mint: str, *, timeout: float = 10.0) -> Optional[dict[str, Any]]:
    """Exact on-chain curve state, or None when anything is missing.

    Probes v1 then v2 PDAs; a present account with a wrong discriminator
    (program upgrade) is treated as missing, never mis-parsed.
    """
    mint = (mint or "").strip()
    url = _rpc_url()
    if not mint or url is None:
        return None
    try:
        from solders.pubkey import Pubkey

        prog = Pubkey.from_string(PUMP_PROGRAM)
        mb = bytes(Pubkey.from_string(mint))
    except Exception:  # noqa: BLE001
        return None
    try:
        import httpx
    except Exception:  # noqa: BLE001
        return None
    for tag, seed in (("v1", b"bonding-curve"), ("v2", b"bonding-curve-v2")):
        address = _derive(mb, prog, seed)
        if not address:
            continue
        try:
            resp = httpx.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getAccountInfo",
                    "params": [address, {"encoding": "base64"}],
                },
                timeout=timeout,
            )
            value = (resp.json().get("result") or {}).get("value")
        except Exception:  # noqa: BLE001
            continue
        if not value or value.get("owner") != PUMP_PROGRAM:
            continue
        data = (value.get("data") or [None])[0]
        if not data:
            continue
        try:
            parsed = parse_curve_account(base64.b64decode(data))
        except Exception:  # noqa: BLE001
            continue
        if parsed is None:
            continue
        parsed["version"] = tag
        parsed["address"] = address
        return parsed
    return None
