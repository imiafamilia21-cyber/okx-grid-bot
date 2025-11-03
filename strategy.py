import pandas as pd
import numpy as np
from okx_client import get_okx_demo_client
from config import SYMBOL, GRID_RANGE_PCT, GRID_LEVELS


def fetch_ohlcv(client, symbol, timeframe='15m', limit=100):
    data = client.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    return df


def calculate_ema_rsi_atr(df, ema_period=50, rsi_period=14, atr_period=14):
    df['ema'] = df['close'].ewm(span=ema_period).mean()
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(rsi_period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(rsi_period).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    tr1 = df['high'] - df['low']
    tr2 = abs(df['high'] - df['close'].shift())
    tr3 = abs(df['low'] - df['close'].shift())
    df['tr'] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df['atr'] = df['tr'].rolling(atr_period).mean()
    return df


def is_trending(df):
    price = df['close'].iloc[-1]
    ema = df['ema'].iloc[-1]
    rsi = df['rsi'].iloc[-1]
    atr = df['atr'].iloc[-1]
    atr_prev = df['atr'].iloc[-2]
    atr_increasing = atr > atr_prev * 1.05
    if price > ema and rsi > 55 and atr_increasing:
        return True, 'buy'
    elif price < ema and rsi < 45 and atr_increasing:
        return True, 'sell'
    else:
        return False, None


def cancel_all_orders(client, symbol):
    try:
        orders = client.fetch_open_orders(symbol)
        for order in orders:
            try:
                client.cancel_order(order['id'], symbol)
            except:
                pass
    except:
        pass


def place_grid_orders(client, symbol, capital_usdt):
    ticker = client.fetch_ticker(symbol)
    price = ticker['last']
    lower = price * (1 - GRID_RANGE_PCT / 100)
    upper = price * (1 + GRID_RANGE_PCT / 100)
    step = (upper - lower) / (GRID_LEVELS * 2)
    amount_per_level = capital_usdt / (GRID_LEVELS * 2)
    min_size = 0.01

    for i in range(1, GRID_LEVELS + 1):
        buy_price = price - i * step
        buy_size = max(amount_per_level / buy_price, min_size)
        try:
            client.create_order(
                symbol=symbol,
                type='limit',
                side='buy',
                amount=buy_size,
                price=buy_price,
                params={'posSide': 'net'}
            )
        except Exception as e:
            print(f"Buy error: {e}")

        sell_price = price + i * step
        sell_size = max(amount_per_level / sell_price, min_size)
        try:
            client.create_order(
                symbol=symbol,
                type='limit',
                side='sell',
                amount=sell_size,
                price=sell_price,
                params={'posSide': 'net'}
            )
        except Exception as e:
            print(f"Sell error: {e}")