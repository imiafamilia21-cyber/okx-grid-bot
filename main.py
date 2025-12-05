import time
import requests
import logging
from datetime import datetime, date
from okx_client import get_okx_demo_client
from strategy import fetch_ohlcv, calculate_ema_rsi_atr, is_trending, cancel_all_orders, place_grid_orders
from config import SYMBOL, REBALANCE_INTERVAL_HOURS, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
from StopVoronPro import StopVoronPro

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

stop_voron = StopVoronPro(**StopVoronPro().get_recommended_settings("crypto"))

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    for _ in range(3):
        try:
            # ИСПРАВЛЕНО: без лишних пробелов
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': text}, timeout=10)
            logger.info("✅ Сообщение отправлено в Telegram")
            return
        except Exception as e:
            logger.error(f"Ошибка отправки в Telegram: {e}")
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