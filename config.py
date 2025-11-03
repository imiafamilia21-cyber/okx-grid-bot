import os
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("OKX_API_KEY")
SECRET_KEY = os.getenv("OKX_API_SECRET")
PASSPHRASE = os.getenv("OKX_PASSPHRASE")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOL = "BTC-USDT-SWAP"
GRID_RANGE_PCT = 6.0
GRID_LEVELS = 6
REBALANCE_INTERVAL_HOURS = 1
TIMEFRAME = "15m"