from okx_client import get_okx_demo_client

def fetch_ohlcv(client, symbol, timeframe='15m', limit=100):
    return client.fetch_ohlcv(symbol, timeframe, limit=limit)

def calculate_ema_rsi_atr(ohlcv, ema_period=50, rsi_period=14, atr_period=14):
    if len(ohlcv) < max(ema_period, rsi_period, atr_period) + 1:
        raise ValueError("Недостаточно данных для расчёта индикаторов")
    
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
            except Exception:
                pass
    except Exception:
        pass

def place_grid_orders(client, symbol, capital_usdt, upper_pct=None, lower_pct=None):
    ticker = client.fetch_ticker(symbol)
    price = ticker['last']
    min_size = 0.01  # минимальный размер для ETH

    if upper_pct is not None and lower_pct is not None:
        upper = price * (1 + upper_pct / 100)
        lower = price * (1 - lower_pct / 100)
        center = (upper + lower) / 2
        grid_levels = 6
        step = (upper - lower) / (grid_levels * 2)
    else:
        grid_range_pct = 15.0  # увеличен для ETH
        lower = price * (1 - grid_range_pct / 100)
        upper = price * (1 + grid_range_pct / 100)
        center = price
        grid_levels = 6
        step = (upper - lower) / (grid_levels * 2)

    total_levels = grid_levels * 2
    for i in range(1, grid_levels + 1):
        # Покупки
        buy_price = center - i * step
        buy_usd = capital_usdt / total_levels
        buy_size = buy_usd / buy_price
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
            except Exception:
                pass
        
        # Продажи
        sell_price = center + i * step
        sell_usd = capital_usdt / total_levels
        sell_size = sell_usd / sell_price
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
            except Exception:
                pass