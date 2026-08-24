import grpc
import os
import logging
from typing import Iterator, Dict, Any

from . import geyser_pb2
from . import geyser_pb2_grpc

logger = logging.getLogger(__name__)

class YellowstoneStream:
    """
    Yellowstone gRPC stream client for low-latency Solana mempool/account updates.
    """
    def __init__(self, endpoint: str, x_token: str = None):
        self.endpoint = endpoint
        self.x_token = x_token
        self.channel = None
        self.stub = None

    def connect(self):
        """Establish the gRPC connection."""
        if not self.endpoint:
            logger.warning("No gRPC endpoint configured. Yellowstone stream disabled.")
            return

        logger.info(f"Connecting to Yellowstone gRPC endpoint: {self.endpoint}")
        
        # In a real environment with TLS, use secure_channel
        if self.endpoint.startswith("https://") or self.endpoint.endswith(":443"):
            credentials = grpc.ssl_channel_credentials()
            # Remove https:// prefix if present
            target = self.endpoint.replace("https://", "")
            self.channel = grpc.secure_channel(target, credentials)
        else:
            target = self.endpoint.replace("http://", "")
            self.channel = grpc.insecure_channel(target)
            
        self.stub = geyser_pb2_grpc.GeyserStub(self.channel)
        
    def _get_metadata(self):
        if self.x_token:
            return (('x-token', self.x_token),)
        return None

    def subscribe_accounts(self, accounts: list[str]) -> Iterator[Any]:
        """Subscribe to account updates for specific public keys."""
        if not self.stub:
            raise ConnectionError("Not connected to gRPC endpoint")
            
        request = geyser_pb2.SubscribeRequest()
        
        # Setup account subscription
        request.accounts["account_sub"].account.extend(accounts)
        
        logger.info(f"Subscribing to accounts: {accounts}")
        
        try:
            # Yield from the stream
            responses = self.stub.Subscribe(iter([request]), metadata=self._get_metadata())
            for response in responses:
                yield response
        except grpc.RpcError as e:
            logger.error(f"gRPC Stream error: {e}")
            raise

    def subscribe_programs(self, programs: list[str]) -> Iterator[Any]:
        """Subscribe to all account updates owned by specific programs."""
        if not self.stub:
            raise ConnectionError("Not connected to gRPC endpoint")
            
        request = geyser_pb2.SubscribeRequest()
        
        # Setup program owner subscription
        request.accounts["program_sub"].owner.extend(programs)
        
        logger.info(f"Subscribing to programs: {programs}")
        
        try:
            responses = self.stub.Subscribe(iter([request]), metadata=self._get_metadata())
            for response in responses:
                yield response
        except grpc.RpcError as e:
            logger.error(f"gRPC Stream error: {e}")
            raise

    def subscribe_transactions(self, programs: list[str]) -> Iterator[Any]:
        """Subscribe to transactions mentioning specific programs."""
        if not self.stub:
            raise ConnectionError("Not connected to gRPC endpoint")
            
        request = geyser_pb2.SubscribeRequest()
        
        # Setup transaction subscription
        request.transactions["tx_sub"].account_include.extend(programs)
        # Optional: request.transactions["tx_sub"].commitment = 1 # processed
        
        logger.info(f"Subscribing to transactions for programs: {programs}")
        
        try:
            responses = self.stub.Subscribe(iter([request]), metadata=self._get_metadata())
            for response in responses:
                yield response
        except grpc.RpcError as e:
            logger.error(f"gRPC Stream error: {e}")
            raise

    def close(self):
        if self.channel:
            self.channel.close()
            self.channel = None
            self.stub = None
