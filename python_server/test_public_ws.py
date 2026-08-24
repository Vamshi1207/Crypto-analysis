import asyncio
import websockets
import json

async def subscribe():
    uri = "wss://api.mainnet-beta.solana.com/"
    async with websockets.connect(uri) as websocket:
        await websocket.send(json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": ["8AQnp6MJPm7oVtBgyhxUodwmUW7ehyFZEZppbftMwwt7"]},
                {"commitment": "processed"}
            ]
        }))
        print("Connected!")
        try:
            msg = await asyncio.wait_for(websocket.recv(), timeout=5.0)
            print(msg)
            msg2 = await asyncio.wait_for(websocket.recv(), timeout=2.0)
            print("Received a log!")
        except Exception as e:
            print("Error:", e)

asyncio.run(subscribe())
