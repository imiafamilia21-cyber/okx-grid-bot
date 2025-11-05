import time
import requests
import logging
from datetime import datetime, date
from okx_client import get_okx_demo_client
from strategy import fetch_ohlcv, calculate_ema_rsi_atr, is_trending, cancel_all_orders, place_grid_orders
from config import SYMBOL, REBALANCE_INTERVAL_HOURS, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger()

INITIAL_CAPITAL = 120.0
RISK_PER_TRADE = 0.01
EXPECTED_ORDERS = 12
last_positions = {}
last_report_date = date.today()
daily_start_pnl = 0.0
last_rebalance = 0
total_pnl = 0.0
total_trades = 0
winning_trades = 0
max_drawdown = 0.0
equity_high = INITIAL_CAPITAL

def send_telegram(text):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        for _ in range(3):
            try:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML'}
                requests.post(url, data=payload, timeout=10)
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

def calculate_position_size(risk_usd, entry_price, stop_price):
    distance = abs(entry_price - stop_price)
    if distance == 0:
        return 0.01
    size = risk_usd / distance
    min_size = 0.01
    return max(size, min_size)

def daily_report(current_pnl):
    global total_pnl, winning_trades, total_trades, max_drawdown, equity_high
    equity = INITIAL_CAPITAL + total_pnl
    if equity > equity_high:
        equity_high = equity
    drawdown = (equity_high - equity) / equity_high * 100 if equity_high > 0 else 0
    if drawdown > max_drawdown:
        max_drawdown = drawdown
    win_rate = round(winning_trades / total_trades * 100, 1) if total_trades > 0 else 0.0
    report = (
        f"📊 ЕЖЕДНЕВНЫЙ ОТЧЁТ\n"
        f"Общий PnL: {total_pnl:+.2f} USDT\n"
        f"Сделок: {total_trades}\n"
        f"Win Rate: {win_rate}%\n"
        f"Макс. просадка: {max_drawdown:.2f}%"
    )
    logger.info(report)
    send_telegram(report)

def open_trend_position(client, symbol, capital, direction, price, atr):
    risk_usd = capital * RISK_PER_TRADE
    stop_multiplier = 2.0
    stop_distance = atr * stop_multiplier
    
    if direction == 'buy':
        stop_price = price - stop_distance
    else:
        stop_price = price + stop_distance

    size = calculate_position_size(risk_usd, price, stop_price)
    if size <= 0.01:
        size = 0.01

    try:
        order = client.create_order(
            symbol=symbol,
            type='market',
            side=direction,
            amount=size,
            params={'tdMode': 'isolated', 'posSide': 'net'}
        )
        client.create_order(
            symbol=symbol,
            type='trigger',
            side='sell' if direction == 'buy' else 'buy',
            amount=size,
            price=price,
            params={
                'triggerPrice': stop_price,
                'reduceOnly': True,
                'tdMode': 'isolated',
                'posSide': 'net'
            }
        )
        msg = f"🚀 Тренд-фолловинг\n{direction.upper()} {size:.4f} BTC\nСтоп: {stop_price:.1f}"
        logger.info(msg)
        send_telegram(msg)
        return True
    except Exception as e:
        err_msg = f"⚠️ Ошибка тренд-позиции: {e}"
        logger.error(err_msg)
        send_telegram(err_msg)
        return False

def rebalance_grid():
    global last_positions, last_report_date, daily_start_pnl, total_pnl, total_trades, winning_trades
    
    client = get_okx_demo_client()
    
    try:
        ticker = client.fetch_ticker(SYMBOL)
        price = ticker['last']
    except Exception as e:
        err_msg = f"❌ Ошибка цены: {e}"
        logger.error(err_msg)
        send_telegram(err_msg)
        return

    current_positions = get_positions(client, SYMBOL)
    current_pnl = current_positions.get('unrealizedPnl', 0.0)

    today = date.today()
    if today != last_report_date:
        daily_report(current_pnl)
        daily_start_pnl = current_pnl
        last_report_date = today

    if current_positions != last_positions:
        if not last_positions and current_positions:
            side = current_positions['side']
            size = current_positions['size']
            entry = current_positions['entry']
            msg = f"🆕 Позиция открыта\n{side.upper()} {size:.4f} BTC\nЦена входа: {entry:.1f}"
            logger.info(msg)
            send_telegram(msg)
        elif last_positions and not current_positions:
            side = last_positions['side']
            size = last_positions['size']
            entry = last_positions['entry']
            pnl = last_positions.get('unrealizedPnl', 0)
            total_pnl += pnl
            total_trades += 1
            if pnl > 0:
                winning_trades += 1
            result = "✅ Прибыль" if pnl > 0 else "❌ Убыток"
            msg = f"CloseOperation\n{result}\nPnL: {pnl:.2f} USDT\nИтого: {total_pnl:+.2f}\n{side.upper()} {size:.4f} BTC\nВход: {entry:.1f} → Выход: ~{price:.1f}"
            logger.info(msg)
            send_telegram(msg)
        last_positions = current_positions

    try:
        open_orders = client.fetch_open_orders(SYMBOL)
        order_count = len(open_orders)
    except:
        order_count = 0

    msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Перебалансировка\nЦена: {price:.1f}\nКапитал: {INITIAL_CAPITAL:.2f} USDT\nОрдеров: {order_count}"
    if current_positions:
        msg += f"\nПозиция: {current_positions['side']} {current_positions['size']:.4f} BTC\nPnL: {current_pnl:.2f} USDT"
    logger.info(msg)
    send_telegram(msg)
        
    df = fetch_ohlcv(client, SYMBOL)
    indicators = calculate_ema_rsi_atr(df)
    trend_flag, direction = is_trending(indicators)
    
    if trend_flag and not current_positions:
        cancel_all_orders(client, SYMBOL)
        open_trend_position(client, SYMBOL, INITIAL_CAPITAL, direction, indicators['price'], indicators['atr'])
        return
    elif not trend_flag:
        cancel_all_orders(client, SYMBOL)
        place_grid_orders(client, SYMBOL, INITIAL_CAPITAL)
    
    time.sleep(3)
    
    try:
        open_orders = client.fetch_open_orders(SYMBOL)
        new_count = len(open_orders)
    except:
        new_count = 0
        
    if new_count < EXPECTED_ORDERS:
        alert_msg = f"⚠️ Только {new_count} из {EXPECTED_ORDERS} ордеров!"
        logger.warning(alert_msg)
        send_telegram(alert_msg)

# Flask health-check
from flask import Flask
import threading

app = Flask(__name__)

@app.route('/health')
def health():
    return 'OK'

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

import os

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    while True:
        now = time.time()
        if int(now / 3600) != int(last_rebalance / 3600):
            rebalance_grid()
            last_rebalance = now
        time.sleep(60)