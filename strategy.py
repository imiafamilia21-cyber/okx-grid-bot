from okx_client import get_okx_demo_client
from config import SYMBOL, GRID_RANGE_PCT, GRID_LEVELS

def fetch_ohlcv(client, symbol, timeframe='15m', limit=100):
    return client.fetch_ohlcv(symbol, timeframe, limit=limit)

def calculate_ema_rsi_atr(ohlcv, ema_period=50, rsi_period=14, atr_period=14):
    closes = [candle[4] for candle in ohlcv]
    highs = [candle[2] for candle in ohlcv]
    lows = [candle[3] for candle in ohlcv]
    
    # EMA
    ema = closes[-1]
    multiplier = 2 / (ema_period + 1)
    for i in range(len(closes)-2, -1, -1):
        ema = closes[i] * multiplier + ema * (1 - multiplier)
    
    # RSI
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    avg_gain = sum(gains[-rsi_period:]) / rsi_period if gains else 0
    avg_loss = sum(losses[-rsi_period:]) / rsi_period if losses else 0
    rs = avg_gain / avg_loss if avg_loss else 0
    rsi = 100 - (100 / (1 + rs)) if rs else 50
    
    # ATR
    tr_list = []
    for i in range(1, len(closes)):
        tr1 = highs[i] - lows[i]
        tr2 = abs(highs[i] - closes[i-1])
        tr3 = abs(lows[i] - closes[i-1])
        tr_list.append(max(tr1, tr2, tr3))
    atr = sum(tr_list[-atr_period:]) / atr_period if tr_list else 0
    
    return {
        'price': closes[-1],
        'ema': ema,
        'rsi': rsi,
        'atr': atr,
        'atr_prev': tr_list[-atr_period-1] if len(tr_list) > atr_period+1 else atr
    }

def is_trending(data):
    price = data['price']
    ema = data['ema']
    rsi = data['rsi']
    atr = data['atr']
    atr_prev = data['atr_prev']
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