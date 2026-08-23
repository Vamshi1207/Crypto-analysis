from flask import Flask, request, jsonify, render_template, make_response
from flask_sock import Sock
from datetime import datetime
import traceback
from analysis import analyze_token
from indicators import get_indicators_for_token
from decision import cache as decision_cache
from decision import agy_cli
from decision import store as decision_store
from decision.decide import decide as run_decide
from decision.replay import pick_continuous_live_token
from decision.safety import check_token
from decision.schema import DecideMode
from decision.sources import SourceError, resolve_to_mint
from pathlib import Path
import json
import os
import portguard
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
    # Prefer denser TFs when the requested one is empty (discover = 1m only).
    fallback_order = ["5S", "15S", "30S", "1S", "1", "3", "5", "15", "30", "60"]

    for addr, token in token_data.items():
        candles = (token.get("timeframes") or {}).get(tf)
        used_tf = tf
        if not candles:
            for alt in fallback_order:
                if alt == tf:
                    continue
                alt_rows = (token.get("timeframes") or {}).get(alt)
                if alt_rows:
                    candles = alt_rows
                    used_tf = alt
                    break

        meta = {
            "mint": token.get("mint"),
            "discover": bool(token.get("discover")),
            "role": token.get("role") or ("trade" if token.get("tradeable", True) else "observe"),
            "tradeable": token.get("tradeable", True) is not False,
            "cooled": bool(token.get("cooled")),
            "liquidity_usd": token.get("liquidity_usd"),
            "volume_24h_usd": token.get("volume_24h_usd"),
            "discover_source": token.get("discover_source"),
            "tf_used": used_tf,
        }

        if not candles or len(candles) == 0:
            result[addr] = {
                "name": token.get("name"),
                "error": f"Not enough candles in timeframe '{tf}'. Got 0.",
                **meta,
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
                "name": token.get("name"),
                **price_data,
                **indicators,
                "analysis": analysis_result,
                "bars": len(candles),
                **meta,
            }

        except Exception as e:
            error_trace = traceback.format_exc()
            result[addr] = {
                "name": token.get("name"),
                "error": f"{type(e).__name__}: {str(e)}",
                "traceback": error_trace,
                **meta,
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
# Cap in-memory live series so long watching sessions stay bounded.
LIVE_MAX_BARS_PER_TF = int(os.getenv("LIVE_MAX_BARS_PER_TF", "2000"))
LIVE_MAX_STATS = int(os.getenv("LIVE_MAX_STATS", "120"))


def _iter_candle_dicts(candles):
    """Accept list-of-candles or timestamp->candle dict from the extension."""
    if isinstance(candles, dict):
        return list(candles.values())
    if isinstance(candles, list):
        return candles
    return []


def _apply_live_candles(address, name, payload_id, candles_by_tf, stats_by_bucket):
    """Merge incremental live OHLCV into token_data (no disk finalize required)."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    token = token_data.get(address)
    if token is None:
        token = {
            "payload_id": payload_id,
            "name": name,
            "timeframes": {tf: [] for tf in RESOLUTIONS},
            "stats": [],
            "updated": now,
            "live": True,
        }
    else:
        token["name"] = name or token.get("name", "Unknown")
        token["payload_id"] = payload_id or token.get("payload_id")
        token["live"] = True
        token["updated"] = now
        if "timeframes" not in token:
            token["timeframes"] = {tf: [] for tf in RESOLUTIONS}

    bars_merged = 0
    for tf_key, candles in (candles_by_tf or {}).items():
        if tf_key not in RESOLUTIONS:
            continue
        existing = {
            c["timestamp"]: c
            for c in token["timeframes"].get(tf_key, [])
            if isinstance(c, dict) and "timestamp" in c
        }
        for candle in _iter_candle_dicts(candles):
            if not isinstance(candle, dict) or "timestamp" not in candle:
                continue
            existing[candle["timestamp"]] = candle
            bars_merged += 1
        ordered = sorted(existing.values(), key=lambda c: c["timestamp"])
        if len(ordered) > LIVE_MAX_BARS_PER_TF:
            ordered = ordered[-LIVE_MAX_BARS_PER_TF:]
        token["timeframes"][tf_key] = ordered

    if stats_by_bucket:
        stats_map = {
            s.get("createdAt"): s
            for s in token.get("stats", [])
            if isinstance(s, dict) and s.get("createdAt") is not None
        }
        for stat in stats_by_bucket:
            if isinstance(stat, dict) and "createdAt" in stat:
                stats_map[stat["createdAt"]] = stat
        ordered_stats = sorted(stats_map.values(), key=lambda s: s["createdAt"])
        if len(ordered_stats) > LIVE_MAX_STATS:
            ordered_stats = ordered_stats[-LIVE_MAX_STATS:]
        token["stats"] = ordered_stats
        print(f"📊 live stats: +{len(stats_by_bucket)} → {len(token['stats'])} buckets ({name})")

    token_data[address] = token
    return {
        "status": "live_ok",
        "bars_merged": bars_merged,
        "stats_count": len(token.get("stats") or []),
        "timeframes": {
            tf: len(token["timeframes"].get(tf, [])) for tf in RESOLUTIONS
        },
    }


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

    print(f"📊 Final stats count: {len(stats)}")
    print(f"📊 Final candles count: {sum(len(v) for v in timeframes.values())}")

    return {
        "status": "finalized",
        "message": f"Finalized {sum(len(values) for values in timeframes.values())} candles",
    }, True


def _process_payload(data):
    payloadID = data.get("id")
    payload_data = data.get("candles", {})
    is_complete = bool(data.get("complete", False))
    is_live = bool(data.get("live", False))

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

    # Live stream path: merge into memory immediately; skip historical spool/ignore.
    if is_live:
        result = _apply_live_candles(
            address, name, payloadID, candles_by_tf, stats_by_bucket
        )
        if candles_by_tf:
            for tf_key, candles in candles_by_tf.items():
                if tf_key in RESOLUTIONS:
                    print(f"📡 live {tf_key}: +{len(candles)} bars → {name}")
        if stats_by_bucket:
            print(f"📡 live stats: +{len(stats_by_bucket)} buckets → {name}")
        return result, True

    candle_file = DATA_DIR_CANDLES / f"{address}_candles.json"
    stats_file = DATA_DIR_STATS / f"{address}_stats.json"

    existing_session = upload_sessions.get(address)
    same_inflight_payload = existing_session is not None and existing_session.get("payload_id") == payloadID

    # 🔥 FIX: allow overwrite ONLY if new payload session
    if not same_inflight_payload:
        if candle_file.exists() or stats_file.exists():
            return {"status": "ignored", "message": "Token already exists"}, True


    # 🔥 FIX: always create session (candles-first safe)
    session = _get_or_create_upload_session(address, name, payloadID)
    session["name"] = name
    session["updated"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    # 🔥 FIX: spool ANY chunk (candles or stats)
    if candles_by_tf or stats_by_bucket:
        _spool_payload_chunk(session, candles_by_tf, stats_by_bucket)

        if candles_by_tf:
            for tf_key, candles in candles_by_tf.items():
                if tf_key in RESOLUTIONS:
                    print(f"📥 {tf_key}: {len(candles)} candles")

        if stats_by_bucket:
            print(f"📥 Stats: {len(stats_by_bucket)} buckets")

    # 🔥 FIX: finalize ONLY on complete
    if is_complete:
        result, success = _finalize_upload_session(address, name, payloadID)

        if success:
            print(f"🏁 Token {name} finalized ({payloadID})")

        return result, success

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
                "live": payload.get("live", False),
                "complete": payload.get("complete", False),
            }
        )

        result, _success = _process_payload(payload)
        ws.send(json.dumps(result))

@app.route('/safety', methods=["GET", "POST", "OPTIONS"])
def safety():
    """Gate 0: on-chain screening. Accepts a mint *or* an Axiom pool address."""
    if request.method == "OPTIONS":
        return _build_cors_preflight_response()

    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        address = body.get("mint") or body.get("address") or ""
    else:
        address = request.args.get("mint") or request.args.get("address") or ""

    if not address:
        return jsonify({"error": "provide a 'mint' (or 'address') parameter"}), 400

    try:
        resolved = resolve_to_mint(address)
        report = check_token(resolved["mint"])
    except SourceError as exc:
        return jsonify({"error": f"could not resolve address: {exc}", "address": address}), 404
    except Exception as exc:
        return jsonify({
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }), 500

    payload = report.model_dump(mode="json")
    payload["summary"] = report.summary()
    payload["blocking"] = report.blocking
    # Surface the pool→mint mapping so the dashboard/extension can learn the mint.
    payload["resolved"] = resolved

    try:
        decision_store.append("safety", payload)
    except OSError as exc:
        # A failed write must not deny a caller its safety verdict.
        print(f"⚠️ could not log safety report: {exc}")

    return jsonify(payload)


@app.route('/decide', methods=["GET", "POST", "OPTIONS"])
def decide_endpoint():
    """Gates 0–2 + calibrated forecast → DecisionCard. Default mode=fast."""
    if request.method == "OPTIONS":
        return _build_cors_preflight_response()

    if request.method == "POST":
        body = request.get_json(silent=True) or {}
    else:
        body = request.args.to_dict()

    mode = body.get("mode", "fast")
    timeframe = body.get("tf") or body.get("timeframe") or "1"
    address = body.get("address") or body.get("mint") or ""
    source = (body.get("source") or "live").lower()
    corpus_address = body.get("corpus") or (address if source == "corpus" else None)

    try:
        horizon = int(body.get("horizon") or 10)
    except (TypeError, ValueError):
        horizon = 10

    # source=live_replay: pick a contiguous OHLCV run from the candle corpus and
    # feed it through the same path as extension live data (no Gate 0 on stale pools).
    if source in ("live_replay", "replay", "continuous"):
        try:
            picked = pick_continuous_live_token(
                min_bars=int(body.get("min_bars") or 128),
                tail=int(body.get("tail") or 512),
            )
        except Exception as exc:
            return jsonify({"error": f"replay pick failed: {exc}"}), 404

        # Inject into the live buffer so the dashboard can see it too.
        token_data[picked["address"]] = {
            "name": picked["name"],
            "timeframes": dict(picked["live_token"]["timeframes"]),
            "stats": [],
            "updated": "replay",
            "payload_id": "replay",
        }
        try:
            card = run_decide(
                address=picked["address"],
                live_token=picked["live_token"],
                mode=DecideMode(mode),
                timeframe=picked["timeframe"],
                horizon=horizon,
                skip_safety=True,
                run_safety=False,
            )
        except Exception as exc:
            return jsonify({
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }), 500

        payload = card.model_dump(mode="json")
        payload["summary"] = card.summary()
        payload["replay"] = {
            "address": picked["address"],
            "name": picked["name"],
            "timeframe": picked["timeframe"],
            "continuous_bars": picked["continuous_bars"],
            "live_bars": picked["live_bars"],
            "first_ts": picked["first_ts"],
            "last_ts": picked["last_ts"],
        }
        return jsonify(payload)

    if not address and not corpus_address:
        return jsonify({"error": "provide address/mint, or source=corpus|live_replay"}), 400

    live_token = None
    run_safety = source != "corpus"
    if source != "corpus" and address:
        # Prefer live candles from the extension ingest buffer.
        live_token = token_data.get(address)
        if live_token is None and not run_safety:
            return jsonify({"error": f"no live candles for {address}"}), 404

    try:
        if source == "corpus":
            card = run_decide(
                corpus_address=corpus_address or address,
                mode=DecideMode(mode),
                timeframe=timeframe,
                horizon=horizon,
                run_safety=False,
            )
        elif live_token is not None:
            # address is the Axiom URL key (often a pool). decide() resolves SPL mint.
            card = run_decide(
                address=address,
                live_token=live_token,
                mode=DecideMode(mode),
                timeframe=timeframe,
                horizon=horizon,
                run_safety=True,
            )
            resolved_mint = (card.token or {}).get("mint")
            if resolved_mint:
                live_token["mint"] = resolved_mint
                token_data[address] = live_token
        else:
            # No candles yet — still run Gate 0 so the UI can show a block.
            card = run_decide(
                address=address,
                mode=DecideMode(mode),
                timeframe=timeframe,
                horizon=horizon,
                run_safety=True,
            )
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }), 500

    payload = card.model_dump(mode="json")
    payload["summary"] = card.summary()
    return jsonify(payload)


@app.route('/execute', methods=["GET", "POST", "OPTIONS"])
def execute_endpoint():
    """Phase 4: decide + paper-execute for an address."""
    if request.method == "OPTIONS":
        return _build_cors_preflight_response()

    from decision import paper

    body = request.get_json(silent=True) or {} if request.method == "POST" else request.args.to_dict()
    address = body.get("address") or body.get("mint") or ""
    mode = body.get("mode", "full")
    if not address:
        return jsonify({"error": "address required"}), 400

    live_token = token_data.get(address)
    try:
        card = run_decide(
            address=address,
            live_token=live_token,
            mode=DecideMode(mode),
            timeframe=body.get("tf") or "1",
            run_safety=True,
        )
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    mark = None
    if live_token:
        for tf in ("5S", "15S", "30S", "1"):
            rows = (live_token.get("timeframes") or {}).get(tf) or []
            if rows:
                mark = rows[-1].get("close")
                break
    result = paper.execute_decision(card, mark_price=mark)
    exits = []
    if mark is not None:
        exits = paper.mark_and_maybe_exit(address=address, mark_price=float(mark))
    return jsonify({
        "decision": card.model_dump(mode="json"),
        "summary": card.summary(),
        "execute": result,
        "exits": exits,
        "portfolio": paper.snapshot(),
    })


@app.route('/portfolio', methods=["GET", "POST", "OPTIONS"])
def portfolio_endpoint():
    if request.method == "OPTIONS":
        return _build_cors_preflight_response()
    from decision import paper

    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        if "kill_switch" in body:
            return jsonify(paper.set_kill_switch(bool(body.get("kill_switch"))))

    snap = paper.snapshot()
    # Enrich open positions with live marks from the shared buffer (trading-desk PnL).
    open_enriched = []
    unrealized = 0.0
    open_notional = 0.0
    for pos in snap.get("open") or []:
        if not isinstance(pos, dict):
            continue
        row = dict(pos)
        addr = str(row.get("address") or "")
        tok = token_data.get(addr) if addr else None
        mark = None
        if isinstance(tok, dict):
            tfs = tok.get("timeframes") or {}
            for key in ("5S", "15S", "30S", "1", "3", "5"):
                bars = tfs.get(key) or []
                if bars:
                    try:
                        mark = float(bars[-1].get("close"))
                        break
                    except (TypeError, ValueError, AttributeError):
                        pass
        entry = row.get("entry_price")
        qty = row.get("qty")
        u_pnl = None
        u_pct = None
        mark_value = None
        try:
            if mark is not None and entry is not None and qty is not None:
                u_pnl = float(qty) * (float(mark) - float(entry))
                u_pct = (float(mark) / float(entry) - 1.0) * 100.0 if float(entry) else None
                mark_value = float(qty) * float(mark)
                unrealized += u_pnl
                open_notional += mark_value
        except (TypeError, ValueError):
            pass
        row["mark_price"] = mark
        row["unrealized_pnl_usd"] = None if u_pnl is None else round(u_pnl, 4)
        row["unrealized_pct"] = None if u_pct is None else round(u_pct, 4)
        row["mark_value_usd"] = None if mark_value is None else round(mark_value, 4)
        open_enriched.append(row)

    cash = float(snap.get("cash_usd") or 0.0)
    realized = float(snap.get("realized_pnl_usd") or 0.0)
    starting = 1000.0
    equity = cash + open_notional
    snap["open"] = open_enriched
    snap["unrealized_pnl_usd"] = round(unrealized, 4)
    snap["open_notional_usd"] = round(open_notional, 4)
    snap["equity_usd"] = round(equity, 4)
    snap["total_pnl_usd"] = round(realized + unrealized, 4)
    snap["equity_pnl_usd"] = round(equity - starting, 4)
    snap["starting_cash_usd"] = starting
    sniper = dict(snap.get("sniper") or {})
    sniper["open"] = [r for r in open_enriched if r.get("entry_reason") == "snipe"]
    sniper["open_count"] = len(sniper["open"])
    snap["sniper"] = sniper
    return jsonify(snap)


@app.route('/swarm', methods=["GET", "POST", "OPTIONS"])
def swarm_endpoint():
    """Start/stop continuous decide→paper swarm over live token_data."""
    if request.method == "OPTIONS":
        return _build_cors_preflight_response()
    from decision import swarm

    if request.method == "GET":
        return jsonify(swarm.status())

    body = request.get_json(silent=True) or {}
    action = (body.get("action") or "status").lower()
    if action == "start":
        return jsonify(
            swarm.start(
                lambda: token_data,
                interval_s=float(body.get("interval_s") or 15),
                mode=body.get("mode") or "full",
            )
        )
    if action == "stop":
        return jsonify(swarm.stop())
    return jsonify(swarm.status())


@app.route('/discover', methods=["GET", "POST", "OPTIONS"])
def discover_endpoint():
    """Start/stop/force headless Solana discovery into token_data."""
    if request.method == "OPTIONS":
        return _build_cors_preflight_response()
    from decision import discover

    if request.method == "GET":
        return jsonify(discover.status())

    body = request.get_json(silent=True) or {}
    action = (body.get("action") or "status").lower()
    if action == "start":
        interval = body.get("interval_s")
        return jsonify(
            discover.start(
                interval_s=float(interval) if interval is not None else None,
            )
        )
    if action == "stop":
        return jsonify(discover.stop())
    if action in ("scan", "force", "once"):
        return jsonify(discover.scan_once())
    return jsonify(discover.status())


@app.route('/pipeline', methods=["GET", "OPTIONS"])
def pipeline_endpoint():
    """Recent structured pipeline events (discover → gates → paper → swarm)."""
    if request.method == "OPTIONS":
        return _build_cors_preflight_response()
    from decision import pipeline_log

    try:
        limit = int(request.args.get("limit") or 100)
    except (TypeError, ValueError):
        limit = 100
    stage = request.args.get("stage") or None
    rows = pipeline_log.recent(limit=max(1, min(limit, 2000)), stage=stage)
    return jsonify(
        {
            "n_today": pipeline_log.count_today(),
            "returned": len(rows),
            "stage": stage,
            "events": rows,
        }
    )


@app.route('/funnel', methods=["GET", "OPTIONS"])
def funnel_endpoint():
    """Session drop-off from discovery to fill, plus the roster it ran on."""
    if request.method == "OPTIONS":
        return _build_cors_preflight_response()
    from decision import paper, pipeline_log, pricefeed

    roster = {"trade": [], "observe": []}
    for address, token in token_data.items():
        if not isinstance(token, dict):
            continue
        bucket = "observe" if token.get("tradeable") is False else "trade"
        bars = max(
            (len(rows) for rows in (token.get("timeframes") or {}).values() if rows),
            default=0,
        )
        roster[bucket].append(
            {
                "address": address,
                "symbol": token.get("name"),
                "mint": token.get("mint"),
                "bars": bars,
                "candle_source": token.get("candle_source"),
                "liquidity_usd": token.get("liquidity_usd"),
                "cooled": bool(token.get("cooled")),
            }
        )

    port = paper.snapshot()
    return jsonify(
        {
            **pipeline_log.funnel(),
            "roster": {
                "trade_n": len(roster["trade"]),
                "observe_n": len(roster["observe"]),
                "trade": sorted(roster["trade"], key=lambda r: -(r["bars"] or 0))[:40],
                "observe": sorted(roster["observe"], key=lambda r: -(r["bars"] or 0))[:40],
            },
            "pricefeed": pricefeed.status(),
            "paper": {
                "cash_usd": port.get("cash_usd"),
                "open_count": port.get("open_count"),
                "realized_pnl_usd": port.get("realized_pnl_usd"),
                "limits": port.get("limits"),
            },
        }
    )


@app.route('/trenches', methods=["GET", "POST", "OPTIONS"])
def trenches_endpoint():
    """Paper launch + smart-money cluster channel."""
    if request.method == "OPTIONS":
        return _build_cors_preflight_response()
    from decision import trenches

    if request.method == "GET":
        return jsonify(trenches.status())
    body = request.get_json(silent=True) or {}
    action = (body.get("action") or "status").lower()
    if action == "start":
        return jsonify(trenches.start())
    if action == "stop":
        return jsonify(trenches.stop())
    if action == "scan":
        return jsonify(trenches.scan_once())
    return jsonify(trenches.status())


@app.route('/arb', methods=["GET", "POST", "OPTIONS"])
def arb_endpoint():
    """Paper-only cross-pool quote arbitrage (parallel to directional swarm)."""
    if request.method == "OPTIONS":
        return _build_cors_preflight_response()
    from decision import arb

    if request.method == "GET":
        return jsonify(arb.status())

    body = request.get_json(silent=True) or {}
    action = (body.get("action") or "status").lower()
    if action == "start":
        interval = body.get("interval_s")
        return jsonify(
            arb.start(interval_s=float(interval) if interval is not None else None)
        )
    if action == "stop":
        return jsonify(arb.stop())
    if action in ("scan", "force", "once"):
        return jsonify(arb.scan_once())
    return jsonify(arb.status())


@app.route('/readiness', methods=["GET", "OPTIONS"])
def readiness_endpoint():
    """Go / no-go scorecard for enabling LIVE_TRADING (paper must clear bars)."""
    if request.method == "OPTIONS":
        return _build_cors_preflight_response()
    from decision import readiness

    return jsonify(readiness.evaluate())


@app.route('/session/reset', methods=["POST", "OPTIONS"])
def session_reset_endpoint():
    """Archive today's logs + reset paper/discover/arb for a clean monitor window."""
    if request.method == "OPTIONS":
        return _build_cors_preflight_response()
    from decision import session as decision_session

    body = request.get_json(silent=True) or {}
    reason = str(body.get("reason") or "manual").strip() or "manual"
    return jsonify(decision_session.reset_board(reason=reason))


@app.route('/scoreboard', methods=["GET", "OPTIONS"])
def scoreboard_endpoint():
    if request.method == "OPTIONS":
        return _build_cors_preflight_response()
    from decision import scoreboard
    return jsonify(scoreboard.summarize())


@app.route('/calibrate', methods=["GET", "POST", "OPTIONS"])
def calibrate_endpoint():
    """Inspect or refit conformal residuals from live outcome logs."""
    if request.method == "OPTIONS":
        return _build_cors_preflight_response()
    from decision import calibrate as calibrate_mod

    if request.method == "GET":
        store = calibrate_mod.load_residuals()
        return jsonify(
            {
                "n": store.get("n") or len(store.get("errors") or []),
                "coverage_target": store.get("coverage_target"),
                "timeframe": store.get("timeframe"),
                "source": store.get("source"),
                "error_p50": store.get("error_p50"),
                "error_p80": store.get("error_p80"),
                "error_p90": store.get("error_p90"),
                "live_n": store.get("live_n"),
                "corpus_n": store.get("corpus_n"),
            }
        )

    body = request.get_json(silent=True) or {}
    result = calibrate_mod.fit_from_outcomes(
        days=int(body.get("days") or 7),
        min_errors=int(body.get("min_errors") or 30),
        merge_with_existing=bool(body.get("merge", True)),
    )
    return jsonify(result)


@app.route('/health')
def health():
    """Surfaces whether each tier is actually usable, not just whether we're up."""
    agy_status = agy_cli.preflight()
    from decision import forecast as forecast_mod
    from decision import calibrate as calibrate_mod
    from decision import arb, discover, paper, pipeline_log, pricefeed, readiness, swarm
    from decision import trenches
    from decision import session as decision_session

    residuals = calibrate_mod.load_residuals()
    port = paper.snapshot()
    disc = discover.status()
    arb_st = arb.status()
    feed = pricefeed.status()
    ready = readiness.evaluate()
    trade_n = sum(
        1
        for t in token_data.values()
        if isinstance(t, dict) and t.get("tradeable", True) is not False
    )
    observe_n = sum(
        1
        for t in token_data.values()
        if isinstance(t, dict) and t.get("tradeable") is False
    )
    return jsonify({
        "server": "ok",
        "tokens_loaded": len(token_data),
        "tokens_tradeable": trade_n,
        "tokens_observe": observe_n,
        "cache": decision_cache.stats(),
        "agy_cli": {
            "installed": agy_status.installed,
            "authenticated": agy_status.authenticated,
            "model": agy_status.model,
            "detail": agy_status.detail,
        },
        "forecast_backends": forecast_mod.available_backends(),
        "calibration": {
            "residuals": residuals.get("n", 0),
            "coverage_target": residuals.get("coverage_target"),
            "error_p80": residuals.get("error_p80"),
        },
        "live_trading": os.getenv("LIVE_TRADING", "0") == "1",
        "paper": {
            "open": port.get("open_count"),
            "realized_pnl_usd": port.get("realized_pnl_usd"),
            "kill_switch": port.get("kill_switch"),
        },
        "swarm": swarm.status(),
        "discover": {
            "running": disc.get("running"),
            "ticks": disc.get("ticks"),
            "last_scan_at": disc.get("last_scan_at"),
            "watchlist_count": disc.get("watchlist_count"),
            "cooling_count": disc.get("cooling_count"),
            "last_error": disc.get("last_error"),
        },
        "arb": {
            "running": arb_st.get("running"),
            "ticks": arb_st.get("ticks"),
            "paper_fills": arb_st.get("paper_fills"),
            "realized_pnl_usd": arb_st.get("realized_pnl_usd"),
            "last_error": arb_st.get("last_error"),
        },
        "pricefeed": {
            "running": feed.get("running"),
            "ticks": feed.get("ticks"),
            "tracked_n": feed.get("tracked_n"),
            "samples": feed.get("samples"),
            "last_error": feed.get("last_error"),
        },
        "trenches": trenches.status(),
        "readiness": {
            "ready_for_live": ready.get("ready_for_live"),
            "score": ready.get("score"),
            "recommendation": ready.get("recommendation"),
            "passed": ready.get("passed"),
            "total_checks": ready.get("total_checks"),
            "session_started_at": ready.get("session_started_at"),
        },
        "session_started_at": ready.get("session_started_at"),
        "pipeline_events_session": pipeline_log.count_session(),
        "safety_checks_logged_session": decision_session.count_since("safety"),
        "decisions_logged_session": decision_session.count_since("decisions"),
        "pipeline_events_today": pipeline_log.count_session(),
        "safety_checks_logged_today": decision_session.count_since("safety"),
        "decisions_logged_today": decision_session.count_since("decisions"),
    })


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


def _configure_background_channels() -> None:
    """Bind discover + paper-arb to the shared live buffer; optionally auto-start."""
    from decision import arb, discover, pricefeed, swarm, trenches

    def _set_token(address: str, token: dict) -> None:
        token_data[address] = token

    discover.configure(set_token=_set_token, get_tokens=lambda: token_data)
    arb.configure(get_tokens=lambda: token_data)
    trenches.configure(set_token=_set_token)
    if pricefeed.ENABLED:
        pricefeed.start()
        print("pricefeed: auto-started sampled candle feed", flush=True)
    if discover.DISCOVER_ENABLED:
        discover.start()
        print("discover: auto-started (DISCOVER_ENABLED=1)", flush=True)
    if arb.ARB_ENABLED:
        arb.start()
        print("arb: auto-started paper channel (ARB_ENABLED=1)", flush=True)
    if trenches.LAUNCH_ENABLED or trenches.CLUSTER_ENABLED or trenches.SNIPER_ENABLED:
        trenches.start()
        print("trenches: auto-started launch/cluster/sniper paper channel", flush=True)
    if os.getenv("SWARM_ENABLED", "0").strip() == "1":
        interval = float(os.getenv("SWARM_INTERVAL_SEC", "12") or 12)
        swarm_mode = (os.getenv("SWARM_MODE") or "fast").strip() or "fast"
        swarm.start(lambda: token_data, interval_s=interval, mode=swarm_mode)
        print(
            f"swarm: auto-started (SWARM_ENABLED=1, interval={interval}s, mode={swarm_mode})",
            flush=True,
        )


_configure_background_channels()

from decision import session as decision_session

decision_session.bootstrap(reason="server_boot")

if __name__ == '__main__':
    host = os.getenv('HOST', '0.0.0.0')
    port = int(os.getenv('PORT', '8000'))

    # Hand restarts routinely leave the previous instance owning the port.
    # Set SERVER_RECLAIM_PORT=0 to bind strictly and fail instead.
    if os.getenv('SERVER_RECLAIM_PORT', '1') == '1':
        for stale in portguard.reclaim(port):
            print(f"reclaimed port {port} from stale instance pid {stale}")
        if portguard.port_is_listening(port):
            print(f"warning: port {port} still has a LISTEN socket", flush=True)

    print(f"Starting server on {host}:{port}...")
    app.run(host=host, port=port, debug=False)
