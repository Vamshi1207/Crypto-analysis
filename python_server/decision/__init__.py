"""Decision engine: turns live token data into a typed, gated trading decision.

Gate order is deliberate and runs cheapest-first:

    Gate 0  safety        on-chain rug / honeypot / concentration screening
    Gate 1  tradability   liquidity, slippage, history sufficiency
    Gate 2  edge          forecast must beat fees + tip + slippage
    Gate 3  risk          hard veto from the risk committee (Phase 3)
    Gate 4  limits        portfolio caps and kill switch (Phase 4)

No model is trained anywhere in this package. See plans/implementation_plan.md.
"""

from decision.schema import DecisionCard, SafetyReport, SafetyVerdict

__all__ = ["DecisionCard", "SafetyReport", "SafetyVerdict"]
