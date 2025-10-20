"""
Indicators

['ADX', 'ALMA', 'AO', 'ATR', 'AccuDist', 'Aroon', 'BB', 'BOP', 'CCI', 'CHOP', 'ChaikinOsc', 'ChandeKrollStop', 'CoppockCurve', 'DEMA', 'DPO', 'DonchianChannels', 
'EMA', 'EMV', 'FibonacciRetracement', 'ForceIndex', 'HMA', 'IBS', 'Ichimoku', 'Indicator', 'KAMA', 'KST', 'KVO', 'KeltnerChannels', 'MACD', 'MassIndex', 'McGinleyDynamic', 
'MeanDev', 'NATR', 'OBV', 'ParabolicSAR', 'PivotsHL', 'ROC', 'RSI', 'RogersSatchell', 'SFX', 'SMA', 'SMMA', 'SOBV', 'STC', 'StdDev', 'Stoch', 'StochRSI', 'SuperTrend', 
'T3', 'TEMA', 'TRIX', 'TSI', 'TTM', 'UO', 'VTX', 'VWAP', 'VWMA', 'WMA', 'ZLEMA', 'ZigZag']
"""

from talipp.indicators import RSI, MACD, EMA, VWAP, ADX, Stoch, BB, OBV, SuperTrend, ATR, CCI, ROC
from collections import defaultdict



# { token_addr: { timeframe: { indicators..., candles: [] } } }
token_state = defaultdict(lambda: defaultdict(dict))

TF_LIST = [1, 5, 8, 10, 15, 30, 45, 60, 120, 180, 300]  # In seconds


def get_indicators_for_token(raw_candles):
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
    
    ema_cross_signal = []

    # Initialize indicators
    rsi = RSI(14)
    ema10 = EMA(10)
    ema20 = EMA(20)
    ema50 = EMA(50)
    ema100 = EMA(100)
    ema200 = EMA(200)
    macd = MACD(12, 26, 9)
    vwap = VWAP()
    adx = ADX(14, 14)
    stoch = Stoch(14, 3)
    boll = BB(20, 2.0)
    obv = OBV()
    atr = ATR(14)
    supertrend = SuperTrend(10, 3)
    cci = CCI(20)
    roc = ROC(12)

    # print("RSI", dir(rsi))

    # Feed candles to each
    for candle in candles:
        close = candle.close
        rsi.add(close)
        ema10.add(close)
        ema20.add(close)
        ema50.add(close)
        ema100.add(close)
        ema200.add(close)
        macd.add(close)
        boll.add(close)
        roc.add(close)

        vwap.add(candle)
        adx.add(candle)
        stoch.add(candle)
        obv.add(candle)
        atr.add(candle)
        supertrend.add(candle)
        cci.add(candle)
        
        # EMA Cross Signals
        try:
            if len(ema10) >= 2 and len(ema50) >= 2:
                prev_cross = ema_cross_signal[-1] if ema_cross_signal else 0
                if ema10[-1] > ema50[-1] and ema10[-2] <= ema50[-2]:
                    ema_cross_signal.append(1)   # golden cross
                elif ema10[-1] < ema50[-1] and ema10[-2] >= ema50[-2]:
                    ema_cross_signal.append(-1)  # death cross
                else:
                    ema_cross_signal.append(prev_cross)
            else:
                ema_cross_signal.append(0)
        except Exception:
            ema_cross_signal.append(0)

    def safe_val(indicator, attr=None):
        try:
            if not indicator or len(indicator) == 0:
                return None
            val = getattr(indicator[-1], attr) if attr else indicator[-1]
            return round(val, 2) if isinstance(val, (int, float)) else val
        except Exception:
            return None


    return {
        "rsi": safe_val(rsi),
        "ema10": safe_val(ema10),
        "ema20": safe_val(ema20),
        "ema50": safe_val(ema50),
        "ema100": safe_val(ema100),
        "ema200": safe_val(ema200),
        "ema_cross": ema_cross_signal[-1] if ema_cross_signal else 0,
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
        "boll_percent": (close - safe_val(boll, "lb")) / (safe_val(boll, "ub") - safe_val(boll, "lb")) if boll and boll[-1] else None,
        "atr": [float(x) for x in atr if x is not None] if atr else [],
        "obv": [float(x) for x in obv if x is not None] if obv else [],
        "supertrend": [
                        {"value": float(t.value), "trend": str(t.trend)}
                        for t in supertrend if t is not None
                    ],
        "cci": safe_val(cci),
        "roc": safe_val(roc),
        "momentum3": round(candles[-1].close - candles[-4].close, 2) if len(candles) >= 4 else None,



    }




class Candle:
    def __init__(self, open, high, low, close, volume=1.0):
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume

