import time
import requests
import logging
from datetime import datetime, date
from okx_client import get_okx_demo_client
from strategy import fetch_ohlcv, calculate_ema_rsi_atr, is_trending, cancel_all_orders, place_grid_orders
from config import SYMBOL, REBALANCE_INTERVAL_HOURS, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', handlers=[logging.StreamHandler()])
logger = logging.getLogger()

INITIAL_CAPITAL = 120.0
GRID_CAPITAL = 84.0
TREND_CAPITAL = 36.0
RISK_PER_TRADE = 0.008
MIN_ORDER_SIZE = 0.01

last_positions = {}
last_report_date = date.today()
total_pnl = 0.0
total_trades = 0
winning_trades = 0
max_drawdown = 0.0
equity_high = INITIAL_CAPITAL
grid_center = None
current_trend = None
trend_confirmation = 0

def stop_voron(entry: float, atr: float, side: str, current_price: float = None, 
               k: float = 2.0, min_dist_pct: float = 0.005, max_dist_pct: float = 0.04, 
               spread_pct: float = 0.001) -> float:
    if entry <= 0 or atr < 0:
        raise ValueError("Некорректные входные данные")
    min_atr = entry * 0.001
    atr = max(atr, min_atr)
    if side == "long":
        raw_stop = entry - k * atr
        min_stop = entry * (1 - min_dist_pct)
        stop = min(raw_stop, min_stop)
    else:
        raw_stop = entry + k * atr
        min_stop = entry * (1 + min_dist_pct)
        stop = max(raw_stop, min_stop)
    if current_price is not None and current_price > 0:
        if side == "long":
            trail_stop = current_price - k * atr
            stop = max(stop, trail_stop)
        else:
            trail_stop = current_price + k * atr
            stop = min(stop, trail_stop)
    if spread_pct > 0:
        price_for_spread = current_price if current_price is not None else entry
        if price_for_spread and price_for_spread > 0:
            spread_buffer = price_for_spread * spread_pct
            if side == "long":
                stop = stop + spread_buffer
            else:
                stop = stop - spread_buffer
    current_distance_pct = abs(stop - entry) / entry if entry != 0 else 0
    if current_distance_pct > max_dist_pct:
        if side == "long":
            stop = entry * (1 - max_dist_pct)
        else:
            stop = entry * (1 + max_dist_pct)
    return round(stop, 6)

def should_exit_voron(current_price: float, stop_level: float, side: str, 
                      spread_pct: float = 0.0, bar_low: float = None, bar_high: float = None) -> bool:
    if side == "long" and bar_low is not None:
        price_for_exit = bar_low
    elif side == "short" and bar_high is not None:
        price_for_exit = bar_high
    else:
        price_for_exit = current_price
    if spread_pct > 0 and price_for_exit:
        spread_buffer = price_for_exit * spread_pct
        if side == "long":
            return price_for_exit <= (stop_level + spread_buffer)
        else:
            return price_for_exit >= (stop_level - spread_buffer)
    else:
        if side == "long":
            return price_for_exit <= stop_level
        else:
            return price_for_exit >= stop_level

from tenacity import retry, stop_after_attempt, wait_exponential
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def safe_fetch_ohlcv(client, symbol, timeframe, limit):
    return client.fetch_ohlcv(symbol, timeframe, limit)

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def safe_create_order(client, **params):
    return client.create_order(**params)

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    for _ in range(3):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': text}, timeout=10)
            return
        except:
            time.sleep(2)

def get_positions(client, symbol):
    try:
        positions = client.fetch_positions([symbol])
        pos = {}
        for p in positions:
            if p.get('contracts', 0) > 0:
                pos['size'] = p['contracts']
                pos['entry'] = p['entryPrice']
                pos['side'] = p['side']
                pos['unrealizedPnl'] = p.get('unrealizedPnl', 0)
        return pos
    except:
        return {}

