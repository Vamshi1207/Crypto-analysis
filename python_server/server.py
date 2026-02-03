from flask import Flask, request, jsonify, render_template, make_response
from datetime import datetime
import traceback
from analysis import analyze_token
from indicators import get_indicators_for_token
from pathlib import Path
import json
import os

app = Flask(__name__)
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


DATA_DIR = Path("C:/Users/vamsh/Downloads/TA MV2/CandleData")
DATA_DIR.mkdir(parents=True, exist_ok=True)


@app.route('/receive', methods=["POST", "OPTIONS"])
def receive():
    if request.method == "OPTIONS":
        return _build_cors_preflight_response()
    
    data = request.get_json()
    payloadID = data.get("id", None)
    candles_by_tf = data.get("candles", [])
    token = data.get("token", {})
    address = token.get("address", "unknown")
    name = token.get("name", "Unknown")
    isInitialData = bool(data.get("initial", "False"))  

    #print("Candle", candles_by_tf)
    
    if payloadID:

        if not address or address == "unknown":
            return jsonify({"status": "error", "message": "Missing token address"}), 400

        # Initialize for new token
        if address not in token_data or isInitialData:
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
        
        
        #process_new_candles(address, candles)    
        token_data[address]["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print("Token data updated at:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        out_file = DATA_DIR / f"{address}.json"
        with out_file.open("w") as f:
            json.dump(token_data[address], f, indent=2)


        #print(f"📥 Received {len(candles_by_tf)} candles for {name} ({address})")
        return jsonify({"status": "ok"})
    
    # Always return a response if payloadID is missing
    return jsonify({"status": "error", "message": "Missing or invalid payload ID"}), 400

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
