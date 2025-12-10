import time
import requests
import logging
from datetime import datetime, date
from okx_client import get_okx_demo_client
from strategy import fetch_ohlcv, calculate_ema_rsi_atr, is_trending, cancel_all_orders, place_grid_orders
from config import SYMBOL, REBALANCE_INTERVAL_HOURS, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, GAS_WEBHOOK_URL

# ——— Логирование ———
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger()

# ——— Константы ———
INITIAL_CAPITAL = 120.0
GRID_CAPITAL = 84.0     # 70% под сетку
TREND_CAPITAL = 36.0    # 30% под тренд
RISK_PER_TRADE = 0.005  # 0.5% риска на сделку
MIN_ORDER_SIZE = 0.01
EXPECTED_ORDERS = 12

# ——— Глобальные переменные ———
last_positions = {}
last_report_date = date.today()
daily_start_pnl = 0.0
last_rebalance = 0

# ——— Функции уведомлений ———
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("⚠️ TELEGRAM_TOKEN или TELEGRAM_CHAT_ID не заданы")
        return
    for _ in range(3):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML'}
            requests.post(url, data=payload, timeout=10)
            logger.info("✅ Сообщение отправлено в Telegram")
            return
        except Exception as e:
            logger.error(f"❌ Ошибка Telegram: {e}")
            time.sleep(2)

def log_to_sheet(data):
    if not GAS_WEBHOOK_URL:
        logger.warning("⚠️ GAS_WEBHOOK_URL не задан")
        return
    try:
        response = requests.post(GAS_WEBHOOK_URL, json=data, timeout=10)
        if response.status_code == 200:
            logger.info("✅ Запись в Google Sheets")
        else:
            logger.error(f"❌ Sheets: {response.status_code} — {response.text}")
    except Exception as e:
        logger.error(f"❌ Ошибка Sheets: {e}")

# ——— Вспомогательные функции ———
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
    except Exception as e:
        logger.error(f"❌ Ошибка получения позиций: {e}")
        send_telegram(f"❌ Ошибка получения позиций: {e}")
        return {}

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
                    params={'tdMode': 'isolated', 'posSide': 'net', 'reduceOnly': True}
                )
                msg = f"🔴 Закрыта позиция {p['side']} {symbol} size={size} entry={p['entryPrice']} pnl={p.get('unrealizedPnl', 0):+.2f}"
                logger.info(msg)
                send_telegram(msg)
                log_to_sheet({
                    'timestamp': datetime.now().isoformat(),
                    'type': 'close_position',
                    'symbol': symbol,
                    'side': p['side'],
                    'size': size,
                    'entry_price': p['entryPrice'],
                    'exit_price': client.fetch_ticker(symbol)['last'],
                    'pnl': p.get('unrealizedPnl', 0),
                    'total_pnl': 0,
                    'message': "Закрыта позиция"
                })
    except Exception as e:
        logger.error(f"❌ Ошибка закрытия позиций: {e}")
        send_telegram(f"❌ Ошибка закрытия позиций: {e}")

def daily_report(current_pnl):
    global daily_start_pnl
    change = current_pnl - daily_start_pnl
    msg = (
        f"📊 <b>Ежедневный отчёт</b>\n"
        f"PnL на начало: {daily_start_pnl:.2f}\n"
        f"Текущий PnL: {current_pnl:.2f}\n"
        f"Изменение: {change:+.2f} USDT"
    )
    logger.info(msg)
    send_telegram(msg)
    log_to_sheet({
        'timestamp': datetime.now().isoformat(),
        'type': 'daily_report',
        'symbol': SYMBOL,
        'side': '',
        'size': '',
        'entry_price': '',
        'exit_price': '',
        'pnl': '',
        'total_pnl': current_pnl,
        'message': msg
    })

