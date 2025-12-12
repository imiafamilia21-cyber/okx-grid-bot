from okx_client import get_okx_demo_client

def fetch_ohlcv(client, symbol, timeframe='15m', limit=100):
    return client.fetch_ohlcv(symbol, timeframe, limit=limit)

def calculate_ema_rsi_atr(ohlcv, ema_period=50, rsi_period=14, atr_period=14):
    if len(ohlcv) < max(ema_period, rsi_period, atr_period) + 1:
        return {'price': ohlcv[-1][4], 'ema': ohlcv[-1][4], 'rsi': 50, 'atr': 0, 'atr_prev': 0}
    
    closes = [candle[4] for candle in ohlcv]
    highs = [candle[2] for candle in ohlcv]
    lows = [candle[3] for candle in ohlcv]
    
    ema = closes[-1]
    multiplier = 2 / (ema_period + 1)
    for i in range(len(closes)-2, -1, -1):
        ema = closes[i] * multiplier + ema * (1 - multiplier)
    
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    avg_gain = sum(gains[-rsi_period:]) / rsi_period if gains else 0
    avg_loss = sum(losses[-rsi_period:]) / rsi_period if losses else 0
    rs = avg_gain / avg_loss if avg_loss else 0
    rsi = 100 - (100 / (1 + rs)) if rs else 50
    
    tr_list = []
    for i in range(1, len(closes)):
        tr1 = highs[i] - lows[i]
        tr2 = abs(highs[i] - closes[i-1])
        tr3 = abs(lows[i] - closes[i-1])
        tr_list.append(max(tr1, tr2, tr3))
    atr = sum(tr_list[-atr_period:]) / atr_period if tr_list else 0
    atr_prev = tr_list[-atr_period-1] if len(tr_list) > atr_period else (atr * 0.95)
    
    return {
        'price': closes[-1],
        'ema': ema,
        'rsi': rsi,
        'atr': atr,
        'atr_prev': atr_prev
    }

def is_trending(data):
    price = data['price']
    ema = data['ema']
    rsi = data['rsi']
    atr = data['atr']
    atr_prev = data.get('atr_prev', atr * 0.95)
    atr_increasing = atr > atr_prev * 1.02
    
    if price > ema and rsi > 50 and atr_increasing:
        return True, 'buy'
    elif price < ema and rsi < 50 and atr_increasing:
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

def place_grid_orders(client, symbol, capital_usdt, upper_pct=None, lower_pct=None):
    ticker = client.fetch_ticker(symbol)
    price = ticker['last']
    min_size = 0.01  # минимальный размер для ETH

    # Уменьшаем до 3 уровней = 6 ордеров
    grid_levels = 3
    grid_range_pct = 20.0  # расширяем диапазон

    lower = price * (1 - grid_range_pct / 100)
    upper = price * (1 + grid_range_pct / 100)
    center = price
    step = (upper - lower) / (grid_levels * 2)

    total_usd = capital_usdt
    total_levels = grid_levels * 2
    usd_per_level = total_usd / total_levels

    for i in range(1, grid_levels + 1):
        buy_price = center - i * step
        buy_size = usd_per_level / buy_price
        if buy_size >= min_size:
            try:
                client.create_order(
                    symbol=symbol,
                    type='limit',
                    side='buy',
                    amount=buy_size,
                    price=buy_price,
                    params={'tdMode': 'isolated', 'posSide': 'net'}
                )
            except:
                pass
        
        sell_price = center + i * step
        sell_size = usd_per_level / sell_price
        if sell_size >= min_size:
            try:
                client.create_order(
                    symbol=symbol,
                    type='limit',
                    side='sell',
                    amount=sell_size,
                    price=sell_price,
                    params={'tdMode': 'isolated', 'posSide': 'net'}
                )
            except:
                pass