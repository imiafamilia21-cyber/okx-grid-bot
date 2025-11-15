import time
import requests
import logging
from datetime import datetime, date
from okx_client import get_okx_demo_client
from strategy import fetch_ohlcv, calculate_ema_rsi_atr, is_trending, cancel_all_orders, place_grid_orders
from config import SYMBOL, REBALANCE_INTERVAL_HOURS, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

# --- Настройка логирования ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger()

# --- Глобальные переменные ---
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

# --- Google Apps Script Webhook URL ---
GAS_WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbzbtvlbwBIDEK8Rz1BtH-XWaIN3BZYabS93t_ERuXjBTVT82-SH7D1uLSe_FL0a1EoN/exec"

def send_telegram(text):
    """
    Отправляет сообщение в Telegram. Безопасно, с повторами.
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials отсутствуют. Уведомление не отправлено.")
        return
    for attempt in range(3):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML'}
            response = requests.post(url, data=payload, timeout=10)
            if response.status_code == 200:
                logger.info(f"✅ Telegram: {text[:50]}...")
                return
            else:
                logger.error(f"❌ Telegram API ошибка: {response.status_code}, {response.text}")
        except Exception as e:
            logger.error(f"❌ Ошибка отправки в Telegram (попытка {attempt + 1}): {e}")
        time.sleep(2)
    logger.error("❌ Не удалось отправить уведомление в Telegram после 3 попыток.")

def log_to_sheet(data):
    """
    Отправляет данные сделки на Webhook GAS.
    """
    try:
        response = requests.post(GAS_WEBHOOK_URL, json=data, timeout=10)
        if response.status_code == 200:
            resp_json = response.json()
            if resp_json.get("result") == "success":
                logger.info(f"📊 Записано в Google Sheets: {data.get('type', 'unknown')}")
            else:
                logger.error(f"❌ GAS вернул ошибку: {resp_json.get('message', 'unknown error')}")
        else:
            logger.error(f"❌ Ошибка отправки в GAS: {response.status_code}, {response.text}")
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к GAS: {e}")

def get_positions(client, symbol):
    """
    Получает текущие позиции. Оборачивает в try-except.
    """
    try:
        positions = client.fetch_positions([symbol])
        pos = {}
        for p in positions:
            if p.get('contracts', 0) > 0:
                pos['size'] = p['contracts']
                pos['entry'] = p['entryPrice']
                pos['side'] = p['side']
                pos['unrealizedPnl'] = p.get('unrealizedPnl', 0)
        logger.info(f"📊 Получены позиции: {pos}")
        return pos
    except Exception as e:
        logger.error(f"❌ Ошибка получения позиций: {e}")
        send_telegram(f"❌ Ошибка получения позиций: {e}")
        return {}

def close_all_positions(client, symbol):
    """
    Закрывает все открытые позиции. Оборачивает в try-except.
    """
    try:
        logger.info("⏳ Попытка закрытия всех позиций...")
        positions = client.fetch_positions([symbol])
        closed_count = 0
        for p in positions:
            if p.get('contracts', 0) > 0:
                side = 'buy' if p['side'] == 'short' else 'sell'
                size = p['contracts']
                try:
                    client.create_order(
                        symbol=symbol,
                        type='market',
                        side=side,
                        amount=size,
                        params={'tdMode': 'isolated', 'posSide': 'net', 'reduceOnly': True}
                    )
                    msg = f"CloseOperation\nЗакрытие позиции от сетки\n{p['side'].upper()} {size:.4f} BTC"
                    logger.info(msg)
                    send_telegram(msg)
                    
                    # Запись в Google Sheets
                    log_data = {
                        'type': 'close_position',
                        'symbol': SYMBOL,
                        'side': p['side'],
                        'size': size,
                        'entry_price': p['entry'],
                        'exit_price': client.fetch_ticker(SYMBOL)['last'],
                        'pnl': p['unrealizedPnl'],
                        'total_pnl': total_pnl + p['unrealizedPnl']
                    }
                    log_to_sheet(log_data)
                    
                    closed_count += 1
                except Exception as e:
                    logger.error(f"❌ Ошибка закрытия позиции {p['side']} {size}: {e}")
                    send_telegram(f"❌ Ошибка закрытия позиции: {e}")
        if closed_count == 0:
            logger.info("ℹ️ Нет открытых позиций для закрытия.")
        else:
            logger.info(f"✅ Закрыто {closed_count} позиций.")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка при закрытии позиций: {e}")
        send_telegram(f"❌ Критическая ошибка при закрытии позиций: {e}")

def daily_report(current_pnl):
    """
    Ежедневный отчёт. Вызывается в 09:00 UTC (12:00 MSK).
    """
    global total_pnl, winning_trades, total_trades, max_drawdown, equity_high
    try:
        equity = INITIAL_CAPITAL + total_pnl
        if equity > equity_high:
            equity_high = equity
        drawdown = (equity_high - equity) / equity_high * 100 if equity_high > 0 else 0
        if drawdown > max_drawdown:
            max_drawdown = drawdown
        win_rate = round(winning_trades / total_trades * 100, 1) if total_trades > 0 else 0.0
        
        report = (
            f"📊 ЕЖЕДНЕВНЫЙ ОТЧЁТ\n"
            f"Дата: {datetime.now().strftime('%d.%m.%Y')}\n"
            f"Общий PnL: {total_pnl:+.2f} USDT\n"
            f"Сделок: {total_trades}\n"
            f"Win Rate: {win_rate}%\n"
            f"Макс. просадка: {max_drawdown:.2f}%"
        )
        logger.info(report)
        send_telegram(report)
        
        # Запись в Google Sheets
        log_data = {
            'type': 'daily_report',
            'symbol': SYMBOL,
            'side': '',
            'size': '',
            'entry_price': '',
            'exit_price': '',
            'pnl': '',
            'total_pnl': total_pnl
        }
        log_to_sheet(log_data)
    except Exception as e:
        logger.error(f"❌ Ошибка при формировании ежедневного отчёта: {e}")
        send_telegram(f"❌ Ошибка ежедневного отчёта: {e}")

def open_trend_position(client, symbol, capital, direction, price, atr):
    """
    Открывает трендовую позицию. Оборачивает в try-except.
    """
    try:
        logger.info(f"⏳ Открытие тренд-позиции: {direction.upper()} по {price}...")
        risk_usd = capital * RISK_PER_TRADE
        stop_multiplier = 2.0
        stop_distance = atr * stop_multiplier
        
        if direction == 'buy':
            stop_price = price - stop_distance
        else:
            stop_price = price + stop_distance

        size = risk_usd / stop_distance
        min_size = 0.01
        if size < min_size:
            size = min_size

        order = client.create_order(
            symbol=symbol,
            type='market',
            side=direction,
            amount=size,
            params={'tdMode': 'isolated', 'posSide': 'net'}
        )
        logger.info(f"✅ Ордер на открытие позиции отправлен: {order['id']}")

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
        logger.info(f"✅ Стоп-лосс установлен: {stop_price:.1f}")

        msg = f"🚀 Тренд-фолловинг\n{direction.upper()} {size:.4f} BTC\nСтоп: {stop_price:.1f}"
        logger.info(msg)
        send_telegram(msg)
        
        # Запись в Google Sheets
        log_data = {
            'type': 'open_position',
            'symbol': SYMBOL,
            'side': direction,
            'size': size,
            'entry_price': price,
            'exit_price': '',
            'pnl': '',
            'total_pnl': total_pnl
        }
        log_to_sheet(log_data)
        
        return True
    except Exception as e:
        err_msg = f"⚠️ Ошибка тренд-позиции: {e}"
        logger.error(err_msg)
        send_telegram(err_msg)
        return False

def rebalance_grid():
    """
    Основная функция перебалансировки. Оборачиваем в try-except.
    """
    global last_positions, last_report_date, daily_start_pnl, total_pnl, total_trades, winning_trades
    
    try:
        logger.info("🔄 Начало перебалансировки...")
        client = get_okx_demo_client()
        
        # Получаем цену
        try:
            ticker = client.fetch_ticker(SYMBOL)
            price = ticker['last']
        except Exception as e:
            logger.error(f"❌ Ошибка получения цены: {e}")
            send_telegram(f"❌ Ошибка получения цены: {e}")
            return

        current_positions = get_positions(client, SYMBOL)
        current_pnl = current_positions.get('unrealizedPnl', 0.0)

        # --- Ежедневный отчёт в 09:00 UTC (12:00 MSK) ---
        from datetime import datetime
        current_time = datetime.utcnow()
        current_hour = current_time.hour
        today = current_time.date()
        
        if current_hour == 9 and today != last_report_date:
            daily_report(current_pnl)
            daily_start_pnl = current_pnl
            last_report_date = today
        # --- Конец ежедневного отчёта ---

        # Проверка на изменение позиции
        if current_positions != last_positions:
            if not last_positions and current_positions:
                side = current_positions['side']
                size = current_positions['size']
                entry = current_positions['entry']
                msg = f"🆕 Позиция открыта\n{side.upper()} {size:.4f} BTC\nЦена входа: {entry:.1f}"
                logger.info(msg)
                send_telegram(msg)
                
                # Запись в Google Sheets
                log_data = {
                    'type': 'open_position',
                    'symbol': SYMBOL,
                    'side': side,
                    'size': size,
                    'entry_price': entry,
                    'exit_price': '',
                    'pnl': '',
                    'total_pnl': total_pnl
                }
                log_to_sheet(log_data)
                
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
                
                # Запись в Google Sheets
                log_data = {
                    'type': 'close_position',
                    'symbol': SYMBOL,
                    'side': side,
                    'size': size,
                    'entry_price': entry,
                    'exit_price': price,
                    'pnl': pnl,
                    'total_pnl': total_pnl
                }
                log_to_sheet(log_data)
                
            last_positions = current_positions

        # Получаем ордера
        try:
            open_orders = client.fetch_open_orders(SYMBOL)
            order_count = len(open_orders)
        except:
            order_count = 0

        msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Перебалансировка\nЦена: {price:.1f}\nКапитал: {INITIAL_CAPITAL:.2f} USDT\nОрдеров: {order_count}"
        if current_positions:
            msg += f"\nПозиция: {current_positions['side']} {current_positions['size']:.4f} BTC\nPnL: {current_pnl:.2f} USDT"
        logger.info(msg)
        
        # Запись в Google Sheets
        log_data = {
            'type': 'rebalance',
            'symbol': SYMBOL,
            'side': current_positions.get('side', ''),
            'size': current_positions.get('size', ''),
            'entry_price': '',
            'exit_price': '',
            'pnl': current_pnl,
            'total_pnl': total_pnl
        }
        log_to_sheet(log_data)
        
        # Проверяем тренд
        df = fetch_ohlcv(client, SYMBOL)
        indicators = calculate_ema_rsi_atr(df)
        trend_flag, direction = is_trending(indicators)
        if trend_flag:
            logger.info(f"📈 Обнаружен тренд: {direction.upper()}")
            send_telegram(f"📈 Тренд обнаружен: {direction.upper()}")
            
            if current_positions:
                logger.info("⏳ Закрываем позиции от сетки перед трендом...")
                close_all_positions(client, SYMBOL)
            
            logger.info("⏳ Отменяем сетку...")
            cancel_all_orders(client, SYMBOL)
            
            logger.info("⏳ Открываем тренд-позицию...")
            open_trend_position(client, SYMBOL, INITIAL_CAPITAL, direction, indicators['price'], indicators['atr'])
            return
            
        logger.info("⏳ Отменяем старые ордера и размещаем новую сетку...")
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

    except Exception as e:
        logger.error(f"❌ Критическая ошибка в rebalance_grid: {e}")
        send_telegram(f"❌ Критическая ошибка в rebalance_grid: {e}")

# Flask health-check сервер
from flask import Flask
import threading
import os

app = Flask(__name__)

@app.route('/health')
def health():
    logger.info("✅ Запрос /health получен. Бот жив.")
    return 'OK'

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    logger.info(f"🚀 Flask запущен на порту {port}")
    app.run(host='0.0.0.0', port=port)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    
    logger.info("✅ Бот запущен. Ожидание цикла перебалансировки...")
    send_telegram("✅ Бот запущен и работает.")
    
    while True:
        now = time.time()
        if int(now / 3600) != int(last_rebalance / 3600):
            rebalance_grid()
            last_rebalance = now
        time.sleep(60)