import time
import requests
import logging
from datetime import datetime, date
import threading
import os
from flask import Flask

# Внешние модули проекта (должны быть в репозитории)
from okx_client import get_okx_demo_client
from strategy import fetch_ohlcv, calculate_ema_rsi_atr, is_trending, cancel_all_orders, place_grid_orders
from config import SYMBOL, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
from StopVoronPro import StopVoronPro

# -------------------------
# Логирование
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# -------------------------
# Константы капитала и риска
# -------------------------
INITIAL_CAPITAL = 120.0
GRID_CAPITAL = 84.0
TREND_CAPITAL = 36.0
RISK_PER_TRADE = 0.008
MIN_ORDER_SIZE = 0.01

# -------------------------
# Состояние
# -------------------------
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

# -------------------------
# StopVoronPro (рекомендованные параметры)
# -------------------------
stop_voron = StopVoronPro(**StopVoronPro().get_recommended_settings("crypto"))

# -------------------------
# Telegram
# -------------------------
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.info("Telegram отключен: нет токена или chat_id")
        return
    for _ in range(2):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': text}, timeout=10)
            logger.info("✅ Сообщение отправлено в Telegram")
            return
        except Exception as e:
            logger.error(f"Ошибка отправки в Telegram: {e}")
            time.sleep(1)

# -------------------------
# Клиент биржи и позиции
# -------------------------
def get_positions(client, symbol):
    try:
        positions = client.fetch_positions([symbol])
        pos = {}
        for p in positions:
            if p.get('contracts', 0) > 0:
                pos['size'] = p['contracts']
                pos['entry'] = p.get('entryPrice')
                pos['side'] = p.get('side')
                pos['unrealizedPnl'] = p.get('unrealizedPnl', 0.0)
        return pos
    except Exception as e:
        logger.error(f"Ошибка получения позиций: {e}")
        return {}

def close_all_positions(client, symbol):
    try:
        positions = client.fetch_positions([symbol])
        for p in positions:
            if p.get('contracts', 0) > 0:
                side = 'buy' if p['side'] == 'short' else 'sell'
                size = p['contracts']
                client.create_order(
                    symbol=symbol, type='market', side=side, amount=size,
                    params={'tdMode': 'isolated', 'posSide': 'net', 'reduceOnly': True}
                )
                send_telegram(
                    f"🔴 Выход {symbol} {p['side']} size={size} exit=market "
                    f"entry={p.get('entryPrice')} pnl={p.get('unrealizedPnl', 0.0)}"
                )
    except Exception as e:
        logger.error(f"Ошибка закрытия позиции: {e}")

# -------------------------
# Ордерный размер и тейк-профит
# -------------------------
def compute_position_size(entry: float, stop: float, capital: float, max_exposure_pct: float = 0.3) -> float:
    try:
        risk_usd = capital * RISK_PER_TRADE
        r_dist = abs(entry - stop)
        if r_dist <= 0:
            return MIN_ORDER_SIZE
        size = risk_usd / r_dist
        max_size = (capital * max_exposure_pct) / entry if entry > 0 else 0.0
        size = min(size