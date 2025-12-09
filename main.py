import os, time, json, logging, threading, requests
from datetime import datetime, date, timedelta
from flask import Flask
from tenacity import retry, stop_after_attempt, wait_exponential
from okx_client import get_okx_demo_client
from strategy import (fetch_ohlcv, calculate_ema_rsi_atr, is_trending,
                      cancel_all_orders, place_grid_orders,
                      volume_filter, ema21_2days)
from config import *
from StopVoronPro import StopVoronPro

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger()

total_pnl = 0.0; total_trades = 0; winning_trades = 0
equity_high = INITIAL_CAPITAL; max_drawdown = 0.0
last_flat_time = datetime.min; position_open_time = None
trail_stop_price = None; current_trend = None; trend_confirmation = 0
grid_center = None; last_report_date = date.today() - timedelta(days=1)
last_rebalance = 0
STATE_FILE = "state.json"
stop_voron = StopVoronPro(**StopVoronPro().get_recommended_settings("crypto"))

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}
def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump({
            "total_pnl": total_pnl, "total_trades": total_trades, "winning_trades": winning_trades,
            "equity_high": equity_high, "max_drawdown": max_drawdown,
            "last_flat_time": last_flat_time.isoformat() if last_flat_time else None,
            "position_open_time": position_open_time.isoformat() if position_open_time else None,
            "current_trend": current_trend, "trend_confirmation": trend_confirmation, "grid_center": grid_center
        }, f, indent=2)

_state = load_state()
total_pnl          = _state.get("total_pnl", 0.0)
total_trades       = _state.get("total_trades", 0)
winning_trades     = _state.get("winning_trades", 0)
equity_high        = _state.get("equity_high", INITIAL_CAPITAL)
max_drawdown       = _state.get("max_drawdown", 0.0)
last_flat_time     = datetime.fromisoformat(_state["last_flat_time"]) if _state.get("last_flat_time") else datetime.min
position_open_time = datetime.fromisoformat(_state["position_open_time"]) if _state.get("position_open_time") else None
current_trend      = _state.get("current_trend")
trend_confirmation = _state.get("trend_confirmation", 0)
grid_center        = _state.get("grid_center")

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=1, max=10))
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    logger.info(f"TG {r.status_code}")

def log_to_sheet(data):
    if not GAS_WEBHOOK_URL: return
    try:
        r = requests.post(GAS_WEBHOOK_URL, json=data, timeout=10)
    except: pass

def get_positions(client, symbol=SYMBOL):
    try:
        return {p["side"]: {"size": p["contracts"], "entry": p["entryPrice"], "unrealizedPnl": p.get("unrealizedPnl", 0)}
                for p in client.fetch_positions([symbol]) if p.get("contracts", 0) > 0}
    except: return {}

def close_all_positions(client, symbol=SYMBOL):
    global last_flat_time
    try:
        for p in client.fetch_positions([symbol]):
            if p.get("contracts", 0) > 0:
                side = "buy" if p["side"] == "short" else "sell"
                client.create_order(symbol, "market", side, p["contracts"], params={"tdMode": "isolated", "posSide": "net", "reduceOnly": True})
                send_telegram(f"🔴 Closed {p['side']} {symbol}")
        last_flat_time = datetime.utcnow()
        bal = client.fetch_balance()
        free_usdt = float(bal["USDT"]["free"])
        if free_usdt > 10:
            amt = round(free_usdt*0.3, 2)
            client.transfer("USDT", amt, "trading", "funding")
            client.privatePost_finance_savings_purchase({"ccy": "USDT", "amt": amt})
            logger.info(f"30 % ({amt} USDT) → Earn")
    except Exception as e:
        logger.error(f"close_all: {e}")

def compute_size(entry, stop, capital):
    risk_usd = capital * RISK_PER_TRADE
    dist = abs(entry - stop)
    if dist <= 0: return MIN_ORDER_SIZE
    size = risk_usd / dist
    max_size = (capital * MAX_EQUITY_PCT) / entry
    return max(min(size, max_size), MIN_ORDER_SIZE)

def adaptive_tp_multiplier(atr_pct):
    if atr_pct > 2.5: return 1.4
    if atr_pct < 1.5: return 1.8
    return 1.6

def place_take_profit(client, symbol, side, entry, stop, size):
    try:
        atr_pct = abs(entry - stop)/entry*100
        dist = abs(entry - stop) * adaptive_tp_multiplier(atr_pct)
        tp_price = round(entry + dist*(1 if side=="buy" else -1), 1)
        client.create_order(symbol, "limit", "sell" if side=="buy" else "buy", size, tp_price,
                            params={"reduceOnly": True, "tdMode": "isolated", "posSide": "net"})
        send_telegram(f"✅ TP {side} {symbol} {tp_price}")
    except Exception as e:
        logger.error(f"TP: {e}")

