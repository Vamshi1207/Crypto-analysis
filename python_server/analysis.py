# --- analysis.py ---
import numpy as np


def analyze_token(metrics):
    result = {
        "trend_strength": None,
        "momentum": None,
        "volatility": None,
        "overall": None,
        "notes": []
    }

    highs = metrics.get("highs", [])
    lows = metrics.get("lows", [])
    volumes = metrics.get("volumes", [])
    price = metrics.get("close")

    # Existing ChoCH
    choch_result, choch_note = detect_choch(highs, lows, volumes)
    result["choch"] = choch_result
    result["notes"].append(choch_note)

    # --- Fibonacci ---
    fib = compute_fibonacci_levels(highs, lows, span=3, lookback=300)
    result["fib"] = fib  # expose to UI if you want to draw it

    recent_closes = metrics.get("closes", [])
    patterns = detect_all_chart_patterns(recent_closes)
    for p in patterns:
        result["notes"].append(f"📊 Chart Pattern: {p}")


    if fib and price:
        # Check proximity to retracements
        near_retr = nearest_level(price, fib["retracements"])
        if near_retr and near_retr[2] <= 0.01:  # within 1%
            lvl_name, lvl_price, dist = near_retr
            result["notes"].append(
                f"📏 Near Fib retracement {lvl_name.replace('r_','')}% at {lvl_price:.2f} (Δ {dist*100:.2f}%)"
            )

        # Check proximity to extensions (breakouts)
        near_ext = nearest_level(price, fib["extensions"])
        if near_ext and near_ext[2] <= 0.01:
            lvl_name, lvl_price, dist = near_ext
            result["notes"].append(
                f"🚀 Near Fib extension {lvl_name.replace('x_','')} at {lvl_price:.2f} (Δ {dist*100:.2f}%)"
            )

        # Optional: directional context
        if fib["direction"] == "up" and price >= fib["anchors"]["to"]:
            result["notes"].append("💚 Trading above swing high (potential trend continuation)")
        if fib["direction"] == "down" and price <= fib["anchors"]["to"]:
            result["notes"].append("❤️ Trading below swing low (potential downtrend continuation)")

    # --- Your existing indicator-based logic (unchanged) ---
    rsi = metrics.get("rsi")
    macd = metrics.get("macd_line")
    signal = metrics.get("macd_signal")
    histogram = metrics.get("macd_hist")
    ema200 = metrics.get("ema200")
    vwap = metrics.get("vwap")
    adx = metrics.get("adx")
    plus_di = metrics.get("plus_di")
    minus_di = metrics.get("minus_di")
    boll_upper = metrics.get("boll_upper")
    boll_middle = metrics.get("boll_middle")
    boll_lower = metrics.get("boll_lower")
    obv = metrics.get("obv")
    atr = metrics.get("atr")
    supertrend = metrics.get("supertrend")

    if adx and adx > 25:
        result["trend_strength"] = "Strong"
        result["notes"].append("✅ Strong trend (ADX > 25)")
    elif adx and adx < 20:
        result["trend_strength"] = "Weak"
        result["notes"].append("⚠️ Weak trend (ADX < 20)")

    if rsi:
        if rsi < 30:
            result["momentum"] = "Oversold"
            result["notes"].append("🟢 RSI < 30 (possible buy)")
        elif rsi > 70:
            result["momentum"] = "Overbought"
            result["notes"].append("🔴 RSI > 70 (possible sell)")

    if macd is not None and signal is not None and histogram is not None:
        if macd > signal and histogram > 0:
            result["momentum"] = "Bullish Momentum"
            result["notes"].append("📈 MACD crossover")
        elif macd < signal and histogram < 0:
            result["momentum"] = "Bearish Momentum"
            result["notes"].append("📉 MACD crossover")

    if boll_upper and boll_lower and price:
        band_range = boll_upper - boll_lower
        if band_range / price > 0.2:
            result["volatility"] = "High"
            result["notes"].append("🌊 High volatility (wide BB)")
        elif band_range / price < 0.05:
            result["volatility"] = "Low"
            result["notes"].append("💤 Low volatility (tight BB)")

    if isinstance(supertrend, list):
        result["supertrend"] = [
            {
                "value": float(t["value"]) if isinstance(t["value"], (float, int, str)) else str(t["value"]),
                "trend": str(t["trend"])
            }
            for t in supertrend
            if isinstance(t, dict) and "value" in t and "trend" in t
        ]



    if isinstance(atr, list) and len(atr) > 0 and price:
        if atr[-1] / price > 0.05:
            result["notes"].append("🌪️ High volatility (ATR > 5%)")
        elif atr[-1] / price < 0.02:
            result["notes"].append("🔕 Low volatility (ATR < 2%)")


    
    obv_series = metrics.get("obv", [])
    if isinstance(obv_series, list) and len(obv_series) >= 2:
        if obv_series[-1] > obv_series[-2]:
            result["notes"].append("📈 OBV rising (buying pressure)")
        elif obv_series[-1] < obv_series[-2]:
            result["notes"].append("📉 OBV falling (selling pressure)")



    positive_signals = len([n for n in result["notes"] if "✅" in n or "🟢" in n or "📈" in n or "💚" in n])
    negative_signals = len([n for n in result["notes"] if "⚠️" in n or "🔴" in n or "📉" in n or "❤️" in n])
    score = positive_signals - negative_signals

    if score >= 2:
        result["overall"] = "🟢 Bullish"
    elif score <= -2:
        result["overall"] = "🔴 Bearish"
    else:
        result["overall"] = "⚪ Neutral"

    return result



