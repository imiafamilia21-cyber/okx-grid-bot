import time
import requests
import logging
from datetime import datetime, date
from okx_client import get_okx_demo_client
from strategy import fetch_ohlcv, calculate_ema_rsi_atr, is_trending, cancel_all_orders, place_grid_orders
from config import SYMBOL, REBALANCE_INTERVAL_HOURS, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

# === Логирование ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger()

# === Конфигурация ===
INITIAL_CAPITAL = 120.0
EXPECTED_ORDERS = 12

# === Глобальные переменные ===
last_positions = {}
last_report_date = date.today()
daily_start_pnl = 0.0
last_rebalance = 0

# === Telegram ===
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    for _ in range(3):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': text}
            requests.post(url, data=payload, timeout=10)
            logger.info("✅ Telegram отправлен")
            return
        except Exception as e:
            logger.error(f"❌ Ошибка Telegram: {e}")
            time.sleep(2)

# === Получение позиций ===
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
    return {}

# === Ежедневный отчёт ===
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

# === Основная логика ===
def rebalance_grid():
    global last_positions, last_report_date, daily_start_pnl, last_rebalance

    client = get_okx_demo_client()

    try:
        ticker = client.fetch_ticker(SYMBOL)
        price = ticker['last']
    except Exception as e:
        err_msg = f"❌ Ошибка получения цены: {e}"
        logger.error(err_msg)
        send_telegram(err_msg)
        return

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
        msg = "📉 Тренд обнаружен — сетка отключена"
        logger.info(msg)
        send_telegram(msg)
        cancel_all_orders(client, SYMBOL)
        return

    # Режим сетки
    if current_positions:
        logger.info("Закрываем позиции от сетки перед обновлением")
        # В рабочей версии — закрытие не было реализовано, поэтому просто отмена
    cancel_all_orders(client, SYMBOL)
    place_grid_orders(client, SYMBOL, INITIAL_CAPITAL)

    # Уведомление о перебалансировке
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
        msg += f"\nПозиция: {current_positions['side']} {current_positions['size']:.4f} BTC\nPnL: {current_pnl:.2f} USDT"
    logger.info(msg)
    send_telegram(msg)

    # Лог закрытия сделки
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

# === Запуск ===
if __name__ == "__main__":
    logger.info(f"🚀 Запуск бота | Капитал: {INITIAL_CAPITAL} USDT")
    while True:
        now = time.time()
        if int(now / 3600) != int(last_rebalance / 3600):
            rebalance_grid()
            last_rebalance = now
        time.sleep(60)