def trail_stop(side, price, atr):
    return round(price - TRAIL_ATR_MUL*atr if side=="buy" else price + TRAIL_ATR_MUL*atr, 1)

def check_time_stop(open_time):
    return (datetime.utcnow() - open_time).days >= TIMEOUT_DAYS if open_time else False

app = Flask(__name__)
@app.route("/health", methods=["GET", "HEAD"])
def health(): return "OK", 200
def run_flask(): app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), threaded=True)

def rebalance_grid():
    global last_rebalance, total_pnl, total_trades, winning_trades, equity_high, max_drawdown
    global current_trend, trend_confirmation, position_open_time, trail_stop_price, grid_center, last_report_date

    client = get_okx_demo_client()
    try:
        price = client.fetch_ticker(SYMBOL)["last"]
    except:
        logger.error("price fail"); return

    now = time.time()
    if int(now/300) != int(last_rebalance/300):
        if not volume_filter(client):
            logger.info("Volume filter – skip")
            confirmed_trend = False
            return

        positions = get_positions(client)
        if positions and position_open_time:
            if check_time_stop(position_open_time):
                logger.info("Time-stop"); send_telegram("⏰ Time-stop"); close_all_positions(client); positions = {}; position_open_time = None; trail_stop_price = None
            else:
                side = next(iter(positions.keys()))
                df = fetch_ohlcv(client, limit=20)
                ind = calculate_ema_rsi_atr(df)
                new_stop = trail_stop(side, price, ind["atr"])
                if new_stop != trail_stop_price:
                    for o in client.fetch_open_orders(SYMBOL):
                        if o["type"]=="trigger" and o["side"]!=side and o["reduceOnly"]:
                            client.cancel_order(o["id"], SYMBOL)
                    client.create_order(SYMBOL, "trigger", "sell" if side=="buy" else "buy", positions[side]["size"],
                                        params={"triggerPrice": new_stop, "reduceOnly": True, "tdMode": "isolated", "posSide": "net"})
                    trail_stop_price = new_stop
                    logger.info(f"Trail → {new_stop}")

        if int(now/3600) != int(last_rebalance/3600):
            df = fetch_ohlcv(client, limit=100)
            ind = calculate_ema_rsi_atr(df)
            atr = ind["atr"]
            trend_flag, trend_dir = is_trending(ind)
            if trend_flag and trend_dir==current_trend:
                trend_confirmation +=1
            elif trend_flag:
                current_trend, trend_confirmation = trend_dir, 1
            else:
                current_trend, trend_confirmation = None, 0

            if positions:
                side = next(iter(positions.keys()))
                if current_trend and current_trend != side:
                    if abs(price - ind["ema"]) > 1.5*atr:
                        logger.info("Trend flip + far from EMA – close")
                        close_all_positions(client); positions = {}; position_open_time = None; trail_stop_price = None

            vola = ind["sigma7"]
            margin_ratio = MR_LOW_VOL if vola < VOL_THRESHOLD_MR else MR_HIGH_VOL
            min_atr_pct = ind["min_atr_pct"]
            confirmed_trend = trend_confirmation>=1 and vola<0.01 and atr/price >= min_atr_pct

            if confirmed_trend and ema21_2days(client):
                if current_trend=="buy":
                    place_grid_orders(client, capital=GRID_CAPITAL*margin_ratio, upper_pct=ASYMMETRY_RANGE, lower_pct=5.0)
                else:
                    place_grid_orders(client, capital=GRID_CAPITAL*margin_ratio, upper_pct=5.0, lower_pct=ASYMMETRY_RANGE)
            elif confirmed_trend:
                place_grid_orders(client, capital=GRID_CAPITAL*margin_ratio)
            else:
                cancel_all_orders(client)

            eq = INITIAL_CAPITAL + total_pnl
            if positions:
                eq += sum([p["unrealizedPnl"] for p in positions.values()])
            equity_high = max(equity_high, eq)
            max_drawdown = max(max_drawdown, (equity_high-eq)/equity_high*100 if equity_high>0 else 0)

            today = date.today()
            if today != last_report_date:
                wr = round(winning_trades/total_trades*100,1) if total_trades else 0
                send_telegram(f"📈 Daily {today:%d.%m.%Y}  PnL={total_pnl:+.2f}  trades={total_trades}  WR={wr}%  DD={max_drawdown:.2f}%")
                last_report_date = today
            save_state()
        last_rebalance = now
    time.sleep(60)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    send_telegram("🟢 Bot started (2025-12-09, all 10 opts ON)")
    while True:
        try:
            rebalance_grid()
        except Exception as e:
            logger.error(f"LOOP: {e}")
            time.sleep(60)