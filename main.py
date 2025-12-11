import time
import requests
import logging
import threading
import os
from datetime import datetime, date, timezone
from flask import Flask, send_file, abort

# === ВСТРОЕННЫЙ StopVoronPro v5 ===
class StopVoronPro:
    def __init__(self, base_atr_mult=2.0, min_risk_pct=0.005, max_risk_pct=0.04):
        self.base_atr_mult = base_atr_mult
        self.min_risk_pct = min_risk_pct
        self.max_risk_pct = max_risk_pct

    def calculate_stop(self, entry, atr, side, current_price, volatility_ratio, market_regime="normal"):
        risk_pct = 0.010 if market_regime == "trending" else 0.008
        stop_distance = risk_pct * current_price
        atr_distance = self.base_atr_mult * atr
        final_distance = max(stop_distance, atr_distance, current_price * self.min_risk_pct)
        final_distance = min(final_distance, current_price * self.max_risk_pct)
        return entry - final_distance if side == "buy" else entry + final_distance

    def check_exit(self, current_price, stop_level, side, bar_low, bar_high):
        if side == "buy":
            return bar_low <= stop_level
        else:
            return bar_high >= stop_level

# === Логирование ===
LOG_FILE = "/tmp/app.log"
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
console_handler = logging.StreamHandler()
file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
console_handler.setFormatter(formatter)
file_handler.setFormatter(formatter)

logging.basicConfig(level=logging.INFO, handlers=[console_handler, file_handler])
logger = logging.getLogger()

# === Конфигурация ===
SYMBOL = "ETH-USDT-SWAP"
INITIAL_CAPITAL = 120.0
GRID_CAPITAL = 84.0
TREND_CAPITAL = 36.0
RISK_PER_TRADE = 0.005

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# === Глобальные переменные ===
last_positions = {}
last_report_date = date.today()
total_pnl = 0.0
total_trades = 0
winning_trades = 0
equity_high = INITIAL_CAPITAL
max_drawdown = 0.0

# === Telegram ===
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    for _ in range(3):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML'}
            requests.post(url, data=payload, timeout=10)
            logger.info("✅ Telegram отправлен")
            return
        except Exception as e:
            logger.error(f"❌ Ошибка Telegram: {e}")
            time.sleep(2)

# === Позиции ===
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

def close_all_positions(client, symbol):
    try:
        positions = client.fetch_positions([symbol])
        if not any(p.get('contracts', 0) > 0 for p in positions):
            logger.info("Нет позиций для закрытия")
            return

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
                    f"🔴 Закрыта позиция ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')})\n"
                    f"{p['side'].upper()} {size:.4f} ETH\n"
                    f"Вход: {p['entryPrice']:.1f} → PnL: {p.get('unrealizedPnl', 0):+.2f} USDT"
                )
                logger.info(msg)
                send_telegram(msg)
    except Exception as e:
        logger.error(f"❌ Ошибка закрытия позиций: {e}")
        send_telegram(f"❌ Ошибка закрытия позиций: {e}")

# === Flask: health + logs ===
app = Flask(__name__)

@app.route('/health')
def health():
    return 'OK'

@app.route('/logs')
def get_logs():
    if os.path.exists(LOGING_FILE):
        return send_file(LOG_FILE, mimetype='text/plain')
    else:
        abort(404, "Log file not found")

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, threaded=True)

# === Фильтр макроновостей ===
def is_high_impact_news_today():
    today_str = datetime.now(timezone.utc).strftime('%m-%d')  # ← ИСПРАВЛЕНО: utc вместо UTC
    high_risk_dates = ['01-31', '04-30', '07-31', '10-31']
    return today_str in high_risk_dates

