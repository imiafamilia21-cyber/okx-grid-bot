import requests
import time
from datetime import datetime

# --- Google Apps Script Webhook URL ---
GAS_WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbybRhQrqxaiLmKDFiBEn1wppLGOsQVEunUbaOirYfs0mHWa2Yjuhpnuq2Dpj3ciCfvSaQ/exec"

def send_log(data):
    try:
        response = requests.post(GAS_WEBHOOK_URL, json=data, timeout=10)
        print("Status:", response.status_code)
        print("Response:", response.text)
    except Exception as e:
        print("Error:", e)

def main():
    while True:
        log_entry = {
            "type": "trade",
            "symbol": "BTCUSDT",
            "side": "buy",
            "size": 0.01,
            "entry_price": 12345,
            "exit_price": "",
            "pnl": "",
            "total_pnl": "",
            "message": "log.csv запись",
            "timestamp": datetime.now().isoformat()
        }
        send_log(log_entry)
        time.sleep(10)

if __name__ == "__main__":
    main()