def close_all_positions(client, symbol):
    try:
        positions = client.fetch_positions([symbol])
        for p in positions:
            if p.get('contracts', 0) > 0:
                side = 'buy' if p['side'] == 'short' else 'sell'
                size = p['contracts']
                safe_create_order(
                    client,
                    symbol=symbol,
                    type='market',
                    side=side,
                    amount=size,
                    params={'tdMode': 'isolated', 'posSide': 'net', 'reduceOnly': True}
                )
    except:
        pass

def compute_position_size(entry: float, stop: float, capital: float, max_exposure_pct: float = 0.3) -> float:
    risk_usd = capital * RISK_PER_TRADE
    r_dist = abs(entry - stop)
    if r_dist <= 0:
        return MIN_ORDER_SIZE
    size = risk_usd / r_dist
    max_size = (capital * max_exposure_pct) / entry if entry > 0 else 0.0
    size = min(size, max_size)
    return max(size, MIN_ORDER_SIZE)

def place_take_profit(client, symbol, side, entry, stop, size):
    try:
        risk_distance = abs(entry - stop)
        tp_distance = risk_distance * 2.0
        tp_price = entry + tp_distance if side == "buy" else entry - tp_distance
        tp_price = round(tp_price, 1)
        safe_create_order(
            client,
            symbol=symbol,
            type='limit',
            side='sell' if side == 'buy' else 'buy',
            amount=size,
            price=tp_price,
            params={'reduceOnly': True, 'tdMode': 'isolated', 'posSide': 'net'}
        )
        logger.info(f"✅ Take-Profit: {tp_price:.1f}")
    except Exception as e:
        logger.error(f"❌ Ошибка Take-Profit: {e}")

def should_rebalance_grid(current_price: float, grid_center: float, grid_range_pct: float) -> bool:
    if grid_center is None:
        return True
    upper = grid_center * (1 + grid_range_pct / 100)
    lower = grid_center * (1 - grid_range_pct / 100)
    return not (lower <= current_price <= upper)

