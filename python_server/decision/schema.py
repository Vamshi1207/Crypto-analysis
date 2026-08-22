"""Typed contracts for the decision engine.

Every field that can be unknown is Optional and defaults to None. A missing
value must never be silently coerced to a passing value: an unavailable source
degrades the verdict instead of quietly approving the token.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class SafetyVerdict(str, Enum):
    SAFE = "safe"
    CAUTION = "caution"
    DANGER = "danger"
    UNKNOWN = "unknown"


class SourceStatus(str, Enum):
    OK = "ok"
    UNAVAILABLE = "unavailable"
    SKIPPED = "skipped"


class SafetyFlag(BaseModel):
    code: str
    severity: Severity
    message: str
    points: int = 0
    evidence: dict[str, Any] = Field(default_factory=dict)


class SourceResult(BaseModel):
    name: str
    status: SourceStatus
    detail: Optional[str] = None
    latency_ms: Optional[int] = None


class MintAuthorities(BaseModel):
    """Who can still change the token's rules after launch."""

    program: Optional[str] = None
    is_token_2022: Optional[bool] = None
    mint_authority: Optional[str] = None
    freeze_authority: Optional[str] = None
    decimals: Optional[int] = None
    supply_raw: Optional[int] = None
    supply_ui: Optional[float] = None
    transfer_fee_pct: Optional[float] = None
    extensions: list[str] = Field(default_factory=list)


class HolderConcentration(BaseModel):
    """Distribution of supply across the largest token accounts.

    `getTokenLargestAccounts` returns token accounts, not wallet owners, and the
    AMM pool vault is usually the single biggest one. `top10_pct_ex_largest`
    exists so pool-held supply does not read as insider concentration.
    """

    accounts_seen: Optional[int] = None
    top1_pct: Optional[float] = None
    top5_pct: Optional[float] = None
    top10_pct: Optional[float] = None
    top10_pct_ex_largest: Optional[float] = None
    herfindahl: Optional[float] = None
    largest_account: Optional[str] = None


class MarketSnapshot(BaseModel):
    pair_address: Optional[str] = None
    dex_id: Optional[str] = None
    symbol: Optional[str] = None
    # Pre-graduation launchpad tokens trade against a bonding curve rather than
    # a two-sided pool, so they legitimately report no liquidity figure.
    is_bonding_curve: Optional[bool] = None
    price_usd: Optional[float] = None
    # Depth of the primary pool. `liquidity_usd_total` is depth across every
    # pool for this mint, which is what an aggregator can actually route into.
    liquidity_usd: Optional[float] = None
    liquidity_usd_total: Optional[float] = None
    fdv: Optional[float] = None
    volume_h1: Optional[float] = None
    volume_h24: Optional[float] = None
    pool_age_min: Optional[float] = None
    buys_h1: Optional[int] = None
    sells_h1: Optional[int] = None
    price_change_h1: Optional[float] = None
    pair_count: Optional[int] = None


class Sellability(BaseModel):
    """Honeypot proxy: can a small position actually be routed back out?"""

    sellable: Optional[bool] = None
    price_impact_pct: Optional[float] = None
    route_label: Optional[str] = None
    probe_usd: Optional[float] = None


class SafetyReport(BaseModel):
    mint: str
    verdict: SafetyVerdict
    risk_score: int = Field(ge=0, le=100)
    degraded: bool = False
    flags: list[SafetyFlag] = Field(default_factory=list)
    authorities: MintAuthorities = Field(default_factory=MintAuthorities)
    holders: HolderConcentration = Field(default_factory=HolderConcentration)
    market: MarketSnapshot = Field(default_factory=MarketSnapshot)
    sellability: Sellability = Field(default_factory=Sellability)
    sources: list[SourceResult] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    latency_ms: Optional[int] = None

    @property
    def blocking(self) -> bool:
        """True when Gate 0 should stop the pipeline before spending on inference."""
        return self.verdict in (SafetyVerdict.DANGER, SafetyVerdict.UNKNOWN)

    def summary(self) -> str:
        top = [f for f in self.flags if f.severity in (Severity.CRITICAL, Severity.HIGH)]
        if not top:
            return f"{self.verdict.value} (score {self.risk_score})"
        reasons = "; ".join(f.message for f in top[:3])
        return f"{self.verdict.value} (score {self.risk_score}): {reasons}"


