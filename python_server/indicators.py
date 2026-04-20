from collections import deque

from talipp.indicators import (
    ADX,
    ATR,
    BB,
    CCI,
    EMA,
    MACD,
    OBV,
    ROC,
    RSI,
    Stoch,
    SuperTrend,
    VWAP,
)


class Candle:
    def __init__(self, open, high, low, close, volume=1.0):
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


def _safe_last(indicator, attr=None, digits=2):
    try:
        if not indicator or len(indicator) == 0:
            return None
        value = getattr(indicator[-1], attr) if attr else indicator[-1]
        return round(value, digits) if isinstance(value, (int, float)) else value
    except Exception:
        return None


class IncrementalIndicatorEngine:
    def __init__(self):
        self.rsi = RSI(14)
        self.ema10 = EMA(10)
        self.ema20 = EMA(20)
        self.ema50 = EMA(50)
        self.ema100 = EMA(100)
        self.ema200 = EMA(200)
        self.macd = MACD(12, 26, 9)
        self.vwap = VWAP()
        self.adx = ADX(14, 14)
        self.stoch = Stoch(14, 3)
        self.boll = BB(20, 2.0)
        self.obv = OBV()
        self.atr = ATR(14)
        self.supertrend = SuperTrend(10, 3)
        self.cci = CCI(20)
        self.roc = ROC(12)

        # We only need the recent closes for momentum and the latest close for snapshots.
        self.recent_closes = deque(maxlen=4)
        self.last_close = None
        self.ema_cross = 0

    def update(self, candle_data):
        candle = Candle(
            open=candle_data["open"],
            high=candle_data["high"],
            low=candle_data["low"],
            close=candle_data["close"],
            volume=candle_data.get("volume", 1),
        )

        close = candle.close
        self.last_close = close
        self.recent_closes.append(close)
        self.rsi.add(close)
        self.ema10.add(close)
        self.ema20.add(close)
        self.ema50.add(close)
        self.ema100.add(close)
        self.ema200.add(close)
        self.macd.add(close)
        self.boll.add(close)
        self.roc.add(close)

        self.vwap.add(candle)
        self.adx.add(candle)
        self.stoch.add(candle)
        self.obv.add(candle)
        self.atr.add(candle)
        self.supertrend.add(candle)
        self.cci.add(candle)

        try:
            if len(self.ema10) >= 2 and len(self.ema50) >= 2:
                if self.ema10[-1] > self.ema50[-1] and self.ema10[-2] <= self.ema50[-2]:
                    self.ema_cross = 1
                elif self.ema10[-1] < self.ema50[-1] and self.ema10[-2] >= self.ema50[-2]:
                    self.ema_cross = -1
        except Exception:
            pass

    def snapshot(self):
        close = self.last_close
        boll_upper = _safe_last(self.boll, "ub")
        boll_lower = _safe_last(self.boll, "lb")
        supertrend_last = self.supertrend[-1] if self.supertrend and len(self.supertrend) else None

        boll_percent = None
        if close is not None and boll_upper is not None and boll_lower is not None and boll_upper != boll_lower:
            boll_percent = (close - boll_lower) / (boll_upper - boll_lower)

        return {
            "rsi": _safe_last(self.rsi),
            "ema10": _safe_last(self.ema10),
            "ema20": _safe_last(self.ema20),
            "ema50": _safe_last(self.ema50),
            "ema100": _safe_last(self.ema100),
            "ema200": _safe_last(self.ema200),
            "ema_cross": self.ema_cross,
            "macd_line": _safe_last(self.macd, "macd"),
            "macd_signal": _safe_last(self.macd, "signal"),
            "macd_hist": _safe_last(self.macd, "histogram"),
            "vwap": _safe_last(self.vwap),
            "adx": _safe_last(self.adx, "adx"),
            "plus_di": _safe_last(self.adx, "plus_di"),
            "minus_di": _safe_last(self.adx, "minus_di"),
            "stoch_k": _safe_last(self.stoch, "k"),
            "stoch_d": _safe_last(self.stoch, "d"),
            "boll_upper": boll_upper,
            "boll_middle": _safe_last(self.boll, "cb"),
            "boll_lower": boll_lower,
            "boll_percent": boll_percent,
            "atr": _safe_last(self.atr),
            "obv": _safe_last(self.obv),
            "supertrend_value": (
                float(supertrend_last.value) if supertrend_last is not None else None
            ),
            "supertrend_trend": (
                str(supertrend_last.trend) if supertrend_last is not None else None
            ),
            "cci": _safe_last(self.cci),
            "roc": _safe_last(self.roc),
            "momentum3": (
                round(self.recent_closes[-1] - self.recent_closes[0], 2)
                if len(self.recent_closes) >= 4
                else None
            ),
        }

    def legacy_snapshot(self):
        close = self.last_close
        boll_upper = _safe_last(self.boll, "ub")
        boll_lower = _safe_last(self.boll, "lb")

        boll_percent = None
        if close is not None and boll_upper is not None and boll_lower is not None and boll_upper != boll_lower:
            boll_percent = (close - boll_lower) / (boll_upper - boll_lower)

        return {
            "rsi": _safe_last(self.rsi),
            "ema10": _safe_last(self.ema10),
            "ema20": _safe_last(self.ema20),
            "ema50": _safe_last(self.ema50),
            "ema100": _safe_last(self.ema100),
            "ema200": _safe_last(self.ema200),
            "ema_cross": self.ema_cross,
            "macd_line": _safe_last(self.macd, "macd"),
            "macd_signal": _safe_last(self.macd, "signal"),
            "macd_hist": _safe_last(self.macd, "histogram"),
            "vwap": _safe_last(self.vwap),
            "adx": _safe_last(self.adx, "adx"),
            "plus_di": _safe_last(self.adx, "plus_di"),
            "minus_di": _safe_last(self.adx, "minus_di"),
            "stoch_k": _safe_last(self.stoch, "k"),
            "stoch_d": _safe_last(self.stoch, "d"),
            "boll_upper": boll_upper,
            "boll_middle": _safe_last(self.boll, "cb"),
            "boll_lower": boll_lower,
            "boll_percent": boll_percent,
            "atr": [float(x) for x in self.atr if x is not None] if self.atr else [],
            "obv": [float(x) for x in self.obv if x is not None] if self.obv else [],
            "supertrend": [
                {"value": float(item.value), "trend": str(item.trend)}
                for item in self.supertrend
                if item is not None
            ],
            "cci": _safe_last(self.cci),
            "roc": _safe_last(self.roc),
            "momentum3": (
                round(self.recent_closes[-1] - self.recent_closes[0], 2)
                if len(self.recent_closes) >= 4
                else None
            ),
        }


def get_indicators_for_token(raw_candles):
    engine = IncrementalIndicatorEngine()
    for candle in raw_candles:
        engine.update(candle)
    return engine.legacy_snapshot()
