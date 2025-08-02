from flask import Flask, request, jsonify, render_template
from datetime import datetime
import traceback
from analysis import analyze_token
from indicators import get_indicators_for_token

app = Flask(__name__)
token_data = {}  # { address: { name: ..., timeframes: { "1s": [...] }, updated: ... } }

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
            indicators = get_indicators_for_token(addr, tf, candles)

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

        except Exception as e:
            error_trace = traceback.format_exc()
            result[addr] = {
                "name": token["name"],
                "error": f"{type(e).__name__}: {str(e)}",
                "traceback": error_trace
            }

    return jsonify(result)

@app.route('/receive', methods=["POST"])
def receive():
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
        if address not in token_data:
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
                tf[tf_key].extend(new_candles)
                # Keep max N candles per timeframe
                max_len = 300 if tf_key in ["1S", "5S", "15S", "30S"] else (200 if tf_key == "1" else 50)
                tf[tf_key] = tf[tf_key][-max_len:]

        
        
        #process_new_candles(address, candles)    
        token_data[address]["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print("Token data updated at:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        print(f"📥 Received {len(candles_by_tf)} candles for {name} ({address})")
        return jsonify({"status": "ok"})
    
    # Always return a response if payloadID is missing
    return jsonify({"status": "error", "message": "Missing or invalid payload ID"}), 400

@app.after_request
def cors_headers(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Headers'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = '*'
    return resp

if __name__ == '__main__':
    print("Starting server...")
    app.run(debug=False)


# Candle {'1': {'1754166840000': {'timestamp': 1754166840000, 'open': 77238.4034895985, 'high': 77280.15006279225, 'low': 55450.13027832765, 'close': 60536.06407743671, 'volume': 15060.589273802372}, '1754166900000': {'timestamp': 1754166900000, 'open': 60536.06407743671, 'high': 68500.34510114716, 'low': 58553.26923861177, 'close': 64328.94255659289, 'volume': 8120.336672921897}, '1754166960000': {'timestamp': 1754166960000, 'open': 64328.94255659289, 'high': 83627.04970275494, 'low': 55112.86036729681, 'close': 66611.06935553355, 'volume': 17856.096628593506}, '1754167020000': {'timestamp': 1754167020000, 'open': 66611.06935553355, 'high': 66453.43957294626, 'low': 51971.30238537541, 'close': 59881.07092546184, 'volume': 8723.889263105943}, '1754167080000': {'timestamp': 1754167080000, 'open': 59881.07092546184, 'high': 61446.182538070905, 'low': 50614.18603808159, 'close': 53829.986412086175, 'volume': 4081.0638193682257}}, 
#         '3': {'1754166960000': {'timestamp': 1754166960000, 'open': 64328.94255659289, 'high': 83627.04970275494, 'low': 50614.18603808159, 'close': 53829.986412086175, 'volume': 30661.049711067673}}, 
#         '5': {'1754166900000': {'timestamp': 1754166900000, 'open': 60536.06407743671, 'high': 83627.04970275494, 'low': 50614.18603808159, 'close': 53829.986412086175, 'volume': 38781.38638398957}}, 
#         '15S': {'1754167036506': {'timestamp': 1754167036506, 'open': 64249.27787270253, 'high': 64898.2604774773, 'low': 64249.27787270253, 'close': 64898.2604774773, 'volume': 0.01}}, 
