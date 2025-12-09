import ccxt
from config import API_KEY, SECRET_KEY, PASSPHRASE

def get_okx_demo_client():
    exchange = ccxt.okx({
        'apiKey': API_KEY,
        'secret': SECRET_KEY,
        'password': PASSPHRASE,
        'options': {'defaultType': 'swap'},
        'hostname': 'www.okx.com',
    })
    exchange.set_sandbox_mode(True)
    return exchange