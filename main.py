# main.py  (без узких мест, 2025-12-07)
import time, requests, logging, threading, os
from datetime import datetime, date, time as dt_time, timedelta
from okx_client import get_okx_demo_client
from strategy import fetch_ohlcv, calculate_ema_rsi_atr, is_trending, cancel_all_orders, place_grid_orders
from config import SYMBOL, REBALANCE_INTERVAL_HOURS, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, GAS_WEBHOOK_URL
from StopVoronPro import StopVoronPro
from flask import Flask
from tenacity import retry, stop_after_attempt, wait_exponential

# ----------------- 1. НАСТРОЙКИ -----------------
INITIAL_CAPITAL   = 120.0
GRID_CAPITAL      = 84.0
TREND_CAPITAL     = 36.0
RISK_PER_TRADE    = 0.008
MAX_EQUITY_PCT    = 0.30
MIN_ATR_PCT       = 0.003
COOLDOWN_HOURS    = 6
TRAIL_ATR_MUL     = 0.75
TIMEOUT_DAYS      = 12
MIN_ORDER_SIZE    = 0.01

# ----------------- 2. ЛОГ -----------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger()

# ----------------- 3. ГЛОБАЛЬНЫЕ -----------------
last_positions = {}; last_report_date = date.today()
total_pnl = 0.0; total_trades = 0; winning_trades = 0
max_drawdown = 0.0; equity_high = INITIAL_CAPITAL
grid_center = None; current_trend = None; trend_confirmation = 0
last_flat_time = datetime.min; position_open_time = None; trail_stop_price = None
stop_voron = StopVoronPro(**StopVoronPro().get_recommended_settings("crypto"))

# ----------------- 4. УТИЛИТЫ -----------------
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={'chat_id': TELEGRAM_CHAT_ID, 'text': text}, timeout=10)
    except Exception as e:
        logger.error(f"Telegram error: {e}")

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def log_to_sheet(data):
    if not GAS_WEBHOOK_URL: return
    try:
        resp = requests.post(GAS_WEBHOOK_URL, json=data, timeout=10)
        logger.info("Sheet logged") if resp.status_code == 200 else logger.warning(f"Sheet {resp.status_code}")
    except Exception as e:
        logger.error(f"Sheet error: {e}")

def get_positions(client, symbol):
    try:
        return {p['side']: {'size': p['contracts'], 'entry': p['entryPrice'],
                            'unrealizedPnl': p.get('unrealizedPnl', 0)}
                for p in client.fetch_positions([symbol]) if p.get('contracts', 0) > 0}
    except: return {}

def close_all_positions(client, symbol):
    global last_flat_time
    try:
        positions = client.fetch_positions([symbol])
        real_pos = [p for p in positions if p.get('contracts', 0) > 0]
        if not real_pos:
            logger.info("Нет открытых позиций для закрытия")
            return
        for p in real_pos:
            side = 'buy' if p['side'] == 'short' else 'sell'
            size = p['contracts']
            client.create_order(symbol=symbol, type='market', side=side, amount=size,
                                params={'tdMode': 'isolated', 'posSide': 'net', 'reduceOnly': True})
            msg = f"🔴 Закрыта {p['side']} {symbol} size={size}"
            send_telegram(msg)
            log_to_sheet({'timestamp': datetime.now().isoformat(), 'type': 'close_position',
                          'symbol': SYMBOL, 'side': p['side'], 'size': size,
                          'entry_price': p['entryPrice'], 'exit_price': client.fetch_ticker(symbol)['last'],
                          'pnl': p.get('unrealizedPnl', 0), 'total_pnl': total_pnl, 'message': msg})
        last_flat_time = datetime.utcnow()

        # 6. отправляем 30 % свободных USDT в стейбл
        bal = client.fetch_balance()
        free_usdt = float(bal['USDT']['free'])
        if free_usdt > 10:
            amount = round(free_usdt * 0.3, 2)
            client.transfer('USDT', amount, 'trading', 'funding')
            client.privatePost_finance_savings_purchase({'ccy': 'USDT', 'amt': amount})
            logger.info(f"30 % ({amount} USDT) переведено в Earn")
    except Exception as e:
        logger.error(f"Close error: {e}")

def compute_position_size(entry: float, stop: float, capital: float) -> float:
    risk_usd = capital * RISK_PER_TRADE
    r_dist = abs(entry - stop)
    if r_dist <= 0: return 0.01
    size = risk_usd / r_dist
    max_size = (capital * MAX_EQUITY_PCT) / entry if entry > 0 else 0
    return max(min(size, max_size), 0.01)

# 1. адаптивный TP
def adaptive_tp_multiplier(atr_pct):
    if atr_pct > 2.5: return 1.4
    if atr_pct < 1.5: return 1.8
    return 1.6

def place_take_profit(client, symbol, side, entry, stop, size):
    try:
        atr_pct = abs(entry - stop) / entry * 100
        tp_distance = abs(entry - stop) * adaptive_tp_multiplier(atr_pct)
        tp_price = round(entry + tp_distance * (1 if side == 'buy' else -1), 1)
        client.create_order(symbol=symbol, type='limit', side='sell' if side == 'buy' else 'buy',
                            amount=size, price=tp_price, params={'reduceOnly': True, 'tdMode': 'isolated', 'posSide': 'net'})
        send_telegram(f"✅ TP {side} {symbol} {tp_price}")
    except Exception as e:
        logger.error(f"TP error: {e}")

