import asyncio
import websockets
import json
import time

async def subscribe():
    uri = "wss://pumpdev.io/ws"
    async with websockets.connect(uri) as websocket:
        # Let's try to subscribe to trades or creates. They usually have a method
        await websocket.send(json.dumps({"method": "subscribeTrades"}))
        await websocket.send(json.dumps({"method": "subscribeNewToken"}))
        
        print("Connected to pumpdev!")
        start = time.time()
        while time.time() - start < 10:
            try:
                msg = await asyncio.wait_for(websocket.recv(), timeout=2.0)
                print(msg)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print("Error:", e)
                break

asyncio.run(subscribe())
