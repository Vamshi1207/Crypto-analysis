from flask import Flask, request, jsonify, render_template, make_response
from flask_sock import Sock
from datetime import datetime
import traceback
from analysis import analyze_token
from indicators import get_indicators_for_token
from pathlib import Path
import json
import os
import shutil

app = Flask(__name__)
sock = Sock(app)
token_data = {}
upload_sessions = {}

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


BASE_DATA_DIR = Path(
    os.getenv(
        "CANDLE_DATA_DIR",
        Path(__file__).resolve().parent / "ML_Training_datasets" / "CandleData",
    )
)
DATA_DIR_CANDLES = BASE_DATA_DIR / "Candles"
DATA_DIR_STATS = BASE_DATA_DIR / "Stats"
DATA_DIR_TMP = BASE_DATA_DIR / "TmpUploads"

DATA_DIR_CANDLES.mkdir(parents=True, exist_ok=True)
DATA_DIR_STATS.mkdir(parents=True, exist_ok=True)
DATA_DIR_TMP.mkdir(parents=True, exist_ok=True)

RESOLUTIONS = ["5S", "15S", "30S", "1", "3", "5", "15", "30", "60"]

def _build_materialized_token(name, payload_id, timeframes, stats, updated):
    token = {
        "payload_id": payload_id,
        "name": name,
        "timeframes": {tf: [] for tf in RESOLUTIONS},
        "stats": stats,
        "updated": updated,
    }
    for tf in RESOLUTIONS:
        token["timeframes"][tf] = timeframes.get(tf, [])
    return token


def _write_final_token_files(address, name, timeframes, stats, updated):
    candle_file = DATA_DIR_CANDLES / f"{address}_candles.json"
    stats_file = DATA_DIR_STATS / f"{address}_stats.json"

    with candle_file.open("w", encoding="utf-8") as f:
        json.dump({
            "name": name,
            "address": address,
            "timeframes": timeframes,
            "updated": updated
        }, f, indent=2)

    with stats_file.open("w", encoding="utf-8") as f:
        json.dump({
            "name": name,
            "address": address,
            "stats": stats,
            "updated": updated
        }, f, indent=2)


def _get_or_create_upload_session(address, name, payload_id):
    session = upload_sessions.get(address)
    if session is not None and session.get("payload_id") == payload_id:
        return session

    session_dir = DATA_DIR_TMP / address / payload_id
    session_dir.mkdir(parents=True, exist_ok=True)
    session = {
        "payload_id": payload_id,
        "name": name,
        "dir": session_dir,
        "chunk_index": 0,
        "updated": None,
    }
    upload_sessions[address] = session
    return session


def _spool_payload_chunk(session, candles_by_tf, stats_by_bucket):
    chunk_path = session["dir"] / f"chunk_{session['chunk_index']:06d}.json"
    with chunk_path.open("w", encoding="utf-8") as f:
        json.dump({
            "candles": candles_by_tf,
            "stats": stats_by_bucket,
        }, f, separators=(",", ":"))
    session["chunk_index"] += 1


def _finalize_upload_session(address, name, payload_id):
    session = upload_sessions.get(address)
    if session is None or session.get("payload_id") != payload_id:
        return {"status": "error", "message": "Upload session not found"}, False

    timeframe_maps = {tf: {} for tf in RESOLUTIONS}
    stats_map = {}

    for chunk_file in sorted(session["dir"].glob("chunk_*.json")):
        with chunk_file.open("r", encoding="utf-8") as f:
            chunk_payload = json.load(f)

        for tf_key, candles in chunk_payload.get("candles", {}).items():
            if tf_key not in timeframe_maps:
                continue
            for candle in candles.values():
                timeframe_maps[tf_key][candle["timestamp"]] = candle

        for stat in chunk_payload.get("stats", []):
            if "createdAt" in stat:
                stats_map[stat["createdAt"]] = stat

    timeframes = {
        tf: sorted(
            timeframe_maps[tf].values(),
            key=lambda candle: candle["timestamp"],
        )
        for tf in RESOLUTIONS
    }
    stats = sorted(
        stats_map.values(),
        key=lambda stat: stat["createdAt"],
    )
    updated = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    _write_final_token_files(address, name, timeframes, stats, updated)
    token_data[address] = _build_materialized_token(name, payload_id, timeframes, stats, updated)

    shutil.rmtree(session["dir"], ignore_errors=True)
    address_tmp_dir = DATA_DIR_TMP / address
    try:
        address_tmp_dir.rmdir()
    except OSError:
        pass
    upload_sessions.pop(address, None)

    return {
        "status": "finalized",
        "message": f"Finalized {sum(len(values) for values in timeframes.values())} candles",
    }, True


def _process_payload(data):
    payloadID = data.get("id")
    payload_data = data.get("candles", {})
    is_complete = bool(data.get("complete", False))

    if isinstance(payload_data, dict) and "candles" in payload_data:
        candles_by_tf = payload_data.get("candles", {})
        stats_by_bucket = payload_data.get("stats", [])
    else:
        candles_by_tf = payload_data if isinstance(payload_data, dict) else {}
        stats_by_bucket = data.get("stats", [])

    token = data.get("token", {})
    address = token.get("address")
    name = token.get("name", "Unknown")

    if not payloadID or not address:
        return {"status": "error", "message": "Missing payload ID or token address"}, False

    candle_file = DATA_DIR_CANDLES / f"{address}_candles.json"
    stats_file = DATA_DIR_STATS / f"{address}_stats.json"
    existing_session = upload_sessions.get(address)
    same_inflight_payload = existing_session is not None and existing_session.get("payload_id") == payloadID

    if not same_inflight_payload and (candle_file.exists() or stats_file.exists()):
        return {"status": "ignored", "message": "Token already exists"}, True

    session = _get_or_create_upload_session(address, name, payloadID)
    session["name"] = name
    session["updated"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    if candles_by_tf or stats_by_bucket:
        _spool_payload_chunk(session, candles_by_tf, stats_by_bucket)
        for tf_key, candles in candles_by_tf.items():
            if tf_key in RESOLUTIONS:
                print(f"📥 {tf_key}: chunk stored with {len(candles)} candles")

    if is_complete:
        result, success = _finalize_upload_session(address, name, payloadID)
        if success:
            print(f"🏁 Token {name} finalized for payload {payloadID}")
            print(f"🏁 Token {name} merging complete")
        return result, success

    print(f"✅ Token {name} chunk accepted at {session['updated']}")
    return {"status": "ok"}, True


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
            print("🔌 [WS] Client disconnected")
            break

        print(f"📨 [WS] Raw message received ({len(message)} bytes)")

        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            print("❌ [WS] Invalid JSON payload")
            ws.send(json.dumps({"status": "error", "message": "Invalid JSON"}))
            continue

        print(
            "📨 [WS] Parsed payload",
            {
                "id": payload.get("id"),
                "token": payload.get("token", {}).get("name"),
                "initial": payload.get("initial"),
                "complete": payload.get("complete", False),
            }
        )

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
    host = os.getenv('HOST', '0.0.0.0')
    port = int(os.getenv('PORT', '8000'))

    print(f"Starting server on {host}:{port}...")
    app.run(host=host, port=port, debug=False)
