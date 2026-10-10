"""
gRPC Fast-Path Sniper for Pump.fun creates.
Bypasses the websocket ingest and routes directly to fast_execute.
"""
import threading
import logging
import os
import base64
from typing import Optional, Any
from decision.grpc_client.stream import YellowstoneStream
from decision.pumpfun import PUMP_PROGRAM, parse_create_event
from decision import fast_execute
from decision import pipeline_log

logger = logging.getLogger(__name__)

_stop = threading.Event()
_thread: Optional[threading.Thread] = None

def start():
    """Start the gRPC Sniper in a background thread."""
    global _thread
    
    geyser_endpoint = os.getenv("GEYSER_ENDPOINT")
    geyser_token = os.getenv("GEYSER_TOKEN")
    
    # Fallback to Helius gRPC if they have a standard Helius key
    if not geyser_endpoint and os.getenv("HELIUS_API_KEY"):
        geyser_endpoint = "https://mainnet.helius-rpc.com:2083"
        geyser_token = os.getenv("HELIUS_API_KEY")

    if not geyser_endpoint:
        logger.warning("grpc_sniper: No GEYSER_ENDPOINT. Fast-Path disabled.")
        return

    if _thread and _thread.is_alive():
        return
        
    _stop.clear()
    _thread = threading.Thread(
        target=_run_sniper,
        args=(geyser_endpoint, geyser_token),
        name="grpc-sniper",
        daemon=True
    )
    _thread.start()
    pipeline_log.emit("grpc_sniper", "start")
    logger.info("grpc_sniper: Fast-Path background thread started.")


def stop():
    """Stop the gRPC Sniper."""
    _stop.set()
    pipeline_log.emit("grpc_sniper", "stop")


def _run_sniper(endpoint: str, token: str):
    """Background loop for consuming the gRPC stream."""
    stream = YellowstoneStream(endpoint=endpoint, x_token=token)
    try:
        stream.connect()
        logger.info(f"grpc_sniper: Subscribing to transactions for {PUMP_PROGRAM}")
        
        # Subscribe to all transactions involving Pump.fun
        for update in stream.subscribe_transactions([PUMP_PROGRAM]):
            if _stop.is_set():
                break
                
            # Drill down into the gRPC SubscribeUpdate message
            if update.HasField("transaction"):
                tx_info = update.transaction
                _process_transaction(tx_info)
                
    except Exception as e:
        logger.error(f"grpc_sniper: Stream error: {e}")
        pipeline_log.emit("grpc_sniper", "error", error=str(e))
    finally:
        stream.close()


def _process_transaction(tx_info: Any):
    """Parse transaction logs and execute if it's a new pool creation."""
    # Check if transaction was successful
    if tx_info.transaction.meta.err:
        return
        
    logs = tx_info.transaction.meta.log_messages
    if not logs:
        return
        
    # Standard check for Pump.fun creation
    create = False
    for line in logs:
        low = line.lower()
        if "instruction: create" in low and "idempotent" not in low:
            create = True
            continue
        if not create:
            continue
            
        marker = "Program data: "
        if marker not in line:
            continue
            
        payload = line.split(marker, 1)[-1].strip()
        try:
            raw = base64.b64decode(payload)
            parsed = parse_create_event(raw)
            if parsed:
                mint = parsed.get("mint")
                if mint:
                    logger.info(f"grpc_sniper: Detected fast creation of {mint}. Executing!")
                    pipeline_log.emit("grpc_sniper", "detected", mint=mint)
                    # Instant Execution Bypass!
                    # Start with default price of Pump.fun bonding curves
                    fast_execute.execute_fast_snipe(
                        mint=mint,
                        current_price=0.00003, # standard initial virtual SOL/TOKEN price 
                        liquidity=85.0 * 150.0, # initial liquidity approx 85 SOL @ 150 USD
                        source="grpc_sniper"
                    )
                return
        except Exception:
            continue
