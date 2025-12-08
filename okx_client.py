import os
from ccxt import okx

def get_okx_demo_client():
    return okx({
        'apiKey': os.getenv("OKX_API_KEY"),
        'secret': os.getenv("OKX_SECRET"),
        'password': os.getenv("OKX_PASS"),
        'sandbox': True,
        'options': {'defaultType': 'swap'}
    })