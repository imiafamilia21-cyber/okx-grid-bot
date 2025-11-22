import requests
from datetime import datetime

url = "https://script.google.com/macros/s/AKfycbx-kl7hVpC6BmgmgdUcJ-_f0qNQiHCyMSXpCsiSdD_h-sPBdfjtd567hCOdTv1I56G2kQ/exec"

payload = {
    "type": "test",
    "symbol": "BTCUSDT",
    "side": "buy",
    "size": 1,
    "entry_price": 100,
    "exit_price": 110,
    "pnl": 10,
    "total_pnl": 10,
    "message": "python test",
    "timestamp": datetime.now().isoformat()
}

response = requests.post(url, json=payload)
print("Status:", response.status_code)
print("Response:", response.text)
