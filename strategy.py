import logging, os, requests, json
from okx_client import get_okx_demo_client
from config import SYMBOL
logger = logging.getLogger("strategy")

# ---------- 1. fetch ----------
def fetch_ohlcv(client, symbol=SYMBOL, timeframe="15m", limit=100):
    try:
        return client.fetch_ohlcv(symbol, timeframe, limit=limit)
    except Exception as e:
        logger.error(f"fetch_ohlcv: {e}")
        return []

# ---------- 2. indicators ----------
def calculate_ema_rsi_atr(ohlcv, ema_p=50, rsi_p=14, atr_p=14):
    closes = [c[4] for c in ohlcv]
    highs  = [c[2] for c in ohlcv]
    lows   = [c[3] for c in ohlcv]

    ema = closes[-1]
    mult = 2 / (ema_p + 1)
    for i in range(len(closes)-2, -1, -1):
        ema = closes[i]*mult + ema*(1-mult)

    deltas = [closes[i]-closes[i-1] for i in range(1, len(closes))]
    gains  = [d for d in deltas if d>0]
    losses = [-d for d in deltas if d<0]
    avg_gain = sum(gains[-rsi_p:])/rsi_p if gains else 0
    avg_loss = sum(losses[-rsi_p:])/rsi_p if losses else 0
    rs = avg_gain/avg_loss if avg_loss else 0
    rsi = 100 - (100/(1+rs)) if rs else 50

    tr_list = []
    for i in range(1, len(closes)):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        tr_list.append(tr)
    atr = sum(tr_list[-atr_p:])/atr_p if tr_list else 0
    atr_prev = sum(tr_list[-atr_p-1:-1])/atr_p if len(tr_list)>atr_p else atr*0.95

    # 3. адаптивный мин ATR
    sigma7 = (sum([(closes[-i]-closes[-i-1])**2 for i in range(1,8)])/7)**0.5 / closes[-1]
    min_atr = max(MIN_ATR_BASE - MIN_ATR_RANGE + sigma7*2, 0.0025)

    return {"price": closes[-1], "ema": ema, "rsi": rsi, "atr": atr,
            "atr_prev": atr_prev, "sigma7": sigma7, "min_atr_pct": min_atr}

# ---------- 3. trend ----------
def is_trending(data):
    price = data["price"]
    ema   = data["ema"]
    rsi   = data["rsi"]
    atr   = data["atr"]
    atr_prev = data["atr_prev"]
    atr_up = atr > atr_prev*1.05
    if price>ema and rsi>50 and atr_up:
        return True, "buy"
    if price<ema and rsi<50 and atr_up:
        return True, "sell"
    return False, None

# ---------- 4. volume filter ----------
def volume_filter(client, symbol=SYMBOL):
    try:
        ticker = client.fetch_ticker(symbol)
        vol24 = float(ticker["quoteVolume"])
        ohlcv7 = client.fetch_ohlcv(symbol, "1d", limit=7)
        avg7 = sum([c[5] for c in ohlcv7])/7
        return vol24 >= avg7*0.9
    except:
        return False

# ---------- 5. 2-дневный тренд ----------
def ema21_2days(client, symbol=SYMBOL):
    try:
        ohlcv = client.fetch_ohlcv(symbol, "1d", limit=3)
        closes = [c[4] for c in ohlcv[-2:]]
        ema21  = sum(closes)/2          # упрощённо
        return closes[-1] > ema21
    except:
        return False

# ---------- 6. orders ----------
def cancel_all_orders(client, symbol=SYMBOL):
    try:
        for o in client.fetch_open_orders(symbol):
            client.cancel_order(o["id"], symbol)
    except:
        pass

def place_grid_orders(client, symbol=SYMBOL, capital=84, levels=5,
                      upper_pct=None, lower_pct=None):
    ticker = client.fetch_ticker(symbol)
    price  = ticker["last"]
    min_sz = 0.01

    if upper_pct is not None and lower_pct is not None:
        upper = price * (1 + upper_pct/100)
        lower = price * (1 - lower_pct/100)
        center = (upper+lower)/2
    else:
        rng = 18.0/100
        upper = price*(1+rng)
        lower = price*(1-rng)
        center = price

    step = (upper-lower)/(levels*2)
    amt  = capital/(levels*2)

    cancel_all_orders(client, symbol)
    for i in range(1, levels+1):
        buy_p = center - i*step
        sell_p = center + i*step
        buy_sz  = max(amt/buy_p, min_sz)
        sell_sz = max(amt/sell_p, min_sz)
        try:
            client.create_order(symbol, "limit", "buy",  buy_sz,  buy_p,  params={"tdMode":"cash","posSide":"net"})
            client.create_order(symbol, "limit", "sell", sell_sz, sell_p, params={"tdMode":"cash","posSide":"net"})
        except:
            pass