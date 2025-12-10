import time
import requests
import logging
import threading
import os
from datetime import datetime, date
from flask import Flask
from okx_client import get_okx_demo_client
from strategy import fetch_ohlcv, calculate_ema_rsi_atr, is_trending, cancel_all_orders, place_grid_orders

# ——— Настройка логирования ———
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger()

# ——— Конфигурация ———
SYMBOL = "BTC-USDT-SWAP"
INITIAL_CAPITAL = 120.0
GRID_CAPITAL = 84.0     # 70% на сетку
TREND_CAPITAL = 36.0    # 30% на тренд
RISK_PER_TRADE = 0.005
EXPECTED_ORDERS = 12

# Из config или .env
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ——— Глобальные переменные ———
last_positions = {}
last_report_date = date.today()
daily_start_pnl = 0.0
last_rebalance = 0

# ——— Уведомления в Telegram ———
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("TELEGRAM_TOKEN или TELEGRAM_CHAT_ID не заданы")
        return
    for _ in range(3):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {
                'chat_id': TELEGRAM_CHAT_ID,
                'text': text,
                'parse_mode': 'HTML'
            }
            requests.post(url, data=payload, timeout=10)
            logger.info("✅ Отправлено в Telegram")
            return
        except Exception as e:
            logger.error(f"❌ Ошибка Telegram: {e}")
            time.sleep(2)

# ——— Получение текущих позиций ———
def get_positions(client, symbol):
    try:
        positions = client.fetch_positions([symbol])
        for p in positions:
            if p.get('contracts', 0) > 0:
                return {
                    'size': p['contracts'],
                    'entry': p['entryPrice'],
                    'side': p['side'],
                    'unrealizedPnl': p.get('unrealizedPnl', 0)
                }
    except Exception as e:
        logger.error(f"❌ Ошибка получения позиций: {e}")
        send_telegram(f"❌ Ошибка получения позиций: {e}")
    return {}

# ——— Закрытие всех позиций ———
def close_all_positions(client, symbol):
    try:
        positions = client.fetch_positions([symbol])
        for p in positions:
            if p.get('contracts', 0) > 0:
                side = 'buy' if p['side'] == 'short' else 'sell'
                size = p['contracts']
                client.create_order(
                    symbol=symbol,
                    type='market',
                    side=side,
                    amount=size,
                    params={'reduceOnly': True, 'tdMode': 'isolated', 'posSide': 'net'}
                )
                msg = (
                    f"🔴 Закрыта позиция\n"
                    f"{symbol} {p['side'].upper()}\n"
                    f"Размер: {size:.4f} BTC\n"
                    f"Вход: {p['entryPrice']:.1f}\n"
                    f"PnL: {p.get('unrealizedPnl', 0):+.2f} USDT"
                )
                logger.info(msg)
                send_telegram(msg)
    except Exception as e:
        logger.error(f"❌ Ошибка закрытия позиций: {e}")
        send_telegram(f"❌ Ошибка закрытия позиций: {e}")

# ——— Ежедневный отчёт ———
def daily_report(current_pnl):
    global daily_start_pnl
    change = current_pnl - daily_start_pnl
    msg = (
        f"📊 Отчёт за день ({datetime.now().strftime('%Y-%m-%d %H:%M')})\n"
        f"PnL на начало: {daily_start_pnl:.2f} USDT\n"
        f"Текущий PnL: {current_pnl:.2f} USDT\n"
        f"Изменение: {change:+.2f} USDT"
    )
    logger.info(msg)
    send_telegram(msg)

