import os
import logging
from ccxt import okx

logger = logging.getLogger("okx_client")

def get_okx_demo_client():
    api_key = os.getenv("OKX_API_KEY")
    api_secret = os.getenv("OKX_SECRET")
    api_pass = os.getenv("OKX_PASS")
    
    if not api_key or not api_secret or not api_pass:
        logger.error("❌ Не заданы API ключи OKX")
        raise ValueError("API ключи OKX не заданы")
    
    try:
        return okx({
            'apiKey': api_key,
            'secret': api_secret,
            'password': api_pass,
            'sandbox': True,
            'options': {'defaultType': 'swap'},
            'enableRateLimit': True,
            'timeout': 30000
        })
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации OKX клиента: {e}")
        raise