def detect_choch(highs, lows, volumes, volume_multiplier=1.5):
    if len(highs) < 6 or len(lows) < 6 or len(volumes) < 6:
        return "N/A", "Not enough data for ChoCH"

    prev_highs = highs[-6:-1]
    prev_lows = lows[-6:-1]
    curr_high = highs[-1]
    curr_low = lows[-1]
    curr_volume = volumes[-1]
    avg_volume = sum(volumes[-6:-1]) / 5

    choch_type = None

    if curr_high > max(prev_highs) and curr_low > max(prev_lows):
        choch_type = "Bullish ChoCH"
    elif curr_high < min(prev_highs) and curr_low < min(prev_lows):
        choch_type = "Bearish ChoCH"

    if choch_type:
        if curr_volume >= avg_volume * volume_multiplier:
            return choch_type, f"{choch_type} confirmed by volume spike ({curr_volume:.2f} vs avg {avg_volume:.2f})"
        else:
            return "Unconfirmed ChoCH", f"{choch_type} detected but no volume confirmation ({curr_volume:.2f} < avg {avg_volume:.2f})"

    return "No ChoCH", "🟡 Structure unchanged"

# --- analysis.py ---

def _find_swings_fractal(highs, lows, span=3, lookback=300):
    """
    Returns lists of (idx, price) swing highs and lows using a simple fractal rule:
    A swing high at i if highs[i] is the max in [i-span, i+span], swing low if min.
    """
    n = len(highs)
    if n < 2*span + 1:
        return [], []

    start = max(0, n - lookback)
    sh, sl = [], []
    for i in range(max(span, start), n - span):
        window_h = highs[i-span:i+span+1]
        window_l = lows[i-span:i+span+1]
        if highs[i] == max(window_h):
            sh.append((i, highs[i]))
        if lows[i] == min(window_l):
            sl.append((i, lows[i]))
    return sh, sl


def _pick_last_leg(highs, lows, span=3, lookback=300):
    """
    Pick the latest completed leg: (start_idx, end_idx, direction, low_price, high_price)
    direction is 'up' if last leg is low->high, 'down' if high->low.
    """
    sh, sl = _find_swings_fractal(highs, lows, span=span, lookback=lookback)
    if not sh and not sl:
        return None

    # Merge swings by index order and take the last two of alternating types
    swings = [(i, p, 'H') for i, p in sh] + [(i, p, 'L') for i, p in sl]
    swings.sort(key=lambda x: x[0])
    # Need last two opposite-type swings to define a leg
    for j in range(len(swings) - 2, -1, -1):
        i1, p1, t1 = swings[j]
        i2, p2, t2 = swings[j+1]
        if t1 != t2:
            if t1 == 'L' and t2 == 'H' and i1 < i2:
                return (i1, i2, 'up', p1, p2)
            if t1 == 'H' and t2 == 'L' and i1 < i2:
                return (i1, i2, 'down', p2, p1)  # normalize low, high
    return None


def compute_fibonacci_levels(highs, lows, span=3, lookback=300):
    """
    Compute fib retracements/extensions for the latest leg.
    Returns dict with anchors, retracements, extensions, direction, and indices.
    """
    leg = _pick_last_leg(highs, lows, span=span, lookback=lookback)
    if not leg:
        return None

    start_i, end_i, direction, swing_low, swing_high = leg
    diff = swing_high - swing_low
    if diff <= 0:
        return None

    retr_ratios = [0.236, 0.382, 0.5, 0.618, 0.786]
    ext_ratios = [1.272, 1.618, 2.618]

    if direction == 'up':
        retr = {f"r_{int(r*100)}": swing_high - r*diff for r in retr_ratios}
        exts = {f"x_{str(r).replace('.','')}": swing_high + (r-1.0)*diff for r in ext_ratios}
        anchors = {"from": swing_low, "to": swing_high}
    else:  # down
        retr = {f"r_{int(r*100)}": swing_low + r*diff for r in retr_ratios}
        exts = {f"x_{str(r).replace('.','')}": swing_low - (r-1.0)*diff for r in ext_ratios}
        anchors = {"from": swing_high, "to": swing_low}

    return {
        "direction": direction,
        "start_idx": start_i,
        "end_idx": end_i,
        "anchors": anchors,
        "retracements": retr,    # r_236, r_382, r_50, r_618, r_786
        "extensions": exts,      # x_1272, x_1618, x_2618
    }