# ——— Основная логика перебалансировки ———
def rebalance_grid():
    global last_positions, last_report_date, daily_start_pnl
    client = get_okx_demo_client()

    # Получение цены
    try:
        ticker = client.fetch_ticker(SYMBOL)
        price = ticker['last']
    except Exception as e:
        err_msg = f"❌ Ошибка получения цены: {e}"
        logger.error(err_msg)
        send_telegram(err_msg)
        return

    # Текущие позиции и PnL
    current_positions = get_positions(client, SYMBOL)
    current_pnl = current_positions.get('unrealizedPnl', 0.0)

    # Ежедневный отчёт
    today = date.today()
    if today != last_report_date:
        daily_report(current_pnl)
        daily_start_pnl = current_pnl
        last_report_date = today

    # Анализ тренда
    df = fetch_ohlcv(client, SYMBOL)
    indicators = calculate_ema_rsi_atr(df)
    trend_flag, direction = is_trending(indicators)

    if trend_flag:
        trend_msg = f"📉 Тренд обнаружен — сетка отключена ({datetime.now().strftime('%Y-%m-%d %H:%M')})"
        logger.info(trend_msg)
        send_telegram(trend_msg)
        cancel_all_orders(client, SYMBOL)

        # Открытие трендовой позиции (если ещё не открыта)
        if not current_positions:
            try:
                size = TREN_CAPITAL / price * 0.3  # ~30% от тренд-капитала
                size = max(size, 0.001)
                client.create_order(
                    symbol=SYMBOL,
                    type='market',
                    side=direction,
                    amount=size,
                    params={'tdMode': 'isolated', 'posSide': 'net'}
                )
                msg = (
                    f"🆕 Позиция открыта ({datetime.now().strftime('%Y-%m-%d %H:%M')})\n"
                    f"{direction.upper()} {size:.4f} BTC\n"
                    f"Цена входа: {price:.1f}"
                )
                logger.info(msg)
                send_telegram(msg)
                current_positions = get_positions(client, SYMBOL)
            except Exception as e:
                send_telegram(f"❌ Ошибка открытия трендовой позиции: {e}")

    else:
        # Режим сетки
        if current_positions:
            close_all_positions(client, SYMBOL)
            current_positions = {}

        cancel_all_orders(client, SYMBOL)
        place_grid_orders(client, SYMBOL, GRID_CAPITAL)

    # Лог перебалансировки
    try:
        open_orders = client.fetch_open_orders(SYMBOL)
        order_count = len(open_orders)
    except:
        order_count = 0

    msg = (
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Перебалансировка\n"
        f"Цена: {price:.1f}\n"
        f"Капитал: {INITIAL_CAPITAL:.2f} USDT\n"
        f"Ордеров: {order_count}"
    )
    if current_positions:
        msg += (
            f"\nПозиция: {current_positions['side']} {current_positions['size']:.4f} BTC\n"
            f"PnL: {current_positions['unrealizedPnl']:.2f} USDT"
        )
    logger.info(msg)
    send_telegram(msg)

    # Лог закрытия сделки
    global last_positions
    if last_positions and not current_positions:
        side = last_positions['side']
        size = last_positions['size']
        entry = last_positions['entry']
        pnl = last_positions.get('unrealizedPnl', 0)
        result = "✅ Прибыль" if pnl > 0 else "❌ Убыток"
        msg = (
            f"CloseOperation ({datetime.now().strftime('%Y-%m-%d %H:%M')})\n"
            f"{result}\n"
            f"PnL: {pnl:.2f} USDT\n"
            f"{side.upper()} {size:.4f} BTC\n"
            f"Вход: {entry:.1f} → Выход: ~{price:.1f}"
        )
        logger.info(msg)
        send_telegram(msg)

    last_positions = current_positions.copy() if current_positions else {}

# ——— Flask health-check ———
app = Flask(__name__)

@app.route('/health')
def health():
    return 'OK', 200

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, threaded=True)

# ——— Запуск ———
if __name__ == "__main__":
    logger.info(f"🚀 Запуск бота | Капитал: {INITIAL_CAPITAL} USDT")
    threading.Thread(target=run_flask, daemon=True).start()

    while True:
        now = time.time()
        if int(now / 3600) != int(last_rebalance / 3600):
            rebalance_grid()
            last_rebalance = now
        time.sleep(60)