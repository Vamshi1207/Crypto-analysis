"""
Fast execution pipeline for Sniper Mode.
Bypasses LLM forecasts and indicators for sub-millisecond paper trading.
"""
import time
import logging
from decision import paper
from decision import pipeline_log

logger = logging.getLogger(__name__)

# Rigid fast-path constraints
FAST_SNIPE_SIZE_USD = 20.0
FAST_SNIPE_TP_PCT = 1.50  # 50% profit
FAST_SNIPE_SL_PCT = 0.80  # 20% loss
FAST_SNIPE_HOLD_SEC = 45.0

def execute_fast_snipe(mint: str, current_price: float, liquidity: float, source: str = "grpc_sniper"):
    """
    Executes a paper buy immediately without forecasting.
    """
    if liquidity < 1500:
        logger.debug(f"Fast snipe skipped {mint}: Liquidity {liquidity} too low")
        return False
        
    try:
        pipeline_log.emit("fast_execute", "buy_attempt", mint=mint, price=current_price)
        
        # Open a paper trade in the 'snipe' lane with hardcoded limits
        success = paper.buy(
            mint=mint,
            size_usd=FAST_SNIPE_SIZE_USD,
            price=current_price,
            lane="snipe",
            why=source,
            limits={
                "target_profit_usd": FAST_SNIPE_SIZE_USD * (FAST_SNIPE_TP_PCT - 1.0),
                "stop_loss_usd": FAST_SNIPE_SIZE_USD * (1.0 - FAST_SNIPE_SL_PCT),
                "stop_loss_pct": (1.0 - FAST_SNIPE_SL_PCT) * 100,
                "trail_enabled": True,
                "trail_arm_usd": FAST_SNIPE_SIZE_USD * 0.2, # Arm trail early
                "trail_giveback_pct": 0.5
            }
        )
        
        if success:
            pipeline_log.emit("fast_execute", "buy_success", mint=mint, price=current_price)
            return True
            
        return False
        
    except Exception as e:
        logger.error(f"Failed to execute fast snipe for {mint}: {e}")
        pipeline_log.emit("fast_execute", "buy_error", mint=mint, error=str(e))
        return False
