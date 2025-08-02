from talipp.indicators import RSI, MACD, EMA, VWAP, ADX, Stochastic, BollingerBands
from talipp.indicators.Indicator import Candle
from collections import defaultdict

# { token_addr: { timeframe: { indicators..., candles: [] } } }
token_state = defaultdict(lambda: defaultdict(dict))

def get_indicators_for_token(token_addr, tf, new_candles):
    from talipp.aggregation import Timeframe

    if 'candles' not in token_state[token_addr][tf]:
        # Initialize indicators and candle history
        token_state[token_addr][tf]['candles'] = []

        token_state[token_addr][tf]['rsi'] = RSI(14)
        token_state[token_addr][tf]['ema200'] = EMA(200)
        token_state[token_addr][tf]['macd'] = MACD()
        token_state[token_addr][tf]['vwap'] = VWAP()
        token_state[token_addr][tf]['adx'] = ADX()
        token_state[token_addr][tf]['stoch'] = Stochastic()
        token_state[token_addr][tf]['boll'] = BollingerBands()

    state = token_state[token_addr][tf]
    indicators = ['rsi', 'ema200', 'macd', 'vwap', 'adx', 'stoch', 'boll']

    # Convert to talipp candles and append
    for c in new_candles:
        candle = Candle(
            open=c['open'], high=c['high'], low=c['low'],
            close=c['close'], volume=c.get('volume', 1)
        )
        state['candles'].append(candle)
        for name in indicators:
            state[name].add(candle)

    # Optional: Limit memory
    state['candles'] = state['candles'][-300:]

    return {
        "rsi": round(state['rsi'][-1], 2) if state['rsi'] else None,
        "ema200": round(state['ema200'][-1], 6) if state['ema200'] else None,
        "macd_line": round(state['macd'][-1].macd, 6) if state['macd'] else None,
        "macd_signal": round(state['macd'][-1].signal, 6) if state['macd'] else None,
        "macd_hist": round(state['macd'][-1].histogram, 6) if state['macd'] else None,
        "vwap": round(state['vwap'][-1], 6) if state['vwap'] else None,
        "adx": round(state['adx'][-1].adx, 2) if state['adx'] else None,
        "plus_di": round(state['adx'][-1].plus_di, 2) if state['adx'] else None,
        "minus_di": round(state['adx'][-1].minus_di, 2) if state['adx'] else None,
        "stoch_k": round(state['stoch'][-1].k, 2) if state['stoch'] else None,
        "stoch_d": round(state['stoch'][-1].d, 2) if state['stoch'] else None,
        "boll_upper": round(state['boll'][-1].upper, 6) if state['boll'] else None,
        "boll_middle": round(state['boll'][-1].middle, 6) if state['boll'] else None,
        "boll_lower": round(state['boll'][-1].lower, 6) if state['boll'] else None,
    }
