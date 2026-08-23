"""Environment-backed configuration for the decision engine."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

PUBLIC_SOLANA_RPC = "https://api.mainnet-beta.solana.com"
WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, default))


def paper_risk_on() -> bool:
    """Paper-only: take more trades so we learn the real drawdown.

    Never applies when ``LIVE_TRADING=1``. Default on for paper so a fresh
    session actually fills instead of sitting on a two-token roster.
    """
    if os.getenv("LIVE_TRADING", "0").strip() == "1":
        return False
    return os.getenv("PAPER_RISK_ON", "0").strip() == "1"


@dataclass(frozen=True)
class SafetyThresholds:
    """Tunable limits for Gate 0. Deliberately conservative for memecoins."""

    min_liquidity_usd: float = field(
        default_factory=lambda: _env_float("SAFETY_MIN_LIQUIDITY_USD", 5_000.0)
    )
    max_top10_pct: float = field(
        default_factory=lambda: _env_float("SAFETY_MAX_TOP10_PCT", 55.0)
    )
    min_pool_age_min: float = field(
        default_factory=lambda: _env_float("SAFETY_MIN_POOL_AGE_MIN", 5.0)
    )
    max_transfer_fee_pct: float = field(
        default_factory=lambda: _env_float("SAFETY_MAX_TRANSFER_FEE_PCT", 5.0)
    )
    danger_score: int = field(default_factory=lambda: _env_int("SAFETY_DANGER_SCORE", 60))
    caution_score: int = field(default_factory=lambda: _env_int("SAFETY_CAUTION_SCORE", 30))


@dataclass(frozen=True)
class Settings:
    solana_rpc_url: str
    helius_api_key: str
    birdeye_api_key: str
    agy_model: str
    agy_binary: str
    nvidia_key: str
    nvidia_base_url: str
    nemotron_model: str
    ollama_host: str
    ollama_model: str
    live_trading: bool
    http_timeout_s: float
    thresholds: SafetyThresholds

    @property
    def rpc_endpoint(self) -> str:
        """Helius free tier is far more reliable than the public RPC."""
        if self.helius_api_key:
            return f"https://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"
        return self.solana_rpc_url or PUBLIC_SOLANA_RPC

    @property
    def using_public_rpc(self) -> bool:
        return not self.helius_api_key and not self.solana_rpc_url


def load_settings() -> Settings:
    return Settings(
        solana_rpc_url=os.getenv("SOLANA_RPC_URL", "").strip(),
        helius_api_key=os.getenv("HELIUS_API_KEY", "").strip(),
        birdeye_api_key=os.getenv("BIRDEYE_API_KEY", "").strip(),
        # Empty = let agy use its default model for the signed-in account.
        agy_model=os.getenv("AGY_MODEL", os.getenv("GEMINI_MODEL", "")).strip(),
        agy_binary=os.getenv("AGY_CLI_PATH", "agy").strip() or "agy",
        nvidia_key=os.getenv("NVIDIA_KEY", "").strip(),
        nvidia_base_url=os.getenv(
            "NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"
        ).strip(),
        nemotron_model=os.getenv("NEMOTRON_MODEL", "nvidia/nemotron-3-nano-30b-a3b").strip(),
        ollama_host=os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434").strip(),
        ollama_model=os.getenv("OLLAMA_MODEL", "qwen2.5:14b").strip(),
        live_trading=os.getenv("LIVE_TRADING", "0").strip() == "1",
        http_timeout_s=_env_float("HTTP_TIMEOUT_S", 12.0),
        thresholds=SafetyThresholds(),
    )


SETTINGS = load_settings()
