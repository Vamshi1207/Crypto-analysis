from flask import Flask, request, jsonify, render_template
from datetime import datetime
from indicators import calculate_rsi  # 👈 import from your module
import traceback

app = Flask(__name__)
token_data = {}  # { address: { name: ..., candles: [...], updated: ... } }

@app.route('/')
def dashboard():
    return render_template("dashboard.html")  # 👈 use render_template instead

@app.route('/data')
def data():
    from indicators import (
        calculate_rsi,
        calculate_ema,
        calculate_macd,
        calculate_stochastic,
        calculate_vwap,
        calculate_adx,
        calculate_bollinger_bands
    )
    from analysis import analyze_token
    
    tf = request.args.get("tf", "1s")
    
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
            closes = [c.get('close') for c in candles if 'close' in c]
            highs = [c.get('high') for c in candles if 'high' in c]
            lows = [c.get('low') for c in candles if 'low' in c]
            volumes = [c.get('volume', 1) for c in candles]
           

            latest = candles[-1]
            price_data = {
                "open": latest.get("open"),
                "high": latest.get("high"),
                "low": latest.get("low"),
                "close": latest.get("close"),
                "volume": latest.get("volume"),
                "timestamp": latest.get("timestamp")
            }

            macd_result = calculate_macd(closes)
            macd_line, macd_signal, macd_hist = macd_result if macd_result else (None, None, None)

            stoch_result = calculate_stochastic(highs, lows, closes)
            stoch_k, stoch_d = stoch_result if stoch_result else (None, None)

            adx_result = calculate_adx(highs, lows, closes)
            if adx_result and len(adx_result) == 3:
                adx, plus_di, minus_di = adx_result
            else:
                adx, plus_di, minus_di = None, None, None


            boll_result = calculate_bollinger_bands(closes)
            boll_upper, boll_middle, boll_lower = boll_result if boll_result else (None, None, None)

            
            analysis_result = analyze_token({
                "rsi": calculate_rsi(closes),
                "macd_line": macd_line,
                "macd_signal": macd_signal,
                "macd_hist": macd_hist,
                "stoch_k": stoch_k,
                "stoch_d": stoch_d,
                "ema200": calculate_ema(closes),
                "vwap": calculate_vwap(highs, lows, closes, volumes),
                "adx": adx,
                "plus_di": plus_di,
                "minus_di": minus_di,
                "boll_upper": boll_upper,
                "boll_middle": boll_middle,
                "boll_lower": boll_lower,
                "close": price_data["close"],
                "highs": highs,
                "lows": lows,
                "volumes": volumes
            })

            result[addr] = {
                "name": token["name"],
                "candles": candles,  # Optional: remove if too large for frontend
                "open": price_data["open"],
                "high": price_data["high"],
                "low": price_data["low"],
                "close": price_data["close"],
                "volume": price_data["volume"],
                "timestamp": price_data["timestamp"],
                "rsi": calculate_rsi(closes),
                "ema200": calculate_ema(closes),
                "macd_line": macd_line,
                "macd_signal": macd_signal,
                "macd_hist": macd_hist,
                "stoch_k": stoch_k,
                "stoch_d": stoch_d,
                "vwap": calculate_vwap(highs, lows, closes, volumes),
                "adx": adx,
                "plus_di": plus_di,
                "minus_di": minus_di,
                "boll_upper": boll_upper,
                "boll_middle": boll_middle,
                "boll_lower": boll_lower,
                "analysis": analysis_result
            }
            
            

        except Exception as e:
            error_trace = traceback.format_exc()
            result[addr] = {
                "name": token["name"],
                "error": f"{type(e).__name__}: {str(e)}",
                "traceback": error_trace  # ✅ full stack trace
            }

    return jsonify(result)


def aggregate_candles(candles, group_size):
    grouped = []
    #print(f"🧮 Aggregating {len(candles)} into {group_size}s candles")
    for i in range(0, len(candles), group_size):
        group = candles[i:i + group_size]
        if len(group) < group_size:
            #print(f"⏭️ Skipping incomplete group of {len(group)} candles at index {i}")
            continue
        grouped.append({
            "open": group[0]['open'],
            "high": max(c['high'] for c in group),
            "low": min(c['low'] for c in group),
            "close": group[-1]['close'],
            "volume": sum(c.get('volume', 0) for c in group),
            "timestamp": group[0]['timestamp']
        })
    #print(f"✅ Aggregated {len(grouped)} candles for group size {group_size}")
    return grouped




@app.route('/receive', methods=['POST'])
def receive():
    data = request.get_json()
    candles = data.get("candles", [])
    token = data.get("token", {})
    address = token.get("address", "unknown")
    name = token.get("name", "Unknown")

    if not address or address == "unknown":
        return jsonify({"status": "error", "message": "Missing token address"}), 400

    # Ensure token bucket exists
    if address not in token_data:
        token_data[address] = {
            "name": name,
            "timeframes": {
                "1s": [],
                "1m": [],
                "2m": [],
                "3m": [],
                "5m": []
            },
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

    # Append new 1s candles
    tf = token_data[address]["timeframes"]
    tf["1s"].extend(candles)

    # Keep only the latest N candles (memory safety)
    tf["1s"] = tf["1s"][-300:]  # keep 5m worth at 1s interval

    # Recalculate aggregates
    tf["3s"] = aggregate_candles(tf["1s"], group_size=3)[-300:]
    tf["5s"] = aggregate_candles(tf["1s"], group_size=5)[-300:]
    tf["8s"] = aggregate_candles(tf["1s"], group_size=8)[-300:]
    tf["10s"] = aggregate_candles(tf["1s"], group_size=10)[-300:]
    tf["15s"] = aggregate_candles(tf["1s"], group_size=15)[-300:]
    tf["30s"] = aggregate_candles(tf["1s"], group_size=30)[-300:]
    tf["45s"] = aggregate_candles(tf["1s"], group_size=45)[-300:]
    tf["1m"] = aggregate_candles(tf["1s"], group_size=60)[-300:]
    tf["2m"] = aggregate_candles(tf["1s"], group_size=120)[-300:]
    tf["3m"] = aggregate_candles(tf["1s"], group_size=180)[-300:]
    tf["5m"] = aggregate_candles(tf["1s"], group_size=300)[-300:]

    token_data[address]["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"📥 Received {len(candles)} candles for {name} ({address})")
    return jsonify({"status": "ok"})


@app.after_request
def cors_headers(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Headers'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = '*'
    return resp

if __name__ == '__main__':
    print("Starting server...")
    app.run(debug=False)
