"""Gate 0 tests.

Sources are patched at the names `decision.safety` imported, which also bypasses
the TTL cache, so each test is hermetic and offline.

Two tests here are regressions for bugs that only appeared against live mainnet
data and that no amount of unit testing in isolation would have caught:
`test_quote_only_mint_does_not_describe_counterparty` and
`test_dormant_deep_pool_is_not_primary`.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import pytest

from decision import safety
from decision.config import TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID
from decision.schema import SafetyVerdict, Severity, SourceStatus
from decision.sources import NoRouteError, SourceError, TooManyAccountsError

MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
OTHER_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

SUPPLY = 1_000_000_000_000  # 1M tokens at 6 decimals


def mint_info(
    *,
    mint_authority: Optional[str] = None,
    freeze_authority: Optional[str] = None,
    extensions: Optional[list[dict[str, Any]]] = None,
    token_2022: bool = False,
    supply: int = SUPPLY,
    decimals: int = 6,
) -> dict[str, Any]:
    return {
        "decimals": decimals,
        "supply": str(supply),
        "mintAuthority": mint_authority,
        "freezeAuthority": freeze_authority,
        "isInitialized": True,
        "extensions": extensions or [],
        "_program_owner": TOKEN_2022_PROGRAM_ID if token_2022 else TOKEN_PROGRAM_ID,
        "_is_token_2022": token_2022,
    }


def holder_accounts(*shares: float) -> list[dict[str, Any]]:
    """Build largest-account entries from fractions of total supply."""
    return [
        {"address": f"holder{i}", "amount": str(int(SUPPLY * share))}
        for i, share in enumerate(shares)
    ]


def pair(
    *,
    base: str = MINT,
    liquidity: Optional[float] = 250_000.0,
    volume_h24: float = 100_000.0,
    buys: int = 50,
    sells: int = 40,
    age_min: float = 600.0,
    price: str = "0.01",
    dex: str = "raydium",
) -> dict[str, Any]:
    return {
        "chainId": "solana",
        "dexId": dex,
        "pairAddress": f"pair-{dex}-{liquidity or 0:.0f}",
        "baseToken": {"address": base, "symbol": "TEST" if base == MINT else "OTHER"},
        "quoteToken": {"address": OTHER_MINT, "symbol": "USDC"},
        "priceUsd": price,
        # DexScreener omits the object entirely for bonding-curve venues.
        "liquidity": {"usd": liquidity} if liquidity is not None else None,
        "volume": {"h1": volume_h24 / 24, "h24": volume_h24},
        "txns": {"h1": {"buys": buys, "sells": sells}},
        "priceChange": {"h1": 1.5},
        "fdv": 5_000_000.0,
        "pairCreatedAt": (time.time() - age_min * 60) * 1000,
    }


def sell_quote(impact: str = "0.001", label: str = "Raydium") -> dict[str, Any]:
    return {
        "outAmount": "12345",
        "priceImpactPct": impact,
        "routePlan": [{"swapInfo": {"label": label}}],
    }


@pytest.fixture
def wire(monkeypatch):
    """Patch all four sources. Pass an Exception instance to simulate failure."""

    def apply(
        *,
        mint: Any = None,
        holders: Any = None,
        pairs: Any = None,
        quote: Any = None,
    ) -> None:
        def stub(value):
            def inner(*_args, **_kwargs):
                if isinstance(value, Exception):
                    raise value
                return value

            return inner

        monkeypatch.setattr(safety, "fetch_mint_account", stub(mint))
        monkeypatch.setattr(safety, "fetch_largest_accounts", stub(holders))
        monkeypatch.setattr(safety, "fetch_dexscreener_pairs", stub(pairs))
        monkeypatch.setattr(safety, "fetch_sell_quote", stub(quote))

    return apply


def codes(report) -> set[str]:
    return {flag.code for flag in report.flags}


# --- happy path ---------------------------------------------------------------


def test_clean_token_is_safe(wire):
    wire(
        mint=mint_info(),
        holders=holder_accounts(0.30, 0.04, 0.03, 0.02, 0.02),
        pairs=[pair()],
        quote=sell_quote(),
    )
    report = safety.check_token(MINT)

    assert report.verdict is SafetyVerdict.SAFE
    assert report.risk_score == 0
    assert report.flags == []
    assert report.degraded is False
    assert report.blocking is False
    assert report.sellability.sellable is True
    assert report.market.symbol == "TEST"


# --- critical rug mechanisms --------------------------------------------------


def test_live_mint_authority_is_danger(wire):
    wire(
        mint=mint_info(mint_authority="Attacker111"),
        holders=holder_accounts(0.3, 0.02),
        pairs=[pair()],
        quote=sell_quote(),
    )
    report = safety.check_token(MINT)

    assert report.verdict is SafetyVerdict.DANGER
    assert report.blocking is True
    assert "mint_authority_active" in codes(report)


def test_freeze_authority_alone_is_not_critical(wire):
    """Freeze authority is serious but not on its own a reason to hard-block."""
    wire(
        mint=mint_info(freeze_authority="Issuer111"),
        holders=holder_accounts(0.3, 0.02),
        pairs=[pair()],
        quote=sell_quote(),
    )
    report = safety.check_token(MINT)

    assert "freeze_authority_active" in codes(report)
    assert not any(f.severity is Severity.CRITICAL for f in report.flags)
    assert report.verdict is SafetyVerdict.CAUTION


@pytest.mark.parametrize(
    "extension",
    ["nonTransferable", "permanentDelegate", "transferHook"],
)
def test_token2022_trap_extensions_are_danger(wire, extension):
    wire(
        mint=mint_info(extensions=[{"extension": extension}], token_2022=True),
        holders=holder_accounts(0.3, 0.02),
        pairs=[pair()],
        quote=sell_quote(),
    )
    report = safety.check_token(MINT)

    assert report.verdict is SafetyVerdict.DANGER
    assert f"ext_{extension}" in codes(report)
    assert report.authorities.is_token_2022 is True


def test_default_frozen_accounts_is_danger(wire):
    wire(
        mint=mint_info(
            extensions=[
                {"extension": "defaultAccountState", "state": {"accountState": "frozen"}}
            ],
            token_2022=True,
        ),
        holders=holder_accounts(0.3, 0.02),
        pairs=[pair()],
        quote=sell_quote(),
    )
    report = safety.check_token(MINT)

    assert report.verdict is SafetyVerdict.DANGER
    assert "default_frozen" in codes(report)


def test_high_transfer_fee_is_flagged_and_reported(wire):
    wire(
        mint=mint_info(
            extensions=[
                {
                    "extension": "transferFeeConfig",
                    "state": {"newerTransferFee": {"transferFeeBasisPoints": 1000}},
                }
            ],
            token_2022=True,
        ),
        holders=holder_accounts(0.3, 0.02),
        pairs=[pair()],
        quote=sell_quote(),
    )
    report = safety.check_token(MINT)

    assert "high_transfer_fee" in codes(report)
    assert report.authorities.transfer_fee_pct == 10.0


def test_low_transfer_fee_is_not_flagged(wire):
    wire(
        mint=mint_info(
            extensions=[
                {
                    "extension": "transferFeeConfig",
                    "state": {"newerTransferFee": {"transferFeeBasisPoints": 100}},
                }
            ],
            token_2022=True,
        ),
        holders=holder_accounts(0.3, 0.02),
        pairs=[pair()],
        quote=sell_quote(),
    )
    report = safety.check_token(MINT)

    assert "high_transfer_fee" not in codes(report)
    assert report.authorities.transfer_fee_pct == 1.0


# --- sellability -------------------------------------------------------------


def test_unroutable_sell_is_critical(wire):
    wire(
        mint=mint_info(),
        holders=holder_accounts(0.3, 0.02),
        pairs=[pair()],
        quote=NoRouteError("no route found"),
    )
    report = safety.check_token(MINT)

    assert report.verdict is SafetyVerdict.DANGER
    assert "not_sellable" in codes(report)
    assert report.sellability.sellable is False


def test_jupiter_outage_is_unknown_not_unsellable(wire):
    """An unreachable router must not be read as a honeypot."""
    wire(
        mint=mint_info(),
        holders=holder_accounts(0.3, 0.02),
        pairs=[pair()],
        quote=SourceError("jupiter unreachable"),
    )
    report = safety.check_token(MINT)

    assert report.sellability.sellable is None
    assert "sellability_unknown" in codes(report)
    assert "not_sellable" not in codes(report)
    assert report.degraded is True


def test_severe_price_impact_is_flagged(wire):
    wire(
        mint=mint_info(),
        holders=holder_accounts(0.3, 0.02),
        pairs=[pair()],
        quote=sell_quote(impact="0.25"),
    )
    report = safety.check_token(MINT)

    assert "high_sell_impact" in codes(report)
    assert report.sellability.price_impact_pct == 25.0


def test_probe_skipped_without_price(wire):
    wire(
        mint=mint_info(),
        holders=holder_accounts(0.3, 0.02),
        pairs=[],
        quote=sell_quote(),
    )
    report = safety.check_token(MINT)

    jupiter = next(s for s in report.sources if s.name == "jupiter")
    assert jupiter.status is SourceStatus.SKIPPED
    assert report.sellability.sellable is None


# --- market / pool selection --------------------------------------------------


def test_quote_only_mint_does_not_describe_counterparty(wire):
    """Regression: DexScreener returns pools where the mint is on either side.

    Before filtering on baseToken.address, a report for USDC described PUMP.
    """
    wire(
        mint=mint_info(),
        holders=holder_accounts(0.3, 0.02),
        pairs=[pair(base=OTHER_MINT), pair(base=OTHER_MINT, dex="orca")],
        quote=sell_quote(),
    )
    report = safety.check_token(MINT)

    assert report.market.symbol is None
    assert report.market.price_usd is None
    assert "quote_asset_only" in codes(report)


def test_dormant_deep_pool_is_not_primary(wire):
    """Regression: ranking pools by depth picked a pool with no transactions,
    which silently disabled the buy/sell-imbalance check."""
    dormant = pair(liquidity=2_000_000.0, volume_h24=0.0, buys=0, sells=0, dex="meteora")
    active = pair(liquidity=50_000.0, volume_h24=900_000.0, buys=120, sells=95)
    wire(
        mint=mint_info(),
        holders=holder_accounts(0.3, 0.02),
        pairs=[dormant, active],
        quote=sell_quote(),
    )
    report = safety.check_token(MINT)

    assert report.market.dex_id == "raydium"
    assert report.market.buys_h1 == 120
    # Exit depth still counts every pool, since a router can reach them all.
    assert report.market.liquidity_usd == 50_000.0
    assert report.market.liquidity_usd_total == 2_050_000.0


def test_thin_liquidity_judged_on_total_depth(wire):
    """Several shallow pools that together clear the floor should not be flagged."""
    wire(
        mint=mint_info(),
        holders=holder_accounts(0.3, 0.02),
        pairs=[pair(liquidity=4_000.0), pair(liquidity=4_000.0, dex="orca")],
        quote=sell_quote(),
    )
    report = safety.check_token(MINT)

    assert "liquidity_thin" not in codes(report)
    assert report.market.liquidity_usd_total == 8_000.0


def test_thin_liquidity_is_flagged(wire):
    wire(
        mint=mint_info(),
        holders=holder_accounts(0.3, 0.02),
        pairs=[pair(liquidity=500.0)],
        quote=sell_quote(),
    )
    report = safety.check_token(MINT)

    assert "liquidity_thin" in codes(report)


def test_bonding_curve_exit_verified_by_route(wire):
    """Pre-graduation launchpad tokens report no liquidity, which is correct.

    A routed sell at low impact is the stronger exit evidence, so the missing
    figure must not be penalised.
    """
    wire(
        mint=mint_info(),
        holders=holder_accounts(0.65, 0.03, 0.02),
        pairs=[pair(dex="pumpfun", liquidity=None, volume_h24=34_000.0)],
        quote=sell_quote(impact="0.008"),
    )
    report = safety.check_token(MINT)

    assert report.market.is_bonding_curve is True
    assert report.market.liquidity_usd_total is None
    assert "depth_from_route_only" in codes(report)
    assert "liquidity_thin" not in codes(report)
    assert report.risk_score == 0
    assert report.verdict is SafetyVerdict.SAFE


def test_no_depth_and_no_route_is_high_severity(wire):
    """With neither a depth figure nor a working route, exit is unevidenced."""
    wire(
        mint=mint_info(),
        holders=holder_accounts(0.65, 0.03),
        pairs=[pair(dex="pumpfun", liquidity=None)],
        quote=SourceError("jupiter unreachable"),
    )
    report = safety.check_token(MINT)

    assert "exit_depth_unverified" in codes(report)
    assert report.verdict is SafetyVerdict.CAUTION
    assert report.verdict is not SafetyVerdict.SAFE


def test_amm_pair_is_not_marked_bonding_curve(wire):
    wire(
        mint=mint_info(),
        holders=holder_accounts(0.3, 0.02),
        pairs=[pair(dex="raydium")],
        quote=sell_quote(),
    )
    report = safety.check_token(MINT)

    assert report.market.is_bonding_curve is False
    assert "depth_from_route_only" not in codes(report)


def test_missing_pool_is_flagged(wire):
    wire(mint=mint_info(), holders=holder_accounts(0.3), pairs=[], quote=sell_quote())
    report = safety.check_token(MINT)

    assert "no_pool" in codes(report)
    assert report.market.pair_count == 0


def test_brand_new_pool_is_flagged(wire):
    wire(
        mint=mint_info(),
        holders=holder_accounts(0.3, 0.02),
        pairs=[pair(age_min=1.0)],
        quote=sell_quote(),
    )
    report = safety.check_token(MINT)

    assert "pool_brand_new" in codes(report)
    assert report.market.pool_age_min is not None
    assert report.market.pool_age_min < 2


def test_buys_with_zero_sells_is_flagged(wire):
    wire(
        mint=mint_info(),
        holders=holder_accounts(0.3, 0.02),
        pairs=[pair(buys=80, sells=0)],
        quote=sell_quote(),
    )
    report = safety.check_token(MINT)

    assert "no_sells_observed" in codes(report)


def test_low_volume_zero_sells_is_not_flagged(wire):
    """Too few transactions to distinguish a honeypot from a quiet pool."""
    wire(
        mint=mint_info(),
        holders=holder_accounts(0.3, 0.02),
        pairs=[pair(buys=3, sells=0)],
        quote=sell_quote(),
    )
    report = safety.check_token(MINT)

    assert "no_sells_observed" not in codes(report)


# --- holder concentration -----------------------------------------------------


def test_pool_vault_excluded_from_concentration(wire):
    """The largest account is normally the AMM vault, not insider supply."""
    wire(
        mint=mint_info(),
        holders=holder_accounts(0.80, 0.01, 0.01, 0.01),
        pairs=[pair()],
        quote=sell_quote(),
    )
    report = safety.check_token(MINT)

    assert report.holders.top1_pct == 80.0
    assert report.holders.top10_pct == 83.0
    assert report.holders.top10_pct_ex_largest == 3.0
    assert "holders_concentrated" not in codes(report)


def test_insider_concentration_is_flagged(wire):
    wire(
        mint=mint_info(),
        holders=holder_accounts(0.10, 0.20, 0.20, 0.20, 0.15),
        pairs=[pair()],
        quote=sell_quote(),
    )
    report = safety.check_token(MINT)

    assert report.holders.top10_pct_ex_largest == 65.0
    assert "holders_concentrated" in codes(report)
    assert report.verdict is SafetyVerdict.CAUTION


# --- degradation --------------------------------------------------------------


def test_missing_mint_state_is_unknown(wire):
    """Without mint state we cannot judge control of the token at all."""
    wire(
        mint=SourceError("rpc down"),
        holders=holder_accounts(0.3, 0.02),
        pairs=[pair()],
        quote=sell_quote(),
    )
    report = safety.check_token(MINT)

    assert report.verdict is SafetyVerdict.UNKNOWN
    assert report.risk_score == 100
    assert report.blocking is True
    assert report.degraded is True


def test_missing_holder_data_caps_at_caution(wire):
    """A partially answered screen must never come back SAFE."""
    wire(
        mint=mint_info(),
        holders=SourceError("HTTP 429"),
        pairs=[pair()],
        quote=sell_quote(),
    )
    report = safety.check_token(MINT)

    assert report.verdict is SafetyVerdict.CAUTION
    assert report.degraded is True
    assert report.blocking is False
    assert "holders_unknown" in codes(report)


def test_unrankable_holder_base_is_information_not_a_gap(wire):
    """A mint with millions of holders cannot be ranked, and cannot be
    concentrated either. That is an answer, so it must not degrade the verdict."""
    wire(
        mint=mint_info(),
        holders=TooManyAccountsError("Too many accounts requested (10000000 pubkeys)"),
        pairs=[pair()],
        quote=sell_quote(),
    )
    report = safety.check_token(MINT)

    assert "holders_very_dispersed" in codes(report)
    assert "holders_unknown" not in codes(report)
    assert report.degraded is False
    assert report.risk_score == 0
    assert report.verdict is SafetyVerdict.SAFE

    holders_source = next(s for s in report.sources if s.name == "solana_rpc.holders")
    assert holders_source.status is SourceStatus.OK


def test_invalid_address_short_circuits(wire):
    wire(mint=mint_info(), holders=[], pairs=[], quote=sell_quote())
    report = safety.check_token("not-a-real-mint")

    assert report.verdict is SafetyVerdict.UNKNOWN
    assert report.risk_score == 100
    assert "invalid_mint" in codes(report)
    # Nothing should have been fetched.
    assert report.sources == []


def test_flags_are_ordered_by_weight(wire):
    wire(
        mint=mint_info(mint_authority="A", freeze_authority="B"),
        holders=SourceError("HTTP 429"),
        pairs=[pair()],
        quote=sell_quote(),
    )
    report = safety.check_token(MINT)

    points = [f.points for f in report.flags]
    assert points == sorted(points, reverse=True)


def test_score_is_capped_at_100(wire):
    wire(
        mint=mint_info(
            mint_authority="A",
            freeze_authority="B",
            extensions=[{"extension": "nonTransferable"}, {"extension": "transferHook"}],
            token_2022=True,
        ),
        holders=holder_accounts(0.1, 0.2, 0.2, 0.2, 0.2),
        pairs=[pair(liquidity=100.0)],
        quote=NoRouteError("no route"),
    )
    report = safety.check_token(MINT)

    assert report.risk_score == 100
    assert report.verdict is SafetyVerdict.DANGER
