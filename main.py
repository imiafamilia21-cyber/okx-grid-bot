import time
import requests
import logging
from datetime import datetime, date
from okx_client import get_okx_demo_client
from strategy import fetch_ohlcv, calculate_ema_rsi_atr, is_trending, cancel_all_orders, place_grid_orders
from config import SYMBOL, REBALANCE_INTERVAL_HOURS, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
from StopVoronPro import StopVoronPro
from flask import Flask
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import threading
import os

# ------------------------------
# ЛОГИРОВАНИЕ
# ------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger()

# ------------------------------
# TELEGRAM
# ------------------------------
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("❌ TELEGRAM_TOKEN или TELEGRAM_CHAT_ID не заданы")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': text}

    try:
        resp = requests.post(url, data=payload, timeout=10)
        if resp.status_code == 200:
            logger.info("✅ Сообщение отправлено в Telegram")
        else:
            logger.error(f"❌ Telegram: код {resp.status_code}; ответ: {resp.text}")
    except Exception as e:
        logger.error(f"❌ Ошибка запроса к Telegram: {e}")

# ------------------------------
# ОСНОВНАЯ ЛОГИКА (сокращённо)
# ------------------------------
def rebalance_grid():
    client = get_okx_demo_client()
    try:
        ticker = client.fetch_ticker(SYMBOL)
        price = ticker['last']
        logger.info(f"Цена {SYMBOL}: {price}")
    except Exception as e:
        logger.error(f"Ошибка получения цены: {e}")
        return

    # Здесь твоя торговая логика...
    send_telegram(f"Ребаланс выполнен, цена {price}")

# ------------------------------
# FLASK СЕРВЕР ДЛЯ HEALTHCHECK
# ------------------------------
app = Flask(__name__)
limiter = Limiter(get_remote_address, app=app, default_limits=["60 per minute"])

@app.route('/health', methods=["GET", "HEAD"])
@limiter.limit("20 per minute")
def health():
    return "OK", 200

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)

# ------------------------------
# ЗАПУСК
# ------------------------------
if __name__ == "__main__":
    logger.info(f"🚀 Запуск бота | Капитал: 120.0 USDT")
    logger.info(f"📊 Сетка: 84.0 USDT | Тренд: 36.0 USDT")

    threading.Thread(target=run_flask, daemon=True).start()

    last_rebalance_hour_bucket = None
    while True:
        now = time.time()
        hour_bucket = int(now / 3600)
        if last_rebalance_hour_bucket is None or hour_bucket != last_rebalance_hour_bucket:
            try:
                rebalance_grid()
            except Exception as e:
                logger.error(f"❌ Ошибка rebalance_grid: {e}")
            last_rebalance_hour_bucket = hour_bucket
        time.sleep(60)