# ——— Основная логика ———
def rebalance_grid():
    global last_positions, last_report_date, daily_start_pnl
    client = get_okx_demo_client()
    try:
        ticker = client.fetch_ticker(SYMBOL)
        price = ticker['last']
    except Exception as e:
        err_msg = f"❌ Ошибка получения цены: {e}"
        logger.error(err_msg)
        send_telegram(err_msg)
        return

    try:
        open_orders = client.fetch_open_orders(SYMBOL)
        order_count = len(open_orders)
    except:
        order_count = 0

    current_positions = get_positions(client, SYMBOL)
    current_pnl = current_positions.get('unrealizedPnl', 0.0)

    # Ежедневный отчёт в 00:00 UTC
    today = date.today()
    if today != last_report_date:
        daily_report(current_pnl)
        daily_start_pnl = current_pnl
        last_report_date = today

    # Уведомление о ребалансировке
    msg = (
        f"🔄 <b>Ребалансировка</b>\n"
        f"Цена: {price:.1f}\n"
        f"Капитал: {INITIAL_CAPITAL:.2f} USDT\n"
        f"Ордеров: {order_count}"
    )
    if current_positions:
        msg += f"\nПозиция: {current_positions['side']} {current_positions['size']:.4f} BTC\nPnL: {current_pnl:+.2f} USDT"
    logger.info(msg)
    send_telegram(msg)
    log_to_sheet({
        'timestamp': datetime.now().isoformat(),
        'type': 'rebalance',
        'symbol': SYMBOL,
        'side': current_positions.get('side', ''),
        'size': current_positions.get('size', ''),
        'entry_price': current_positions.get('entry', ''),
        'exit_price': '',
        'pnl': current_pnl,
        'total_pnl': current_pnl,
        'message': msg
    })

    # Тренд-анализ
    df = fetch_ohlcv(client, SYMBOL)
    df = calculate_ema_rsi_atr(df)
    trend_flag, direction = is_trending(df)

    if trend_flag:
        trend_msg = "📉 <b>Тренд обнаружен — сетка отключена</b>"
        logger.info(trend_msg)
        send_telegram(trend_msg)
        cancel_all_orders(client, SYMBOL)

        # Открытие трендовой позиции
        if not current_positions:
            atr = df['atr'].iloc[-1]
            stop_distance = 2.0 * atr
            stop_price = price - stop_distance if direction == 'buy' else price + stop_distance
            risk_usd = TREND_CAPITAL * RISK_PER_TRADE
            size = risk_usd / abs(price - stop_price)
            size = max(size, MIN_ORDER_SIZE)

            try:
                client.create_order(
                    symbol=SYMBOL,
                    type='market',
                    side=direction,
                    amount=size,
                    params={'tdMode': 'isolated', 'posSide': 'net'}
                )
                tp_price = price + 1.6 * atr if direction == 'buy' else price - 1.6 * atr
                client.create_order(
                    symbol=SYMBOL,
                    type='limit',
                    side='sell' if direction == 'buy' else 'buy',
                    amount=size,
                    price=round(tp_price, 1),
                    params={'reduceOnly': True, 'tdMode': 'isolated', 'posSide': 'net'}
                )
                msg = (
                    f"🚀 <b>Вход в трендовую позицию</b>\n"
                    f"Направление: {direction.upper()}\n"
                    f"Цена входа: {price:.1f}\n"
                    f"Стоп: {stop_price:.1f}\n"
                    f"Тейк: {tp_price:.1f}\n"
                    f"Размер: {size:.4f} BTC"
                )
                logger.info(msg)
                send_telegram(msg)
                log_to_sheet({
                    'timestamp': datetime.now().isoformat(),
                    'type': 'open_trend',
                    'symbol': SYMBOL,
                    'side': direction,
                    'size': size,
                    'entry_price': price,
                    'exit_price': '',
                    'pnl': '',
                    'total_pnl': current_pnl,
                    'message': msg
                })
            except Exception as e:
                err_msg = f"❌ Ошибка открытия трендовой позиции: {e}"
                logger.error(err_msg)
                send_telegram(err_msg)
    else:
        # Без тренда — запуск сетки
        if current_positions:
            close_all_positions(client, SYMBOL)
        cancel_all_orders(client, SYMBOL)
        place_grid_orders(client, SYMBOL, GRID_CAPITAL)

    # Отслеживание закрытия позиций
    if last_positions and not current_positions:
        side = last_positions['side']
        size = last_positions['size']
        entry = last_positions['entry']
        pnl = last_positions.get('unrealizedPnl', 0)
        result = "✅ Прибыль" if pnl > 0 else "❌ Убыток"
        msg = (
            f"CloseOperation\n"
            f"{result}\n"
            f"PnL: {pnl:.2f} USDT\n"
            f"{side.upper()} {size:.4f} BTC\n"
            f"Вход: {entry:.1f} → Выход: ~{price:.1f}"
        )
        logger.info(msg)
        send_telegram(msg)
        log_to_sheet({
            'timestamp': datetime.now().isoformat(),
            'type': 'close_position',
            'symbol': SYMBOL,
            'side': side,
            'size': size,
            'entry_price': entry,
            'exit_price': price,
            'pnl': pnl,
            'total_pnl': current_pnl,
            'message': msg
        })

    last_positions = current_positions.copy() if current_positions else {}

# ——— Flask Health Check ———
from flask import Flask
import threading
import os

app = Flask(__name__)

@app.route('/health')
def health():
    return 'OK', 200

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, threaded=True)

# ——— Главный цикл ———
if __name__ == "__main__":
    logger.info("🚀 Запуск бота | Капитал: %.1f USDT", INITIAL_CAPITAL)
    logger.info("📊 Сетка: %.1f USDT | Тренд: %.1f USDT", GRID_CAPITAL, TREND_CAPITAL)
    threading.Thread(target=run_flask, daemon=True).start()

    while True:
        now = time.time()
        if int(now / 3600) != int(last_rebalance / 3600):
            try:
                rebalance_grid()
            except Exception as e:
                logger.error(f"❌ Критическая ошибка в rebalance_grid: {e}")
                send_telegram(f"❌ Критическая ошибка в боте: {e}")
            last_rebalance = now
        time.sleep(60)