requests.post(
    "https://script.google.com/macros/s/AKfycb.../exec",
    json={
        "type": "trade",
        "symbol": "BTC-USDT",
        "side": "buy",
        "size": 0.1,
        "entry_price": 42000,
        "exit_price": 43000,
        "pnl": 100,
        "total_pnl": 500
    }
)
