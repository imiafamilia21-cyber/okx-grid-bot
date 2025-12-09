import logging
import math
from okx_client import get_okx_demo_client
from config import SYMBOL, MIN_ATR_BASE, MIN_ATR_RANGE

logger = logging.getLogger("strategy")

# Получение свечей
def fetch_ohlcv(client, symbol=SYMBOL, timeframe="15m", limit=100):
    try:
        return client.fetch_ohlcv(symbol, timeframe, limit=limit)
    except Exception as e:
        logger.error(f"❌ Ошибка получения свечей: {e}")
        return []

# Расчет индикаторов
def calculate_ema_rsi_atr(ohlcv, ema_p=50, rsi_p=14, atr_p=14):
    if not ohlcv or len(ohlcv) < max(ema_p, rsi_p, atr_p) + 1:
        logger.warning("Недостаточно данных для расчета индикаторов")
        return {
            "price": 0,
            "ema": 0,
            "rsi": 50,
            "atr": 0,
            "atr_prev": 0,
            "sigma7": 0,
            "min_atr_pct": MIN_ATR_BASE
        }
    
    closes = [c[4] for c in ohlcv]
    highs = [c[2] for c in ohlcv]
    lows = [c[3] for c in ohlcv]
    
    # EMA
    ema = closes[-1]
    mult = 2 / (ema_p + 1)
    for i in range(len(closes)-2, -1, -1):
        ema = closes[i] * mult + ema * (1 - mult)
    
    # RSI
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    
    avg_gain = sum(gains[-rsi_p:]) / rsi_p if gains else 0
    avg_loss = sum(losses[-rsi_p:]) / rsi_p if losses else 0
    rs = avg_gain / avg_loss if avg_loss else 0
    rsi = 100 - (100 / (1 + rs)) if rs else 50
    
    # ATR
    tr_list = []
    for i in range(1, len(closes)):
        tr1 = highs[i] - lows[i]
        tr2 = abs(highs[i] - closes[i-1])
        tr3 = abs(lows[i] - closes[i-1])
        tr_list.append(max(tr1, tr2, tr3))
    
    atr = sum(tr_list[-atr_p:]) / atr_p if tr_list else 0
    atr_prev = sum(tr_list[-atr_p-1:-1]) / atr_p if len(tr_list) > atr_p else (atr * 0.95 if atr else 0)
    
    # Адаптивный минимальный ATR
    if len(closes) >= 7:
        returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, 7)]
        sigma7 = math.sqrt(sum(x*x for x in returns) / 6) * 100
        min_atr = max(0.0025, min(0.0035, sigma7 * 0.8))
    else:
        sigma7 = 0
        min_atr = MIN_ATR_BASE
    
    return {
        "price": closes[-1],
        "ema": ema,
        "rsi": rsi,
        "atr": atr,
        "atr_prev": atr_prev,
        "sigma7": sigma7,
        "min_atr_pct": min_atr
    }

# Определение тренда
def is_trending(data):
    price = data["price"]
    ema = data["ema"]
    rsi = data["rsi"]
    atr = data["atr"]
    atr_prev = data["atr_prev"]
    atr_up = atr > atr_prev * 1.02  # Более мягкое условие
    
    if price > ema and rsi > 50 and atr_up:
        return True, "buy"
    if price < ema and rsi < 50 and atr_up:
        return True, "sell"
    return False, None

# Фильтр объема
def volume_filter(client, symbol=SYMBOL):
    try:
        ticker = client.fetch_ticker(symbol)
        vol24 = float(ticker.get("quoteVolume", 0))
        
        ohlcv7 = client.fetch_ohlcv(symbol, "1d", limit=7)
        if len(ohlcv7) < 7:
            return True
        
        avg7 = sum([c[5] for c in ohlcv7]) / 7
        return vol24 >= avg7 * 0.9
    except Exception as e:
        logger.warning(f"⚠️ Ошибка фильтра объема: {e}")
        return True

# 2-дневный тренд по EMA-21
def ema21_2days(client, symbol=SYMBOL):
    try:
        ohlcv = client.fetch_ohlcv(symbol, "1d", limit=3)
        if len(ohlcv) < 3:
            return False
        
        closes = [c[4] for c in ohlcv[-2:]]
        ema21 = sum(closes) / 2  # Упрощенный расчет
        return all(c > ema21 for c in closes)
    except Exception as e:
        logger.warning(f"⚠️ Ошибка определения 2-дневного тренда: {e}")
        return False

# Отмена всех ордеров
def cancel_all_orders(client, symbol=SYMBOL):
    try:
        orders = client.fetch_open_orders(symbol)
        for order in orders:
            try:
                client.cancel_order(order["id"], symbol)
            except Exception as e:
                logger.warning(f"⚠️ Ошибка отмены ордера {order['id']}: {e}")
    except Exception as e:
        logger.error(f"❌ Ошибка получения ордеров: {e}")

# Установка сетки ордеров
def place_grid_orders(client, symbol=SYMBOL, capital=84, levels=5, upper_pct=None, lower_pct=None):
    try:
        ticker = client.fetch_ticker(symbol)
        price = ticker["last"]
        min_sz = 0.01
        
        if upper_pct is not None and lower_pct is not None:
            upper = price * (1 + upper_pct/100)
            lower = price * (1 - lower_pct/100)
            center = (upper + lower) / 2
        else:
            rng = 18.0 / 100
            upper = price * (1 + rng)
            lower = price * (1 - rng)
            center = price
        
        step = (upper - lower) / (levels * 2)
        amt = capital / (levels * 2)
        
        cancel_all_orders(client, symbol)
        
        for i in range(1, levels + 1):
            buy_p = center - i * step
            sell_p = center + i * step
            buy_sz = max(amt / buy_p, min_sz)
            sell_sz = max(amt / sell_p, min_sz)
            
            try:
                client.create_order(
                    symbol,
                    "limit",
                    "buy",
                    buy_sz,
                    buy_p,
                    params={"tdMode": "cash", "posSide": "net"}
                )
                client.create_order(
                    symbol,
                    "limit",
                    "sell",
                    sell_sz,
                    sell_p,
                    params={"tdMode": "cash", "posSide": "net"}
                )
            except Exception as e:
                logger.warning(f"⚠️ Ошибка установки ордера: {e}")
                
    except Exception as e:
        logger.error(f"❌ Ошибка установки сетки: {e}")