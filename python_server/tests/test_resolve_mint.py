"""Hermetic tests for pool→mint resolution used by live Gate 0."""

from __future__ import annotations

import pytest

from decision.config import WRAPPED_SOL_MINT
from decision.sources import SourceError, _mint_from_pair, resolve_to_mint


def test_mint_from_pair_prefers_non_sol_base():
    mint, symbol = _mint_from_pair(
        {
            "baseToken": {"address": "MemeMint1111111111111111111111111111111", "symbol": "MEME"},
            "quoteToken": {"address": WRAPPED_SOL_MINT, "symbol": "SOL"},
        }
    )
    assert mint.startswith("MemeMint")
    assert symbol == "MEME"


def test_mint_from_pair_uses_quote_when_base_is_sol():
    mint, symbol = _mint_from_pair(
        {
            "baseToken": {"address": WRAPPED_SOL_MINT, "symbol": "SOL"},
            "quoteToken": {"address": "MemeMint2222222222222222222222222222222", "symbol": "XYZ"},
        }
    )
    assert mint.startswith("MemeMint2")
    assert symbol == "XYZ"


def test_resolve_rejects_evm_address():
    with pytest.raises(SourceError, match="EVM"):
        resolve_to_mint("0x1c58f34088e33ff14bac3715986d40f296aec7da")
