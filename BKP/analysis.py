def analyze_token(metrics):
    """Analyze token indicators and return signal tags or score."""
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
    
    choch_result, choch_note = detect_choch(highs, lows, volumes)
    result["choch"] = choch_result
    result["notes"].append(choch_note)

    rsi = metrics.get("rsi")
    macd = metrics.get("macd_line")
    signal = metrics.get("macd_signal")
    histogram = metrics.get("macd_hist")
    stoch_k = metrics.get("stoch_k")
    stoch_d = metrics.get("stoch_d")
    ema200 = metrics.get("ema200")
    vwap = metrics.get("vwap")
    adx = metrics.get("adx")
    plus_di = metrics.get("plus_di")
    minus_di = metrics.get("minus_di")
    boll_upper = metrics.get("boll_upper")
    boll_middle = metrics.get("boll_middle")
    boll_lower = metrics.get("boll_lower")
    price = metrics.get("close")

    ## 🔹 Trend Strength
    if adx and adx > 25:
        result["trend_strength"] = "Strong"
        result["notes"].append("✅ Strong trend (ADX > 25)")
    elif adx and adx < 20:
        result["trend_strength"] = "Weak"
        result["notes"].append("⚠️ Weak trend (ADX < 20)")

    ## ⚡ Momentum
    if rsi:
        if rsi < 30:
            result["momentum"] = "Oversold"
            result["notes"].append("🟢 RSI < 30 (possible buy)")
        elif rsi > 70:
            result["momentum"] = "Overbought"
            result["notes"].append("🔴 RSI > 70 (possible sell)")

    if macd and signal and histogram:
        if macd > signal and histogram > 0:
            result["momentum"] = "Bullish Momentum"
            result["notes"].append("📈 MACD crossover")
        elif macd < signal and histogram < 0:
            result["momentum"] = "Bearish Momentum"
            result["notes"].append("📉 MACD crossover")

    ## 📉 Volatility
    if boll_upper and boll_lower and price:
        band_range = boll_upper - boll_lower
        if band_range / price > 0.2:
            result["volatility"] = "High"
            result["notes"].append("🌊 High volatility (wide BB)")
        elif band_range / price < 0.05:
            result["volatility"] = "Low"
            result["notes"].append("💤 Low volatility (tight BB)")

    ## 📊 Overall score (can be expanded)
    positive_signals = len([n for n in result["notes"] if "✅" in n or "🟢" in n or "📈" in n])
    negative_signals = len([n for n in result["notes"] if "⚠️" in n or "🔴" in n or "📉" in n])
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

