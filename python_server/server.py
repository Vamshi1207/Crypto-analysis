from flask import Flask, request, jsonify, render_template, make_response
from flask_sock import Sock
from datetime import datetime
import traceback
from analysis import analyze_token
from indicators import get_indicators_for_token
from pathlib import Path
import json
import os

app = Flask(__name__)
sock = Sock(app)
token_data = {}  

@app.route('/')
def dashboard():
    return render_template("dashboard.html")

@app.route('/data')
def data():

    tf = request.args.get("tf", "1S")
    tf = tf.upper() 
    result = {}

    for addr, token in token_data.items():        
        candles = token["timeframes"].get(tf)
        if not candles or len(candles) == 0:
            result[addr] = {
                "name": token["name"],
                "error": f"Not enough candles in timeframe '{tf}'. Got {len(candles) if candles else 0}."
            }
            continue

        try:
            indicators = get_indicators_for_token(candles)

            latest = candles[-1]
            price_data = {
                "open": latest.get("open"),
                "high": latest.get("high"),
                "low": latest.get("low"),
                "close": latest.get("close"),
                "volume": latest.get("volume"),
                "timestamp": latest.get("timestamp")
            }

            analysis_result = analyze_token({
                **indicators,
                "close": price_data["close"],
                "highs": [c['high'] for c in candles],
                "lows": [c['low'] for c in candles],
                "volumes": [c['volume'] for c in candles]
            })

            result[addr] = {
                "name": token["name"],
                **price_data,
                **indicators,
                "analysis": analysis_result
            }
            
            # ✅ Logging to file
            # print(f"✅ Logged {token['name']} - {tf} timeframe")
            # log_entry = {
            #     "name": token["name"],
            #     "address": addr,
            #     "timeframe": tf,
            #     **price_data,
            #     **indicators,
            #     "analysis": analysis_result
            # }
            #
            # log_dir = r"C:\Users\vamsh\Downloads\TA MV2\Training data"
            # os.makedirs(log_dir, exist_ok=True)  # Ensure dir exists
            # log_path = Path(log_dir) / "training_data.jsonl"
            # with open(log_path, "a", encoding="utf-8") as f:
            #     f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

        except Exception as e:
            error_trace = traceback.format_exc()
            result[addr] = {
                "name": token["name"],
                "error": f"{type(e).__name__}: {str(e)}",
                "traceback": error_trace
            }

    return jsonify(result)


DATA_DIR_CANDLES = Path(r"C:\Users\vamsh\Downloads\TA MV2\python_server\ML_Training_datasets\CandleData\Candles")
DATA_DIR_STATS = Path(r"C:\Users\vamsh\Downloads\TA MV2\python_server\ML_Training_datasets\CandleData\Stats")
DATA_DIR_CANDLES.mkdir(parents=True, exist_ok=True)
DATA_DIR_STATS.mkdir(parents=True, exist_ok=True)


def _process_payload(data):
    payloadID = data.get("id", None)
    payload_data = data.get("candles", {})
    if isinstance(payload_data, dict) and "candles" in payload_data:
        candles_by_tf = payload_data.get("candles", {})
        stats_by_bucket = payload_data.get("stats", [])
    else:
        candles_by_tf = payload_data if isinstance(payload_data, dict) else {}
        stats_by_bucket = data.get("stats", [])
    token = data.get("token", {})
    address = token.get("address", "unknown")
    name = token.get("name", "Unknown")
    is_initial_data = bool(data.get("initial", False))

    #print("Candle", candles_by_tf)
    
    if payloadID:
        if not address or address == "unknown":
            return {"status": "error", "message": "Missing token address"}, False

        # Initialize for new token
        if address not in token_data or is_initial_data:
            token_data[address] = {
                "name": name,
                "timeframes": {
                    "1S": [], 
                    "5S": [], 
                    "15S": [], 
                    "30S": [], 
                    "1": [], 
                    "3": [], 
                    "5": []      
                },
                "stats": [],
                "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            
        tf = token_data[address]["timeframes"]

        for tf_key in tf.keys():
            if tf_key in candles_by_tf:
                new_candles = list(candles_by_tf[tf_key].values())
                print(f"  ⏱️ {tf_key}: {len(new_candles)} new candles")
                # Deduplicate by timestamp
                existing = {c["timestamp"]: c for c in tf[tf_key]}
                for c in new_candles:
                    existing[c["timestamp"]] = c
                tf[tf_key] = list(sorted(existing.values(), key=lambda x: x["timestamp"]))

                # Keep max N candles per timeframe (3 hours worth)
                tf_seconds = timeframe_to_seconds(tf_key)
                max_len = 18000 // tf_seconds
                tf[tf_key] = tf[tf_key][-max_len:]

        if isinstance(stats_by_bucket, list):
            token_data[address]["stats"] = stats_by_bucket
        
        
        #process_new_candles(address, candles)    
        token_data[address]["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print("Token data updated at:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        token_snapshot = token_data[address]

        candle_file = DATA_DIR_CANDLES / f"{address}_candles.json"
        stats_file = DATA_DIR_STATS / f"{address}_stats.json"

        with candle_file.open("w", encoding="utf-8") as f:
            json.dump({
                "name": token_snapshot["name"],
                "address": address,
                "timeframes": token_snapshot["timeframes"],
                "updated": token_snapshot["updated"]
            }, f, indent=2, ensure_ascii=False)

        with stats_file.open("w", encoding="utf-8") as f:
            json.dump({
                "name": token_snapshot["name"],
                "address": address,
                "stats": token_snapshot.get("stats", []),
                "updated": token_snapshot["updated"]
            }, f, indent=2, ensure_ascii=False)


        #print(f"📥 Received {len(candles_by_tf)} candles for {name} ({address})")
        return {"status": "ok"}, True
    
    # Always return a response if payloadID is missing
    return {"status": "error", "message": "Missing or invalid payload ID"}, False


@app.route('/receive', methods=["POST", "OPTIONS"])
def receive():
    if request.method == "OPTIONS":
        return _build_cors_preflight_response()

    data = request.get_json()
    result, success = _process_payload(data)
    status_code = 200 if success else 400
    return jsonify(result), status_code


@sock.route('/ws')
def websocket(ws):
    while True:
        message = ws.receive()
        if message is None:
            break

        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            ws.send(json.dumps({"status": "error", "message": "Invalid JSON"}))
            continue

        result, _success = _process_payload(payload)
        ws.send(json.dumps(result))

def timeframe_to_seconds(tf_key):
    if tf_key.endswith("S"):
        return int(tf_key[:-1])  # e.g., "15S" -> 15
    else:
        return int(tf_key) * 60  # e.g., "1", "3", "5" -> 60, 180, 300


@app.after_request
def cors_headers(resp):
    resp.headers['Access-Control-Allow-Origin'] = 'https://axiom.trade' # Better than '*' for security
    resp.headers['Access-Control-Allow-Headers'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = '*'
    # Add this specific header for Private Network Access
    resp.headers['Access-Control-Allow-Private-Network'] = 'true'
    return resp

def _build_cors_preflight_response():
    response = make_response()
    response.headers["Access-Control-Allow-Origin"] = "https://axiom.trade"
    response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Access-Control-Allow-Private-Network"
    # This is the "magic" key that unlocks the loopback space
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response

if __name__ == '__main__':
    print("Starting server...")
    app.run(debug=False)
