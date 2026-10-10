"""On-chain pump curve reads: IDL-layout parse + live fetch."""

from __future__ import annotations

import struct

import pytest

from decision import pump_onchain


def _blob():
    parts = [bytes([23, 183, 248, 55, 96, 216, 172, 96])]
    for v in (1071000000000000, 30100000000, 790000000000000, 1500000000, 1000000000000000):
        parts.append(struct.pack("<Q", v))
    parts.append(bytes([0]))  # complete = False
    parts.append(bytes(32))  # creator
    parts.append(bytes([0, 0]))  # mayhem, cashback
    parts.append(bytes(32))  # quote_mint = zero key → SOL-quoted
    parts.append(struct.pack("<Q", 30))  # creator_fee_bps
    parts.append(bytes([1]))  # can_edit_creator_fee
    parts.append(bytes([0]))  # is_holder_reward
    parts.append(struct.pack("<Q", 0))  # creator_fee
    parts.append(struct.pack("<Q", 0))  # protocol_fees
    parts.append(bytes([0]))  # depth
    parts.append(struct.pack("<Q", 30000000000))  # initial_virtual_quote_reserves
    parts.append(struct.pack("<Q", 0))  # post_complete_base_out
    parts.append(struct.pack("<Q", 0))  # post_complete_quote_in
    assert len(b"".join(parts)) == 166
    return b"".join(parts)


def test_parse_layout_vectors():
    parsed = pump_onchain.parse_curve_account(_blob())
    assert parsed is not None
    assert parsed["v_tok"] == 1071000000000000
    assert parsed["v_sol"] == 30100000000
    assert parsed["real_tok"] == 790000000000000
    assert parsed["real_sol"] == 1500000000
    assert parsed["complete"] is False
    assert parsed["creator_fee_bps"] == 30
    assert parsed["sol_quoted"] is True


def test_parse_rejects_bad_discriminator_and_length():
    bad = bytearray(_blob())
    bad[0] ^= 0xFF
    assert pump_onchain.parse_curve_account(bytes(bad)) is None
    assert pump_onchain.parse_curve_account(b"\x00" * 10) is None
    assert pump_onchain.parse_curve_account(None) is None


def test_curve_pda_prefers_v1_shape():
    solders = pytest.importorskip("solders")
    out = pump_onchain.curve_pda("65bvXVS7DLf3E3YpxRYrhMMbx8N7TWUnLRNT6Zk7pump")
    assert out is not None
    addr, version = out
    assert version == "v1"
    assert len(addr) >= 32


@pytest.mark.live
def test_fetch_curve_live_mint():
    import os

    if not (os.getenv("HELIUS_API_KEY") or "").strip():
        pytest.skip("HELIUS_API_KEY not set")
    pytest.importorskip("solders")
    curve = pump_onchain.fetch_curve(
        "65bvXVS7DLf3E3YpxRYrhMMbx8N7TWUnLRNT6Zk7pump", timeout=20.0
    )
    # Structural validity against mainnet (this mint has since migrated,
    # so reserves read zeroed — the migrated shape must parse, not fail).
    assert curve is not None
    assert curve["version"] in ("v1", "v2")
    assert curve["supply"] > 0
    assert curve["sol_quoted"] is True
