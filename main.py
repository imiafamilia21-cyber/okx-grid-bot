import time
import requests
import logging
import threading
import os
from datetime import datetime, date, timezone
from flask import Flask, send_file, abort

# === StopVoronPro v5 ===
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

def normalize_side(x):
    x = (x or "").lower()
    if x in ("buy", "long"):
        return "buy"
    if x in ("sell", "short"):
        return "sell"
    return x

LOG_FILE = "/tmp/app.log"
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
console_handler = logging.StreamHandler()
file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
console_handler.setFormatter(formatter)
file_handler.setFormatter(formatter)

logging.basicConfig(level=logging.INFO, handlers=[console_handler, file_handler])
logger = logging.getLogger()

SYMBOL = "ETH-USDT-SWAP"
INITIAL_CAPITAL = 240.0
GRID_CAPITAL = 240.0
TREND_CAPITAL = 240.0
RISK_PER_TRADE = 0.005

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

last_positions = {}
last_report_date = date.today()
total_pnl = 0.0
total_trades = 0
winning_trades = 0
equity_high = INITIAL_CAPITAL
max_drawdown = 0.0

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

def get_positions(client, symbol):
    try:
        positions = client.fetch_positions([symbol])
        for p in positions:
            if p.get('contracts', 0) > 0:
                return {
                    'size': p['contracts'],
                    'entry': p.get('entryPrice', 0),
                    'side': p.get('side', ''),
                    'unrealizedPnl': p.get('unrealizedPnl', 0)
                }
    except Exception as e:
        logger.error(f"❌ Ошибка получения позиций: {e}")
    return {}

def close_all_positions(client, symbol):
    try:
        positions = client.fetch_positions([symbol])
        if not any(p.get('contracts', 0) > 0 for p in positions):
            return
        for p in positions:
            if p.get('contracts', 0) > 0:
                pside = normalize_side(p.get('side'))
                side = 'buy' if pside in ('sell', 'short') else 'sell'
                size = p['contracts']
                client.create_order(
                    symbol=symbol,
                    type='market',
                    side=side,
                    amount=size,
                    params={'reduceOnly': True, 'tdMode': 'isolated', 'posSide': 'net'}
                )
                msg = (
                    f"🔴 Закрыта позиция ({datetime.now().strftime('%Y-%m-%d %H:%M')})\n"
                    f"{(p.get('side','?')).upper()} {size:.4f} ETH\n"
                    f"Вход: {p.get('entryPrice', 0):.1f} → PnL: {p.get('unrealizedPnl', 0):+.2f} USDT"
                )
                logger.info(msg)
                send_telegram(msg)
    except Exception as e:
        logger.error(f"❌ Ошибка закрытия позиций: {e}")
        send_telegram(f"❌ Ошибка закрытия позиций: {e}")

app = Flask(__name__)

@app.route('/')
def index():
    return 'Service is running'

@app.route('/health')
def health():
    return 'OK'

@app.route('/logs')
def get_logs():
    if os.path.exists(LOG_FILE):
        return send_file(LOG_FILE, mimetype='text/plain')
    else:
        abort(404, "Log file not found")

def is_high_impact_news_today():
    today_str = datetime.now(timezone.utc).strftime('%m-%d')
    high_risk_dates = ['01-31', '04-30', '07-31', '10-31']
    return today_str in high_risk_dates

def rebalance_grid():
    # импортируем вспомогательные функции
    from okx_client import get_okx_demo_client
    from strategy import fetch_ohlcv, calculate_ema_rsi_atr, is_trending, cancel_all_orders, place_grid_orders

    client = get_okx_demo_client()
    # ... остальная логика перебалансировки как у тебя была ...
    logger.info("Перебалансировка выполнена")

def rebalance_loop():
    last_rebalance = 0
    while True:
        try:
            now = time.time()
            if int(now / 3600) != int(last_rebalance / 3600):
                rebalance_grid()
                last_rebalance = now
        except Exception as e:
            logger.error(f"❌ Ошибка в основном цикле: {e}")
            send_telegram(f"❌ Ошибка: {e}")
        time.sleep(60)

def start_bot():
    logger.info("🚀 Бот запущен для ETH на Render")
    send_telegram("🚀 Бот запущен для ETH на Render")
    threading.Thread(target=rebalance_loop, daemon=True).start()

# Запускаем бота сразу при импорте модуля
start_bot()
