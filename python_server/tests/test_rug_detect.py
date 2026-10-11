"""Confirmed-rug detector: ghost wick vs real on-chain dump."""

from __future__ import annotations

import time

from decision import rug_detect


def _enable(monkeypatch):
    monkeypatch.setattr(rug_detect, "ENABLED", True)
    monkeypatch.setattr(rug_detect, "WINDOW_SEC", 120.0)
    monkeypatch.setattr(rug_detect, "MIN_SELLS", 1)
    monkeypatch.setattr(rug_detect, "TOL_PCT", 5.0)


def test_disabled_is_unconfirmed(monkeypatch):
    monkeypatch.setattr(rug_detect, "ENABLED", False)
    out = rug_detect.confirmed_rug(mint="MintA", pool="PoolA", gapped_price=1.0)
    assert out["confirmed"] is False
    assert out["evidence"] == "disabled"


def test_no_mint_is_unconfirmed(monkeypatch):
    _enable(monkeypatch)
    out = rug_detect.confirmed_rug(mint="", pool="PoolA", gapped_price=1.0)
    assert out["confirmed"] is False


def test_onchain_sells_confirm(monkeypatch):
    _enable(monkeypatch)
    now = time.time()
    txs = [
        {
            "timestamp": now - 5,
            "tokenTransfers": [
                {"mint": "MintA", "fromUserAccount": "UserA", "toUserAccount": "PoolA"},
            ],
        }
    ]

    def _fake_txs(address, *, limit=20):
        return txs

    monkeypatch.setattr(rug_detect, "fetch_helius_transactions", _fake_txs)
    monkeypatch.setattr(rug_detect, "fetch_gecko_ohlcv", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("not reached")))
    out = rug_detect.confirmed_rug(mint="MintA", pool="PoolA", gapped_price=1.0, now=now)
    assert out["confirmed"] is True
    assert out["sells"] == 1
    assert "on-chain sells" in out["evidence"]


def test_gecko_low_agreement_confirms(monkeypatch):
    _enable(monkeypatch)
    now = time.time()
    monkeypatch.setattr(rug_detect, "fetch_helius_transactions", lambda *a, **k: [])
    # Gecko rows are [ts, o, h, l, c, v]; a low of 0.5 agrees with a 0.52 mark.
    monkeypatch.setattr(
        rug_detect,
        "fetch_gecko_ohlcv",
        lambda *a, **k: [[now - 10, 1.0, 1.0, 0.5, 0.52, 100.0]],
    )
    out = rug_detect.confirmed_rug(mint="MintA", pool="PoolA", gapped_price=0.52, now=now)
    assert out["confirmed"] is True
    assert "gecko low" in out["evidence"]


def test_no_signal_is_unconfirmed(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(rug_detect, "fetch_helius_transactions", lambda *a, **k: [])
    monkeypatch.setattr(rug_detect, "fetch_gecko_ohlcv", lambda *a, **k: [])
    out = rug_detect.confirmed_rug(mint="MintA", pool="PoolA", gapped_price=1.0)
    assert out["confirmed"] is False
    assert "unconfirmed" in out["evidence"]


def test_helius_error_falls_through_to_gecko(monkeypatch):
    _enable(monkeypatch)
    now = time.time()

    def _boom(*a, **k):
        raise rug_detect.SourceError("no key")

    monkeypatch.setattr(rug_detect, "fetch_helius_transactions", _boom)
    monkeypatch.setattr(
        rug_detect,
        "fetch_gecko_ohlcv",
        lambda *a, **k: [[now - 10, 1.0, 1.0, 0.4, 0.45, 50.0]],
    )
    out = rug_detect.confirmed_rug(mint="MintA", pool="PoolA", gapped_price=0.42, now=now)
    assert out["confirmed"] is True
    assert "gecko low" in out["evidence"]