# === Основная логика ===
def rebalance_grid():
    global last_positions, last_report_date, total_pnl, total_trades, winning_trades, equity_high, max_drawdown

    from okx_client import get_okx_demo_client
    from strategy import fetch_ohlcv, calculate_ema_rsi_atr, is_trending, cancel_all_orders, place_grid_orders

    client = get_okx_demo_client()

    if is_high_impact_news_today():
        cancel_all_orders(client, SYMBOL)
        close_all_positions(client, SYMBOL)
        send_telegram("🚫 Высокая волатильность: торговля отключена")
        return

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

    df = fetch_ohlcv(client, SYMBOL)
    indicators = calculate_ema_rsi_atr(df)
    trend_flag, direction = is_trending(indicators)

    try:
        m1_data = client.fetch_ohlcv(SYMBOL, '1m', limit=5)
        bar_low = min(candle[3] for candle in m1_data)
        bar_high = max(candle[2] for candle in m1_data)
    except:
        bar_low = bar_high = price

    if current_positions:
        side = current_positions['side']
        entry = current_positions['entry']
        atr = indicators['atr']
        stop_voron = StopVoronPro()
        stop_level = stop_voron.calculate_stop(entry, atr, side, price, atr/price, "trending" if trend_flag else "normal")
        if stop_voron.check_exit(price, stop_level, side, bar_low, bar_high):
            logger.info("Stop Voron: срабатывание стопа")
            close_all_positions(client, SYMBOL)
            current_positions = {}

    if trend_flag:
        msg = f"📉 Тренд обнаружен ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}) – закрываем всё"
        logger.info(msg)
        send_telegram(msg)

        positions = client.fetch_positions([SYMBOL])
        if any(p.get('contracts', 0) > 0 for p in positions):
            close_all_positions(client, SYMBOL)
        current_positions = {}
        cancel_all_orders(client, SYMBOL)

        atr = indicators['atr']
        stop_price = price - 2 * atr if direction == "buy" else price + 2 * atr
        risk_usd = TREND_CAPITAL * RISK_PER_TRADE
        distance = abs(price - stop_price)
        if distance <= 0:
            logger.warning("Расстояние до стопа = 0 — пропускаем")
            return
        size = risk_usd / distance

        if size < 0.01:
            logger.info(f"Рассчитанный размер ({size:.4f} ETH) < 0.01 ETH — вход пропущен. Возвращаемся к сетке.")
            send_telegram("⚠️ Размер < 0.01 ETH — возвращаемся к сетке")
            cancel_all_orders(client, SYMBOL)
            place_grid_orders(client, SYMBOL, GRID_CAPITAL)
            return

        try:
            client.create_order(
                symbol=SYMBOL,
                type='market',
                side=direction,
                amount=size,
                params={'tdMode': 'isolated', 'posSide': 'net'}
            )
            msg = (
                f"🆕 Позиция открыта ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')})\n"
                f"{direction.upper()} {size:.4f} ETH\n"
                f"Цена входа: {price:.1f}"
            )
            logger.info(msg)
            send_telegram(msg)
            current_positions = get_positions(client, SYMBOL)
        except Exception as e:
            send_telegram(f"❌ Ошибка трендового входа: {e}")
    else:
        if current_positions:
            close_all_positions(client, SYMBOL)
            current_positions = {}
        cancel_all_orders(client, SYMBOL)
        place_grid_orders(client, SYMBOL, GRID_CAPITAL)

    try:
        open_orders = client.fetch_open_orders(SYMBOL)
        order_count = len(open_orders)
    except:
        order_count = 0

    msg = (
        f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}] Перебалансировка\n"
        f"Цена: {price:.1f} | Капитал: {INITIAL_CAPITAL:.2f} USDT | Ордеров: {order_count}"
    )
    if current_positions:
        msg += f"\nПозиция: {current_positions['side']} {current_positions['size']:.4f} ETH | PnL: {current_pnl:.2f} USDT"
    logger.info(msg)
    send_telegram(msg)

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

        side = last_positions['side']
        size = last_positions['size']
        entry = last_positions['entry']
        result = "✅ Прибыль" if pnl > 0 else "❌ Убыток"
        msg = (
            f"CloseOperation ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')})\n"
            f"{result}\n"
            f"PnL: {pnl:.2f} USDT\n"
            f"{side.upper()} {size:.4f} ETH\n"
            f"Вход: {entry:.1f} → Выход: ~{price:.1f}"
        )
        logger.info(msg)
        send_telegram(msg)

    last_positions = current_positions.copy() if current_positions else {}

    today = date.today()
    if today != last_report_date:
        win_rate = round(winning_trades / total_trades * 100, 1) if total_trades > 0 else 0.0
        report = (
            f"📊 ЕЖЕДНЕВНЫЙ ОТЧЁТ ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')})\n"
            f"Общий PnL: {total_pnl:+.2f} USDT\n"
            f"Сделок: {total_trades}\n"
            f"Win Rate: {win_rate}%\n"
            f"Макс. просадка: {max_drawdown:.2f}%"
        )
        logger.info(report)
        send_telegram(report)
        last_report_date = today

# === Запуск ===
if __name__ == "__main__":
    logger.info("🚀 Бот запущен для ETH с полной защитой риска и Stop Voron v5")
    threading.Thread(target=run_flask, daemon=True).start()
    last_rebalance = 0
    while True:
        now = time.time()
        if int(now / 3600) != int(last_rebalance / 3600):
            rebalance_grid()
            last_rebalance = now
        time.sleep(60)