def nearest_level(price, levels_dict):
    """
    Returns (level_name, level_price, rel_distance) for the nearest key in levels_dict.
    rel_distance is abs(price - level) / price.
    """
    if not levels_dict:
        return None
    best = min(
        ((k, v, abs(price - v) / price) for k, v in levels_dict.items()),
        key=lambda x: x[2]
    )
    return best


def detect_all_chart_patterns(prices):
    """
    Detects all major chart patterns (classic + advanced) from a list of price values.
    Returns a list of pattern names with emojis.
    """
    patterns = []
    prices = list(prices)
    np_prices = np.array(prices)

    if len(prices) < 10:
        return patterns

    # --- Helper: Slope ---
    def slope(x):
        x_range = np.arange(len(x))
        return np.polyfit(x_range, x, 1)[0]

    # --- Classic Patterns ---
    def is_double_top(p):
        return len(p) >= 5 and p[0] < p[1] and p[2] < p[1] and abs(p[1] - p[3]) < 0.05 * p[1] and p[4] < p[2]

    def is_double_bottom(p):
        return len(p) >= 5 and p[0] > p[1] and p[2] > p[1] and abs(p[1] - p[3]) < 0.05 * p[1] and p[4] > p[2]

    def is_triple_top(p):
        return (len(p) >= 7 and abs(p[1] - p[3]) < 0.05 * p[1] and abs(p[3] - p[5]) < 0.05 * p[3]
                and p[0] < p[1] and p[2] < p[1] and p[4] < p[3] and p[6] < p[5])

    def is_triple_bottom(p):
        return (len(p) >= 7 and abs(p[1] - p[3]) < 0.05 * p[1] and abs(p[3] - p[5]) < 0.05 * p[3]
                and p[0] > p[1] and p[2] > p[1] and p[4] > p[3] and p[6] > p[5])

    def is_head_and_shoulders(p):
        return (len(p) >= 7 and p[0] < p[1] and p[2] < p[1] and p[2] < p[3] and
                p[3] > p[1] and p[4] < p[3] and p[5] < p[1] and abs(p[1] - p[5]) < 0.05 * p[1])

    def is_inverse_head_and_shoulders(p):
        return (len(p) >= 7 and p[0] > p[1] and p[2] > p[1] and p[2] > p[3] and
                p[3] < p[1] and p[4] > p[3] and p[5] > p[1] and abs(p[1] - p[5]) < 0.05 * p[1])

    # --- Advanced Patterns ---
    def rising_wedge(p):
        return slope(p[-5:]) > 0 and max(p[-5:]) - min(p[-5:]) < 0.05 * p[-1]

    def falling_wedge(p):
        return slope(p[-5:]) < 0 and max(p[-5:]) - min(p[-5:]) < 0.05 * p[-1]

    def symmetrical_triangle(p):
        max_p = max(p[-10:])
        min_p = min(p[-10:])
        return (max_p - min_p) / max_p < 0.07

    def ascending_triangle(p):
        resistance = max(p[-10:])
        higher_lows = all(p[i] > p[i - 1] for i in range(-9, 0, 2))
        return higher_lows and abs(p[-1] - resistance) < 0.02 * resistance

    def descending_triangle(p):
        support = min(p[-10:])
        lower_highs = all(p[i] < p[i - 1] for i in range(-9, 0, 2))
        return lower_highs and abs(p[-1] - support) < 0.02 * support

    def flag(p):
        flagpole = p[-10:-5]
        flag = p[-5:]
        return slope(flagpole) > 0 and abs(slope(flag)) < 0.05

    def pennant(p):
        recent = p[-5:]
        return max(recent) - min(recent) < 0.03 * recent[-1]

    def cup_and_handle(p):
        mid = len(p) // 2
        cup = p[:mid]
        handle = p[mid:]
        if len(cup) < 3 or len(handle) < 2:
            return False
        cup_depth = max(cup) - min(cup)
        handle_range = max(handle) - min(handle)
        return cup_depth > 0.03 * max(cup) and handle_range < 0.5 * cup_depth

    # --- Pattern Checks ---
    if is_double_top(prices):
        patterns.append("🔺 Double Top")
    if is_double_bottom(prices):
        patterns.append("🔻 Double Bottom")
    if is_triple_top(prices):
        patterns.append("📐 Triple Top")
    if is_triple_bottom(prices):
        patterns.append("📐 Triple Bottom")
    if is_head_and_shoulders(prices):
        patterns.append("👤 Head and Shoulders")
    if is_inverse_head_and_shoulders(prices):
        patterns.append("🧍 Inverse Head and Shoulders")

    if rising_wedge(prices):
        patterns.append("📈 Rising Wedge")
    if falling_wedge(prices):
        patterns.append("📉 Falling Wedge")
    if symmetrical_triangle(prices):
        patterns.append("🔺 Symmetrical Triangle")
    if ascending_triangle(prices):
        patterns.append("📈 Ascending Triangle")
    if descending_triangle(prices):
        patterns.append("📉 Descending Triangle")
    if flag(prices):
        patterns.append("🚩 Flag Pattern")
    if pennant(prices):
        patterns.append("🎌 Pennant Pattern")
    if cup_and_handle(prices):
        patterns.append("☕ Cup and Handle")

    return patterns
