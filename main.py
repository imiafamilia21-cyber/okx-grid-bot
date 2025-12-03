import time
import requests
import logging
from datetime import datetime
from flask import Flask
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import threading
import os

# ------------------------------
# КОНФИГ
# ------------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GOOGLE_SHEETS_URL = "https://script.google.com/macros/s/AKfycbwph5qJPcUmcKadckHeCpzZkDX5CZH8G9B4p7sysDN_uFixhs5GyHfJh39wnsZlbXru/exec"

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
# GOOGLE SHEETS
# ------------------------------
def send_to_sheets(data: dict):
    try:
        resp = requests.post(GOOGLE_SHEETS_URL, json=data, timeout=10)
        if resp.status_code == 200:
            logger.info("✅ Запись в Google Sheets выполнена")
        else:
            logger.error(f"❌ Sheets: код {resp.status_code}, ответ: {resp.text}")
    except Exception as e:
        logger.error(f"❌ Ошибка запроса к Sheets: {e}")

# ------------------------------
# ОСНОВНАЯ ЛОГИКА
# ------------------------------
def rebalance_grid():
    # пример получения цены — замени на реальную логику
    price = 93208.8
    msg = f"Ребаланс {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Цена {price}"

    # отправка в Telegram
    send_telegram(msg)

    # запись в Google Sheets
    send_to_sheets({"data": msg, "sheetName": "Лист1"})

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
