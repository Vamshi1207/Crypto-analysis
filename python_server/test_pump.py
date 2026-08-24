import asyncio
import websockets
import json
import time

async def subscribe():
    uri = "wss://pumpportal.fun/api/data"
    async with websockets.connect(uri) as websocket:
        # Subscribe to new token creations
        await websocket.send(json.dumps({"method": "subscribeNewToken"}))
        # Subscribe to trades
        await websocket.send(json.dumps({"method": "subscribeTokenTrade"}))
        
        print("Connected and subscribed!")
        start = time.time()
        while time.time() - start < 5:
            try:
                msg = await asyncio.wait_for(websocket.recv(), timeout=2.0)
                print(msg)
            except asyncio.TimeoutError:
                continue

asyncio.run(subscribe())