# --- Phase 2: forecast + decision -------------------------------------------------


class Action(str, Enum):
    BUY = "buy"
    HOLD = "hold"
    AVOID = "avoid"
    REDUCE = "reduce"


class Direction(str, Enum):
    UP = "up"
    DOWN = "down"
    SIDEWAYS = "sideways"


class DecideMode(str, Enum):
    FAST = "fast"
    FULL = "full"
    DEEP = "deep"


class ReturnBand(BaseModel):
    """Calibrated expected move over the decision horizon, in percent."""

    p10: float
    p50: float
    p90: float


class PriceTargets(BaseModel):
    entry: Optional[float] = None
    upside: Optional[float] = None
    downside: Optional[float] = None
    invalidation: Optional[float] = None


class PositionPlan(BaseModel):
    max_size_usd: float = 0.0
    size_basis: str = "liquidity_capped"


class RiskBlock(BaseModel):
    risk_pass: bool = True
    max_loss_pct: float = 0.0
    veto_reasons: list[str] = Field(default_factory=list)


class Driver(BaseModel):
    source: str
    claim: str
    weight: float = 0.0


class ModelForecast(BaseModel):
    name: str
    available: bool = True
    p10: Optional[float] = None
    p50: Optional[float] = None
    p90: Optional[float] = None
    detail: Optional[str] = None
    latency_ms: Optional[int] = None


class ForecastEnsemble(BaseModel):
    horizon_bars: int
    timeframe: str
    coverage_target: float = 0.8
    raw: ReturnBand
    calibrated: ReturnBand
    agreement: float = Field(ge=0.0, le=1.0, description="1 = models agree on direction and scale")
    models: list[ModelForecast] = Field(default_factory=list)
    residual_count: int = 0
    backend: str = "stat"
    calibration_basis: str = "raw"


class TimeframePacket(BaseModel):
    timeframe: str
    candle_count: int = 0
    ohlcv_tail: list[dict[str, float]] = Field(default_factory=list)
    indicators: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


class MarketPacket(BaseModel):
    """Evidence bundle agents and forecast models may cite. No invented fields."""

    address: str
    mint: Optional[str] = None
    name: Optional[str] = None
    as_of: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    price: Optional[float] = None
    timeframes: dict[str, TimeframePacket] = Field(default_factory=dict)
    orderflow: dict[str, Any] = Field(default_factory=dict)
    safety: Optional[SafetyReport] = None
    market: MarketSnapshot = Field(default_factory=MarketSnapshot)
    est_slippage_bps: Optional[float] = None
    est_round_trip_cost_pct: Optional[float] = None
    source: str = "live"  # live | corpus


class DecisionCard(BaseModel):
    """Final contract for UI, shadow log, and (later) execution."""

    token: dict[str, Any]
    horizon_bars: int
    timeframe: str
    action: Action
    action_confidence: float = Field(ge=0.0, le=1.0)
    confidence_basis: str
    direction: Direction
    expected_return_pct: ReturnBand
    interval_coverage_target: float = 0.8
    cost_adjusted_edge_pct: float = 0.0
    price_targets: PriceTargets = Field(default_factory=PriceTargets)
    position: PositionPlan = Field(default_factory=PositionPlan)
    safety: dict[str, Any] = Field(default_factory=dict)
    risk: RiskBlock = Field(default_factory=RiskBlock)
    forecast: ForecastEnsemble
    drivers: list[Driver] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    gates_passed: list[str] = Field(default_factory=list)
    gates_failed: list[str] = Field(default_factory=list)
    mode: DecideMode = DecideMode.FAST
    degraded: bool = False
    latency_ms: Optional[int] = None
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    langfuse_trace_id: Optional[str] = None

    def summary(self) -> str:
        band = self.expected_return_pct
        return (
            f"{self.action.value} ({self.action_confidence:.0%}) "
            f"{self.direction.value} p50={band.p50:+.2f}% "
            f"[{band.p10:+.2f}%, {band.p90:+.2f}%] "
            f"edge={self.cost_adjusted_edge_pct:+.2f}%"
        )