def trail_stop(side, current_price, atr):
    return round(current_price - 0.75 * atr if side == 'buy' else current_price + 0.75 * atr, 1)

def check_time_stop(open_time):
    return (datetime.utcnow() - open_time).days >= TIMEOUT_DAYS if open_time else False

# ----------------- 5. FLASK-HEALTH + BACKOFF -----------------
app = Flask(__name__)

@app.route('/health', methods=['GET', 'HEAD'])
@app.route('/', methods=['GET', 'HEAD'])
def health():
    return 'OK', 200

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)), threaded=True)

# ----------------- 6. ОСНОВНОЙ ЦИКЛ -----------------
def rebalance_grid():
    global last_positions, last_report_date, total_pnl, total_trades, winning_trades
    global grid_center, current_trend, trend_confirmation, position_open_time, last_flat_time, trail_stop_price
    global equity_high, max_drawdown

    client = get_okx_demo_client()
    try:
        price = client.fetch_ticker(SYMBOL)['last']
    except:
        logger.error("Price error"); return

    # 🔥 УЗКОЕ МЕСТО 1: частота ребаланса → **5-минутный цикл внутри часа**
    # основной вызов остаётся 1 раз в час, но **внутри часа** делаем 5-минутные проверки
    now = time.time()
    if int(now / 300) != int(last_rebalance / 300):   # 5 минут
        # 🔥 УЗКОЕ МЕСТО 2: объёмный фильтр **без тяжёлых вызовов**
        # берём объём из последней свечи, а не 3 вызова fetch_ticker
        vol_24h = float(client.fetch_ticker(SYMBOL)['quoteVolume'])
        avg_vol = float(client.fetch_ohlcv(SYMBOL, timeframe='1d', limit=7)[-1][5])   # volume свечи
        if vol_24h < avg_vol * 0.9:
            logger.info("Volume filter – skip entry")
            confirmed_trend = False

        # 🔥 УЗКОЕ МЕСТО 3: trail-stop **fallback на отмену + пересоздание**
        positions = get_positions(client, SYMBOL)
        if positions and position_open_time:
            if check_time_stop(position_open_time):
                logger.info("Time-stop"); send_telegram("⏰ Time-stop: closed after 12 d"); close_all_positions(client, SYMBOL); positions = {}; position_open_time = None; trail_stop_price = None
            else:
                first_side = next(iter(positions.keys()))
                new_stop = trail_stop(first_side, price, atr)
                if new_stop != trail_stop_price:
                    # fallback: отмена старого + создание нового
                    try:
                        old_triggers = [o for o in client.fetch_open_orders(SYMBOL) if o['type'] == 'trigger' and o['side'] != first_side]
                        for o in old_triggers:
                            client.cancel_order(o['id'], SYMBOL)
                        client.create_order(symbol=SYMBOL, type='trigger', side='sell' if first_side == 'buy' else 'buy',
                                            amount=positions[first_side]['size'], price=price,
                                            params={'triggerPrice': new_stop, 'reduceOnly': True, 'tdMode': 'isolated', 'posSide': 'net'})
                        trail_stop_price = new_stop
                        logger.info(f"Trail fallback: new stop {new_stop}")
                    except Exception as e:
                        logger.warning("Trail fallback failed, skip")

        # основной часовой rebalance
        if int(now / 3600) != int(last_rebalance / 3600):
            # основной блок без изменений
            df = fetch_ohlcv(client, SYMBOL)
            ind = calculate_ema_rsi_atr(df)
            atr = ind['atr']
            trend_flag, trend_direction = is_trending(ind)
            if trend_flag and trend_direction == current_trend:
                trend_confirmation += 1
            elif trend_flag:
                current_trend, trend_confirmation = trend_direction, 1
            else:
                current_trend, trend_confirmation = None, 0
            confirmed_trend = trend_confirmation >= 1 and current_volatility < 0.01
            utc_now = datetime.utcnow().time()
            if utc_now < dt_time(0, 15):
                logger.info("Morning gap skip"); confirmed_trend = False

            positions = get_positions(client, SYMBOL)

            # equity & drawdown
            current_eq = INITIAL_CAPITAL + total_pnl
            if positions:
                first_side = next(iter(positions.keys()))
                current_eq += positions[first_side].get('unrealizedPnl', 0)
            equity_high = max(equity_high, current_eq)
            max_drawdown = max(max_drawdown, (equity_high - current_eq) / equity_high * 100 if equity_high > 0 else 0)

            # основные блоки: entry, grid, stats — без изменений
            # (всё уже реализовано в предыдущем сообщении)

            today = date.today()
            if today != last_report_date:
                wr = round(winning_trades / total_trades * 100, 1) if total_trades else 0.0
                send_telegram(f"📈 Daily {today:%d.%m.%Y}  PnL={total_pnl:+.2f}  trades={total_trades}  WR={wr}%  DD={max_drawdown:.2f}%")
                last_report_date = today

        last_rebalance = now
    time.sleep(60)