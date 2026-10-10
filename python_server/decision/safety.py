"""Gate 0: on-chain safety screening for Solana tokens.

This is the cheapest gate and it runs first, because for memecoins the dominant
loss is not a bad price forecast, it is a token that was never exitable. One RPC
round trip decides whether the rest of the pipeline is worth running at all.

The score is an explicit heuristic, not a guarantee. It answers "is there a
disclosed on-chain mechanism that lets someone take my money", not "is this a
good trade". A clean report means no such mechanism was found in the checks
below; it does not mean the token is honest.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional, TypeVar

from decision.config import SETTINGS, SafetyThresholds
from decision.schema import (
    HolderConcentration,
    MarketSnapshot,
    MintAuthorities,
    SafetyFlag,
    SafetyReport,
    SafetyVerdict,
    Sellability,
    Severity,
    SourceResult,
    SourceStatus,
)
from decision.sources import (
    NoRouteError,
    SourceError,
    TooManyAccountsError,
    fetch_dexscreener_pairs,
    fetch_largest_accounts,
    fetch_mint_account,
    fetch_sell_quote,
)

BASE58_MINT = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

SELL_PROBE_USD = 20.0

# Launchpad venues where a token trades against a bonding curve before it
# graduates to an AMM. DexScreener reports null liquidity for these, which is
# correct rather than missing: there is no two-sided pool to measure yet.
BONDING_CURVE_DEXES = {
    "pumpfun",
    "moonshot",
    "launchlab",
    "boopfun",
    "believe",
    "bags",
    "heaven",
}

# Token-2022 extensions that hand the issuer power over holders after launch.
EXTENSION_RISKS: dict[str, tuple[Severity, int, str]] = {
    "nonTransferable": (
        Severity.CRITICAL,
        60,
        "Token is non-transferable: it can be bought but never sold",
    ),
    "permanentDelegate": (
        Severity.CRITICAL,
        55,
        "Permanent delegate can move tokens out of any wallet without consent",
    ),
    "transferHook": (
        Severity.CRITICAL,
        50,
        "Transfer hook runs issuer code on every transfer and can block selling",
    ),
    "pausableConfig": (
        Severity.HIGH,
        30,
        "Issuer can pause all transfers at will",
    ),
    "confidentialTransferMint": (
        Severity.MEDIUM,
        15,
        "Confidential transfers make holder flow unauditable",
    ),
    "mintCloseAuthority": (
        Severity.MEDIUM,
        10,
        "Mint can be closed by its close authority",
    ),
}

T = TypeVar("T")


class _Collector:
    """Accumulates flags and per-source outcomes for one screening run."""

    def __init__(self) -> None:
        self.flags: list[SafetyFlag] = []
        self.sources: list[SourceResult] = []

    def flag(
        self,
        code: str,
        severity: Severity,
        points: int,
        message: str,
        **evidence: Any,
    ) -> None:
        self.flags.append(
            SafetyFlag(
                code=code,
                severity=severity,
                points=points,
                message=message,
                evidence={k: v for k, v in evidence.items() if v is not None},
            )
        )

    def source(
        self,
        name: str,
        status: SourceStatus,
        detail: Optional[str] = None,
        latency_ms: Optional[int] = None,
    ) -> None:
        self.sources.append(
            SourceResult(name=name, status=status, detail=detail, latency_ms=latency_ms)
        )


def _timed(fn: Callable[[], T]) -> tuple[Optional[T], Optional[Exception], int]:
    start = time.perf_counter()
    try:
        return fn(), None, int((time.perf_counter() - start) * 1000)
    except Exception as exc:  # noqa: BLE001 - recorded and surfaced as degraded
        return None, exc, int((time.perf_counter() - start) * 1000)


def check_token(mint: str, thresholds: Optional[SafetyThresholds] = None) -> SafetyReport:
    """Screen a mint and return a typed, fully-explained safety report."""
    limits = thresholds or SETTINGS.thresholds
    started = time.perf_counter()
    mint = (mint or "").strip()

    if not BASE58_MINT.match(mint):
        collector = _Collector()
        collector.flag(
            "invalid_mint",
            Severity.CRITICAL,
            100,
            "Not a valid base58 Solana mint address",
            value=mint[:64],
        )
        return SafetyReport(
            mint=mint,
            verdict=SafetyVerdict.UNKNOWN,
            risk_score=100,
            degraded=True,
            flags=collector.flags,
            latency_ms=0,
        )

    collector = _Collector()

    # These three are independent; the wall clock is one round trip, not three.
    with ThreadPoolExecutor(max_workers=3) as pool:
        mint_future = pool.submit(_timed, lambda: fetch_mint_account(mint))
        holders_future = pool.submit(_timed, lambda: fetch_largest_accounts(mint))
        market_future = pool.submit(_timed, lambda: fetch_dexscreener_pairs(mint))

        mint_info, mint_error, mint_ms = mint_future.result()
        holder_accounts, holder_error, holder_ms = holders_future.result()
        pairs, market_error, market_ms = market_future.result()

    authorities = _read_authorities(mint_info, mint_error, mint_ms, collector, limits)
    holders = _read_holders(
        holder_accounts, holder_error, holder_ms, authorities, collector, limits
    )
    market = _read_market(mint, pairs, market_error, market_ms, collector, limits)
    sellability = _probe_sellability(mint, authorities, market, collector)
    # Judged last: a successful low-impact route is evidence of exit depth even
    # when no liquidity figure exists, so this needs the probe result.
    _judge_exit_depth(market, sellability, collector, limits)

    # Without mint state we know nothing about who controls the token, so the
    # only honest answer is "unknown" rather than a score built on two sources.
    if mint_info is None:
        return SafetyReport(
            mint=mint,
            verdict=SafetyVerdict.UNKNOWN,
            risk_score=100,
            degraded=True,
            flags=collector.flags,
            authorities=authorities,
            holders=holders,
            market=market,
            sellability=sellability,
            sources=collector.sources,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    score = min(100, sum(flag.points for flag in collector.flags))
    severities = {f.severity for f in collector.flags}
    degraded = any(s.status is SourceStatus.UNAVAILABLE for s in collector.sources)

    # Severity gates independently of the score. Points accumulate evidence, but
    # a single serious finding must stand on its own: one high-severity flag is
    # worth 25 points, which would otherwise fall below the caution threshold
    # and let an active freeze authority report as safe.
    if Severity.CRITICAL in severities or score >= limits.danger_score:
        verdict = SafetyVerdict.DANGER
    elif Severity.HIGH in severities or score >= limits.caution_score or degraded:
        verdict = SafetyVerdict.CAUTION
    else:
        verdict = SafetyVerdict.SAFE

    return SafetyReport(
        mint=mint,
        verdict=verdict,
        risk_score=score,
        degraded=degraded,
        flags=sorted(collector.flags, key=lambda f: -f.points),
        authorities=authorities,
        holders=holders,
        market=market,
        sellability=sellability,
        sources=collector.sources,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


def _read_authorities(
    info: Optional[dict[str, Any]],
    error: Optional[Exception],
    latency_ms: int,
    collector: _Collector,
    limits: SafetyThresholds,
) -> MintAuthorities:
    if info is None:
        collector.source(
            "solana_rpc.mint", SourceStatus.UNAVAILABLE, str(error), latency_ms
        )
        collector.flag(
            "mint_state_unknown",
            Severity.HIGH,
            0,
            f"Could not read mint state: {error}",
        )
        return MintAuthorities()

    collector.source("solana_rpc.mint", SourceStatus.OK, latency_ms=latency_ms)

    decimals = info.get("decimals")
    supply_raw = _as_int(info.get("supply"))
    supply_ui = (
        supply_raw / (10**decimals)
        if supply_raw is not None and isinstance(decimals, int)
        else None
    )

    extensions, transfer_fee_pct = _read_extensions(info, collector, limits)

    authorities = MintAuthorities(
        program=info.get("_program_owner"),
        is_token_2022=info.get("_is_token_2022"),
        mint_authority=info.get("mintAuthority"),
        freeze_authority=info.get("freezeAuthority"),
        decimals=decimals if isinstance(decimals, int) else None,
        supply_raw=supply_raw,
        supply_ui=supply_ui,
        transfer_fee_pct=transfer_fee_pct,
        extensions=extensions,
    )

    if authorities.mint_authority:
        collector.flag(
            "mint_authority_active",
            Severity.CRITICAL,
            45,
            "Mint authority is still active: supply can be inflated at any time",
            authority=authorities.mint_authority,
        )
    if authorities.freeze_authority:
        collector.flag(
            "freeze_authority_active",
            Severity.HIGH,
            25,
            "Freeze authority is still active: your token account can be frozen",
            authority=authorities.freeze_authority,
        )

    if not info.get("isInitialized", True):
        collector.flag(
            "mint_uninitialized", Severity.HIGH, 20, "Mint is not initialized"
        )

    return authorities


def _read_extensions(
    info: dict[str, Any], collector: _Collector, limits: SafetyThresholds
) -> tuple[list[str], Optional[float]]:
    raw_extensions = info.get("extensions") or []
    names: list[str] = []
    transfer_fee_pct: Optional[float] = None

    for entry in raw_extensions:
        if not isinstance(entry, dict):
            continue
        name = entry.get("extension")
        if not name:
            continue
        names.append(name)
        state = entry.get("state") or {}

        if name == "transferFeeConfig":
            transfer_fee_pct = _read_transfer_fee(state)
            if transfer_fee_pct is not None and transfer_fee_pct > limits.max_transfer_fee_pct:
                collector.flag(
                    "high_transfer_fee",
                    Severity.HIGH,
                    25,
                    f"Transfer fee of {transfer_fee_pct:.2f}% is taken on every trade",
                    transfer_fee_pct=transfer_fee_pct,
                )
            continue

        if name == "defaultAccountState":
            if str(state.get("accountState", "")).lower() == "frozen":
                collector.flag(
                    "default_frozen",
                    Severity.CRITICAL,
                    50,
                    "New holder accounts default to frozen and need issuer approval",
                )
            continue

        risk = EXTENSION_RISKS.get(name)
        if risk:
            severity, points, message = risk
            collector.flag(f"ext_{name}", severity, points, message)

    return names, transfer_fee_pct


def _read_transfer_fee(state: dict[str, Any]) -> Optional[float]:
    fee = state.get("newerTransferFee") or state.get("olderTransferFee") or {}
    bps = fee.get("transferFeeBasisPoints")
    return bps / 100.0 if isinstance(bps, (int, float)) else None


def _read_holders(
    accounts: Optional[list[dict[str, Any]]],
    error: Optional[Exception],
    latency_ms: int,
    authorities: MintAuthorities,
    collector: _Collector,
    limits: SafetyThresholds,
) -> HolderConcentration:
    if isinstance(error, TooManyAccountsError):
        # The RPC will not rank the accounts because there are millions of them.
        # That is itself the answer: a holder base that wide cannot be
        # concentrated, so this is information, not a gap in our evidence.
        collector.source(
            "solana_rpc.holders",
            SourceStatus.OK,
            "holder base too wide to rank",
            latency_ms,
        )
        collector.flag(
            "holders_very_dispersed",
            Severity.INFO,
            0,
            "Holder base is too large to rank, which rules out concentration risk",
            detail=str(error),
        )
        return HolderConcentration()

    if accounts is None:
        collector.source(
            "solana_rpc.holders", SourceStatus.UNAVAILABLE, str(error), latency_ms
        )
        collector.flag(
            "holders_unknown",
            Severity.MEDIUM,
            5,
            f"Could not read holder distribution: {error}",
        )
        return HolderConcentration()

    collector.source("solana_rpc.holders", SourceStatus.OK, latency_ms=latency_ms)

    supply = authorities.supply_raw
    if not supply:
        return HolderConcentration(accounts_seen=len(accounts))

    amounts = sorted(
        (a for a in (_as_int(acc.get("amount")) for acc in accounts) if a),
        reverse=True,
    )
    if not amounts:
        return HolderConcentration(accounts_seen=len(accounts))

    def pct(values: list[int]) -> float:
        return round(sum(values) / supply * 100, 2)

    top10 = pct(amounts[:10])
    # The biggest account is normally the AMM vault, which is not insider supply.
    top10_ex_largest = pct(amounts[1:11]) if len(amounts) > 1 else 0.0
    herfindahl = round(sum((a / supply) ** 2 for a in amounts), 4)

    holders = HolderConcentration(
        accounts_seen=len(accounts),
        top1_pct=pct(amounts[:1]),
        top5_pct=pct(amounts[:5]),
        top10_pct=top10,
        top10_pct_ex_largest=top10_ex_largest,
        herfindahl=herfindahl,
        largest_account=accounts[0].get("address"),
    )

    # Judge on the pool-excluded number so a healthy deep pool is not punished.
    if top10_ex_largest > limits.max_top10_pct:
        collector.flag(
            "holders_concentrated",
            Severity.HIGH,
            25,
            f"Top 10 non-pool accounts hold {top10_ex_largest:.1f}% of supply",
            top10_pct_ex_largest=top10_ex_largest,
            top10_pct=top10,
        )
    elif top10_ex_largest > limits.max_top10_pct * 0.7:
        collector.flag(
            "holders_elevated",
            Severity.MEDIUM,
            10,
            f"Top 10 non-pool accounts hold {top10_ex_largest:.1f}% of supply",
            top10_pct_ex_largest=top10_ex_largest,
        )

    return holders


def _read_market(
    mint: str,
    pairs: Optional[list[dict[str, Any]]],
    error: Optional[Exception],
    latency_ms: int,
    collector: _Collector,
    limits: SafetyThresholds,
) -> MarketSnapshot:
    if pairs is None:
        collector.source(
            "dexscreener", SourceStatus.UNAVAILABLE, str(error), latency_ms
        )
        collector.flag(
            "market_unknown",
            Severity.MEDIUM,
            10,
            f"Could not read pool data: {error}",
        )
        return MarketSnapshot()

    collector.source("dexscreener", SourceStatus.OK, latency_ms=latency_ms)

    if not pairs:
        collector.flag(
            "no_pool",
            Severity.HIGH,
            30,
            "No Solana liquidity pool found for this mint",
        )
        return MarketSnapshot(pair_count=0)

    # DexScreener returns pools where the mint is either side. Only pools where
    # it is the base token describe *its* price; in the others priceUsd and
    # symbol belong to the counterparty token.
    own_pairs = [p for p in pairs if (p.get("baseToken") or {}).get("address") == mint]
    if not own_pairs:
        collector.flag(
            "quote_asset_only",
            Severity.MEDIUM,
            10,
            "Mint only appears as the quote side of pools, so it has no own market here",
            pair_count=len(pairs),
        )
        return MarketSnapshot(pair_count=len(pairs))

    # Rank by traded volume, not by depth. The deepest pool is often a dormant
    # one whose transaction counts are all zero, which would silently disable
    # the buy/sell-imbalance check below.
    best = max(own_pairs, key=lambda p: _as_float((p.get("volume") or {}).get("h24")) or 0.0)
    liquidity = _as_float((best.get("liquidity") or {}).get("usd"))
    liquidity_total = sum(
        _as_float((p.get("liquidity") or {}).get("usd")) or 0.0 for p in own_pairs
    ) or None
    created_ms = best.get("pairCreatedAt")
    pool_age_min = (
        round((time.time() * 1000 - created_ms) / 60_000, 1)
        if isinstance(created_ms, (int, float))
        else None
    )
    txns_h1 = (best.get("txns") or {}).get("h1") or {}

    dex_id = best.get("dexId")
    market = MarketSnapshot(
        pair_address=best.get("pairAddress"),
        dex_id=dex_id,
        symbol=(best.get("baseToken") or {}).get("symbol"),
        is_bonding_curve=str(dex_id).lower() in BONDING_CURVE_DEXES,
        price_usd=_as_float(best.get("priceUsd")),
        liquidity_usd=liquidity,
        liquidity_usd_total=round(liquidity_total, 2) if liquidity_total else None,
        fdv=_as_float(best.get("fdv")),
        volume_h1=_as_float((best.get("volume") or {}).get("h1")),
        volume_h24=_as_float((best.get("volume") or {}).get("h24")),
        pool_age_min=pool_age_min,
        buys_h1=txns_h1.get("buys"),
        sells_h1=txns_h1.get("sells"),
        price_change_h1=_as_float((best.get("priceChange") or {}).get("h1")),
        pair_count=len(own_pairs),
    )

    if pool_age_min is not None and pool_age_min < limits.min_pool_age_min:
        collector.flag(
            "pool_brand_new",
            Severity.MEDIUM,
            15,
            f"Pool is {pool_age_min:.0f} min old, too young to judge",
            pool_age_min=pool_age_min,
        )

    # A pool that only absorbs buys is the classic pre-rug shape.
    buys, sells = market.buys_h1, market.sells_h1
    if isinstance(buys, int) and isinstance(sells, int) and buys + sells >= 30 and sells == 0:
        collector.flag(
            "no_sells_observed",
            Severity.HIGH,
            30,
            f"{buys} buys and zero sells in the last hour: possible honeypot",
            buys_h1=buys,
        )

    return market


def _judge_exit_depth(
    market: MarketSnapshot,
    sellability: Sellability,
    collector: _Collector,
    limits: SafetyThresholds,
) -> None:
    """Decide whether the position could actually be exited, and at what cost.

    Depth is judged on the total across pools, because a router reaches all of
    them. When no depth figure exists — normal for a pre-graduation bonding
    curve — a routed sell at low impact is the stronger evidence anyway, so it
    substitutes. Only the case where we have neither is penalised.
    """
    exit_depth = market.liquidity_usd_total or market.liquidity_usd

    if exit_depth is None:
        if market.pair_count == 0:
            return  # already covered by the no_pool flag

        if sellability.sellable is True:
            collector.flag(
                "depth_from_route_only",
                Severity.INFO,
                0,
                (
                    "No pool depth reported (bonding curve); exit verified by a routed sell"
                    if market.is_bonding_curve
                    else "No pool depth reported; exit verified by a routed sell"
                ),
                price_impact_pct=sellability.price_impact_pct,
            )
        else:
            collector.flag(
                "exit_depth_unverified",
                Severity.HIGH,
                25,
                "Neither pool depth nor a working sell route could be confirmed",
            )
        return

    if exit_depth < limits.min_liquidity_usd:
        collector.flag(
            "liquidity_thin",
            Severity.HIGH,
            25,
            f"Only ${exit_depth:,.0f} of liquidity, below the ${limits.min_liquidity_usd:,.0f} floor",
            liquidity_usd_total=exit_depth,
        )
    elif exit_depth < limits.min_liquidity_usd * 2:
        collector.flag(
            "liquidity_shallow",
            Severity.MEDIUM,
            10,
            f"${exit_depth:,.0f} of liquidity leaves little room to exit",
            liquidity_usd_total=exit_depth,
        )


def _probe_sellability(
    mint: str,
    authorities: MintAuthorities,
    market: MarketSnapshot,
    collector: _Collector,
) -> Sellability:
    """Ask Jupiter to route a small sell. The cheapest honeypot test available."""
    price = market.price_usd
    decimals = authorities.decimals

    if price is None or not price or decimals is None:
        collector.source(
            "jupiter",
            SourceStatus.SKIPPED,
            "needs price and decimals to size the probe",
        )
        return Sellability()

    amount_raw = int((SELL_PROBE_USD / price) * (10**decimals))
    if amount_raw <= 0:
        collector.source("jupiter", SourceStatus.SKIPPED, "probe size rounds to zero")
        return Sellability()

    quote, error, latency_ms = _timed(lambda: fetch_sell_quote(mint, amount_raw))

    if isinstance(error, NoRouteError):
        collector.source("jupiter", SourceStatus.OK, "no route", latency_ms)
        collector.flag(
            "not_sellable",
            Severity.CRITICAL,
            60,
            f"No route to sell ~${SELL_PROBE_USD:.0f} of this token",
            detail=str(error),
        )
        return Sellability(sellable=False, probe_usd=SELL_PROBE_USD)

    if quote is None:
        collector.source("jupiter", SourceStatus.UNAVAILABLE, str(error), latency_ms)
        collector.flag(
            "sellability_unknown",
            Severity.MEDIUM,
            10,
            f"Could not verify the token is sellable: {error}",
        )
        return Sellability(probe_usd=SELL_PROBE_USD)

    collector.source("jupiter", SourceStatus.OK, latency_ms=latency_ms)

    impact = _as_float(quote.get("priceImpactPct"))
    impact_pct = round(impact * 100, 3) if impact is not None else None
    route = quote.get("routePlan") or []
    label = None
    if route and isinstance(route[0], dict):
        label = ((route[0].get("swapInfo") or {}).get("label"))

    if impact_pct is not None and impact_pct > 10:
        collector.flag(
            "high_sell_impact",
            Severity.HIGH,
            25,
            f"Selling ~${SELL_PROBE_USD:.0f} already moves the price {impact_pct:.1f}%",
            price_impact_pct=impact_pct,
        )
    elif impact_pct is not None and impact_pct > 3:
        collector.flag(
            "moderate_sell_impact",
            Severity.MEDIUM,
            10,
            f"Selling ~${SELL_PROBE_USD:.0f} moves the price {impact_pct:.1f}%",
            price_impact_pct=impact_pct,
        )

    return Sellability(
        sellable=True,
        price_impact_pct=impact_pct,
        route_label=label,
        probe_usd=SELL_PROBE_USD,
    )


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