def rebalance_grid():
    global last_positions, last_report_date, total_pnl, total_trades, winning_trades, grid_center, current_trend, trend_confirmation
    
    client = get_okx_demo_client()
    try:
        ticker = client.fetch_ticker(SYMBOL)
        price = ticker['last']
    except Exception as e:
        logger.error(f"Ошибка получения цены: {e}")
        return

    try:
        m1_data = safe_fetch_ohlcv(client, SYMBOL, '1m', limit=5)
        bar_low = min(candle[3] for candle in m1_data)
        bar_high = max(candle[2] for candle in m1_data)
        current_volatility = (bar_high - bar_low) / price
    except:
        bar_low = bar_high = price
        current_volatility = 0

    df = fetch_ohlcv(client, SYMBOL)
    indicators = calculate_ema_rsi_atr(df)
    trend_flag, trend_direction = is_trending(indicators)
    
    if trend_flag and trend_direction == current_trend:
        trend_confirmation += 1
    elif trend_flag:
        current_trend = trend_direction
        trend_confirmation = 1
    else:
        current_trend = None
        trend_confirmation = 0

    confirmed_trend = trend_confirmation >= 2 and current_trend is not None

    current_positions = get_positions(client, SYMBOL)
    
    if current_positions:
        position_side = current_positions['side']
        position_entry = current_positions['entry']
        stop_level = stop_voron(position_entry, indicators['atr'], position_side)
        if should_exit_voron(price, stop_level, position_side, 0.001, bar_low, bar_high):
            logger.info("🔴 Сработал Stop-Loss по защите от гэпа")
            close_all_positions(client, SYMBOL)
            current_positions = get_positions(client, SYMBOL)

    if confirmed_trend:
        volatility_threshold = 0.03
        if current_volatility < volatility_threshold:
            if not current_positions:
                stop_price = stop_voron(price, indicators['atr'], current_trend, k=2.0)
                size = compute_position_size(price, stop_price, TREND_CAPITAL)
                if size > 0:
                    try:
                        safe_create_order(
                            client,
                            symbol=SYMBOL,
                            type='market',
                            side=current_trend,
                            amount=size,
                            params={'tdMode': 'isolated', 'posSide': 'net'}
                        )
                        safe_create_order(
                            client,
                            symbol=SYMBOL,
                            type='trigger',
                            side='sell' if current_trend == 'buy' else 'buy',
                            amount=size,
                            price=price,
                            params={'triggerPrice': stop_price, 'reduceOnly': True, 'tdMode': 'isolated', 'posSide': 'net'}
                        )
                        place_take_profit(client, SYMBOL, current_trend, price, stop_price, size)
                        send_telegram(f"🚀 Тренд {current_trend.upper()}: Stop={stop_price:.1f}")
                    except Exception as e:
                        logger.error(f"Ошибка открытия трендовой позиции: {e}")
            
            # 1. В бычьем тренде
            if current_trend == "buy":
                cancel_all_orders(client, SYMBOL)
                place_grid_orders(client, SYMBOL, GRID_CAPITAL, upper_pct=15.0, lower_pct=3.0)
            # 2. В медвежьем тренде
            else:
                cancel_all_orders(client, SYMBOL)
                place_grid_orders(client, SYMBOL, GRID_CAPITAL, upper_pct=3.0, lower_pct=15.0)
            grid_center = price
        else:
            if current_positions:
                close_all_positions(client, SYMBOL)
            # 3. В боковике с высокой волатильностью
            cancel_all_orders(client, SYMBOL)
            current_atr_pct = indicators['atr'] / indicators['price'] * 100
            dynamic_range = max(12.0, min(20.0, current_atr_pct * 1.5))
            place_grid_orders(client, SYMBOL, INITIAL_CAPITAL, grid_range_pct=dynamic_range)
            grid_center = price
    else:
        if current_positions:
            close_all_positions(client, SYMBOL)
        # 4. В боковике без тренда
        cancel_all_orders(client, SYMBOL)
        current_atr_pct = indicators['atr'] / indicators['price'] * 100
        dynamic_range = max(12.0, min(20.0, current_atr_pct * 1.5))
        place_grid_orders(client, SYMBOL, INITIAL_CAPITAL, grid_range_pct=dynamic_range)
        grid_center = price

    if current_positions != last_positions:
        if last_positions and not current_positions:
            pnl = last_positions.get('unrealizedPnl', 0)
            total_pnl += pnl
            total_trades += 1
            if pnl > 0:
                winning_trades += 1
                
            equity = INITIAL_CAPITAL + total_pnl
            if equity > equity_high:
                equity_high = equity
            drawdown = (equity_high - equity) / equity_high * 100 if equity_high > 0 else 0
            if drawdown > max_drawdown:
                max_drawdown = drawdown
                
        last_positions = current_positions.copy() if current_positions else {}

    today = date.today()
    if today != last_report_date:
        win_rate = round(winning_trades / total_trades * 100, 1) if total_trades > 0 else 0.0
        report = (f"📊 ЕЖЕДНЕВНЫЙ ОТЧЁТ\n"
                 f"Дата: {datetime.now().strftime('%d.%m.%Y')}\n"
                 f"Общий PnL: {total_pnl:+.2f} USDT\n"
                 f"Сделок: {total_trades}\n"
                 f"Win Rate: {win_rate}%\n"
                 f"Макс. просадка: {max_drawdown:.2f}%")
        logger.info(report)
        send_telegram(report)
        last_report_date = today

from flask import Flask
import threading
import os

app = Flask(__name__)

@app.route('/health')
def health():
    return 'OK'

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

if __name__ == "__main__":
    logger.info(f"🚀 Запуск бота | Капитал: {INITIAL_CAPITAL} USDT")
    logger.info(f"📊 Сетка: {GRID_CAPITAL} USDT | Тренд: {TREND_CAPITAL} USDT")
    
    threading.Thread(target=run_flask, daemon=True).start()
    last_rebalance = 0
    while True:
        now = time.time()
        if int(now / 3600) != int(last_rebalance / 3600):
            rebalance_grid()
            last_rebalance = now
        time.sleep(60)