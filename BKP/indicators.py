import numpy as np
import pandas as pd

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    series = pd.Series(closes)
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return round(rsi.iloc[-1], 2) if not rsi.empty else None

def calculate_ema(closes, period=200):
    if len(closes) < period:
        return None
    series = pd.Series(closes)
    ema = series.ewm(span=period, adjust=False).mean()
    return round(ema.iloc[-1], 6)

def calculate_macd(closes, short=12, long=26, signal=9):
    if len(closes) < long + signal + 10:  # More warmup period
        return None, None, None

    series = pd.Series(closes, dtype='float64')
    ema_short = series.ewm(span=short, adjust=False).mean()
    ema_long = series.ewm(span=long, adjust=False).mean()
    macd_line = ema_short - ema_long
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line

    return macd_line.iloc[-1], signal_line.iloc[-1], hist.iloc[-1]


def calculate_stochastic(highs, lows, closes, k_period=14, d_period=3):
    if len(closes) < k_period + d_period:
        return None, None
    high_series = pd.Series(highs)
    low_series = pd.Series(lows)
    close_series = pd.Series(closes)

    lowest_low = low_series.rolling(window=k_period).min()
    highest_high = high_series.rolling(window=k_period).max()

    percent_k = 100 * ((close_series - lowest_low) / (highest_high - lowest_low))
    percent_d = percent_k.rolling(window=d_period).mean()

    return round(percent_k.iloc[-1], 2), round(percent_d.iloc[-1], 2)

def calculate_vwap(highs, lows, closes, volumes):
    if not (len(highs) == len(lows) == len(closes) == len(volumes)):
        return None
    df = pd.DataFrame({
        'high': highs,
        'low': lows,
        'close': closes,
        'volume': volumes
    })
    typical_price = (df['high'] + df['low'] + df['close']) / 3
    vwap = (typical_price * df['volume']).cumsum() / df['volume'].cumsum()
    return round(vwap.iloc[-1], 6)

def calculate_adx(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None, None, None, None

    df = pd.DataFrame({'high': highs, 'low': lows, 'close': closes})
    df['tr'] = df[['high', 'close']].max(axis=1) - df[['low', 'close']].min(axis=1)
    df['+dm'] = np.where((df['high'].diff() > df['low'].diff()) & (df['high'].diff() > 0), df['high'].diff(), 0)
    df['-dm'] = np.where((df['low'].diff() > df['high'].diff()) & (df['low'].diff() > 0), df['low'].diff(), 0)
    tr_smooth = df['tr'].rolling(window=period).sum()
    plus_di = 100 * (df['+dm'].rolling(window=period).sum() / tr_smooth)
    minus_di = 100 * (df['-dm'].rolling(window=period).sum() / tr_smooth)
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
    adx = dx.rolling(window=period).mean()

    return (
        round(adx.iloc[-1], 2),
        round(plus_di.iloc[-1], 2),
        round(minus_di.iloc[-1], 2),
    )

def calculate_bollinger_bands(closes, period=20, num_std=2):
    if len(closes) < period:
        return None, None, None
    series = pd.Series(closes)
    sma = series.rolling(window=period).mean()
    std = series.rolling(window=period).std()
    upper = sma + (num_std * std)
    lower = sma - (num_std * std)
    return round(upper.iloc[-1], 6), round(sma.iloc[-1], 6), round(lower.iloc[-1], 6)
