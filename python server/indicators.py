"""
Indicators

['ADX', 'ALMA', 'AO', 'ATR', 'AccuDist', 'Aroon', 'BB', 'BOP', 'CCI', 'CHOP', 'ChaikinOsc', 'ChandeKrollStop', 'CoppockCurve', 'DEMA', 'DPO', 'DonchianChannels', 'EMA', 'EMV', 'FibonacciRetracement', 'ForceIndex', 'HMA', 'IBS', 'Ichimoku', 'Indicator', 'KAMA', 'KST', 'KVO', 'KeltnerChannels', 'MACD', 'MassIndex', 'McGinleyDynamic', 'MeanDev', 'NATR', 'OBV', 'ParabolicSAR', 'PivotsHL', 'ROC', 'RSI', 'RogersSatchell', 'SFX', 'SMA', 'SMMA', 'SOBV', 'STC', 'StdDev', 'Stoch', 'StochRSI', 'SuperTrend', 'T3', 'TEMA', 'TRIX', 'TSI', 'TTM', 'UO', 'VTX', 'VWAP', 'VWMA', 'WMA', 'ZLEMA', 'ZigZag', '__all__', '__builtins__', '__cached__', '__doc__', '__file__', '__loader__', '__name__', '__package__', '__path__', '__spec__']
"""

from talipp.indicators import RSI, MACD, EMA, VWAP, ADX, Stoch, BB
from collections import defaultdict



# { token_addr: { timeframe: { indicators..., candles: [] } } }
token_state = defaultdict(lambda: defaultdict(dict))

TF_LIST = [1, 5, 8, 10, 15, 30, 45, 60, 120, 180, 300]  # In seconds

# def init_timeframes(token_addr):
#     for tf_sec in TF_LIST:
#         tf_str = f"{tf_sec}s" if tf_sec < 60 else f"{tf_sec // 60}m"
#         token_state[token_addr][tf_str] = {
#             "candles": [],
#             "rsi": RSI(14),
#             "ema200": EMA(200),
#             "macd": MACD(12, 26, 9),
#             "vwap": VWAP(),
#             "adx": ADX(14, 14),
#             "stoch": Stoch(14, 3),
#             "boll": BB(20, 2.0)
#         }

# def process_new_candles(token_addr, raw_candles):
#     if token_addr not in token_state:
#         init_timeframes(token_addr)

#     for raw in raw_candles:
#         candle = Candle(
#             open=raw['open'],
#             high=raw['high'],
#             low=raw['low'],
#             close=raw['close'],
#             volume=raw.get('volume', 1)
#         )

#         for tf, state in token_state[token_addr].items():
#             new_candles = state["aggregator"].add(candle)
#             for c in new_candles:
#                 state["candles"].append(c)
#                 for key in ["rsi", "ema200", "macd", "vwap", "adx", "stoch", "boll"]:
#                     state[key].add(c if key in ['vwap', 'adx', 'stoch'] else c.close)
#                 state["candles"] = state["candles"][-300:]



def get_indicators_for_token(token_addr, tf, raw_candles):
    # Convert dicts to Candle objects
    candles = [
        Candle(
            open=c["open"],
            high=c["high"],
            low=c["low"],
            close=c["close"],
            volume=c.get("volume", 1)
        )
        for c in raw_candles
    ]

    # Initialize indicators
    rsi = RSI(14)
    ema = EMA(200)
    macd = MACD(12, 26, 9)
    vwap = VWAP()
    adx = ADX(14, 14)
    stoch = Stoch(14, 3)
    boll = BB(20, 2.0)

    # print("RSI", dir(rsi))

    # Feed candles to each
    for candle in candles:
        close = candle.close
        rsi.add(close)
        ema.add(close)
        macd.add(close)
        boll.add(close)

        vwap.add(candle)
        adx.add(candle)
        stoch.add(candle)

    def safe_val(indicator, attr=None):
        if not indicator or len(indicator) == 0:
            return None
        val = getattr(indicator[-1], attr) if attr else indicator[-1]
        if val is None:
            return None
        return round(val, 2)

    return {
        "rsi": safe_val(rsi),
        "ema200": safe_val(ema),
        "macd_line": safe_val(macd, "macd"),
        "macd_signal": safe_val(macd, "signal"),
        "macd_hist": safe_val(macd, "histogram"),
        "vwap": safe_val(vwap),
        "adx": safe_val(adx, "adx"),
        "plus_di": safe_val(adx, "plus_di"),
        "minus_di": safe_val(adx, "minus_di"),
        "stoch_k": safe_val(stoch, "k"),
        "stoch_d": safe_val(stoch, "d"),
        "boll_upper": safe_val(boll, "ub"),
        "boll_middle": safe_val(boll, "cb"),
        "boll_lower": safe_val(boll, "lb"),
    }




class Candle:
    def __init__(self, open, high, low, close, volume=1.0):
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume

