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
GOOGLE_SHEETS_URL = "https://script.google.com/macros/s/AKfycbyJENok5yfB9rOY85FjkZ85oKzV0v5bwZEGfP0HhX8AAtT8f9LAbI71qLmXPnQqrA6t/exec"

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
            logger.info("✅ Запись в Google Sheets