import time
import requests
import logging
from datetime import datetime, date, timezone
import xml.etree.ElementTree as ET
from okx_client import get_okx_demo_client
from strategy import fetch_ohlcv, calculate_ema_rsi_atr, is_trending, cancel_all_orders, place_grid_orders
from config import SYMBOL, REBALANCE_INTERVAL_HOURS, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

# --- Настройка логирования ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger()

# --- Глобальные переменные ---
INITIAL_CAPITAL = 120.0
RISK_PER_TRADE = 0.01
EXPECTED_ORDERS = 12
last_positions = {}
last_report_date = date.today()
daily_start_pnl = 0.0
last_rebalance = 0
total_pnl = 0.0
total_trades = 0
winning_trades = 0
max_drawdown = 0.0
equity_high = INITIAL_CAPITAL

# --- Google Apps Script Webhook URL ---
GAS_WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbxUdwfnx0g5gJekQ54oHhmB2eciFldGuH_ct8fav-d5wfilf4asVA2kYOBG35Nuwzig/exec"

# --- Новостной kill-switch ---
KEYWORDS = ["tariff", "sanction", "fed", "cpi", "fomc", "export control", "trump", "powell"]
LOCK_HOURS = 4

def news_shock_active() -> bool:
    try:
        r = requests.get("https://www.forexfactory.com/ffcal_week_this.xml", timeout=5)
        root = ET.fromstring(r.content)
        now = datetime.utcnow()
        for event in root.findall("event"):
            impact = event.find("impact").text
            title = event.find("title").text.lower()
            date_el = event.find("date")
            time_el = event.find("time")
            if date_el is None or time_el is None:
                continue
            try:
                event_date = datetime.strptime(date_el.text, "%Y-%m-%d")
                event_time = datetime.strptime(time_el.text, "%H:%M:%S").time()
                ts = datetime.combine(event_date, event_time)
                if abs((ts - now).total_seconds()) < 3600 and impact == "High" and any(k in title for k in KEYWORDS):
                    return True
            except:
                continue
    except:
        pass
    return False

# --- OI-скрининг ---
OI_MULT = 1.5

def oi_screen(client, symbol: str) -> bool:
    try:
        ticker = client.fetch_ticker(symbol)
        oi_now = float(ticker.get("openInterest", 0))
        if oi_now == 0:
            return False
        hist = client.fetch_ohlcv(symbol, timeframe='1d', limit=30)
        oi_30d = [float(c[5]) for c in hist if c[5] is not None]
        if len(oi_30d) < 15:
            return False
        median_oi = sorted(oi_30d)[len(oi_30d)//2]
        return oi_now > OI_MULT * median_oi
    except:
        return False

# --- Stop Voron v4.3 ---
def stop_voron(entry: float, atr: float, side: str, current_price: float = None, bar_low: float = None, 
               k: float = 2.0, min_dist_pct: float = 0.005, max_dist_pct: float = 0.03, spread_pct: float = 0.001) -> float:
    if entry <= 0 or atr < 0:
        raise ValueError("Некорректные входные данные")
    min_atr = entry * 0.001
    atr = max(atr, min_atr)
    if side == "long":
        raw_stop = entry - k * atr
        min_stop = entry * (1 - min_dist_pct)
        stop = min(raw_stop, min_stop)
    else:
        raw_stop = entry + k * atr
        min_stop = entry * (1 + min_dist_pct)
        stop = max(raw_stop, min_stop)
    if current_price is not None:
        if side == "long":
            trail_stop = current_price - k * atr
            stop = max(stop, trail_stop)
        else:
            trail_stop = current_price + k * atr
            stop = min(stop, trail_stop)
    if spread_pct > 0:
        price_for_spread = current_price if current_price is not None else entry
        spread_buffer = price_for_spread * spread_pct
        if side == "long":
            stop = stop + spread_buffer
        else:
            stop = stop - spread_buffer
    current_distance_pct = abs(stop - entry) / entry
    if current_distance_pct > max_dist_pct:
        if side == "long":
            stop = entry * (1 - max_dist_pct)
        else:
            stop = entry * (1 + max_dist_pct)
    return round(stop, 6)

def should_exit_voron(current_price: float, stop_level: float, side: str, 
                      spread_pct: float = 0.0, bar_low: float = None, bar_high: float = None) -> bool:
    if side == "long" and bar_low is not None:
        price_for_exit = bar_low
    elif side == "short" and bar_high is not None:
        price_for_exit = bar_high
    else:
        price_for_exit = current_price
    if spread_pct > 0:
        spread_buffer = price_for_exit * spread_pct
        if side == "long":
            return price_for_exit <= (stop_level + spread_buffer)
        else:
            return price_for_exit >= (stop_level - spread_buffer)
    else:
        if side == "long":
            return price_for_exit <= stop_level
        else:
            return price_for_exit >= stop_level

# --- Telegram ---
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    for _ in range(3):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': text}, timeout=10)
            return
        except:
            time.sleep(2)

# --- Google Sheets logging ---
def log_to_sheet(data):
    try:
        response = requests.post(GAS_WEBHOOK_URL, json=data, timeout=10)
        if response.status_code == 200:
            logger.info(f"📊 Записано в Google Sheets: {data.get('message', 'unknown')}")
        else:
            logger.error(f"❌ Ошибка GAS: {response.status_code}")
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к GAS: {e}")

# --- Основные функции ---
def get_positions(client, symbol):
    try:
        positions = client.fetch_positions([symbol])
        pos = {}
        for p in positions:
            if p.get('contracts', 0) > 0:
                pos['size'] = p['contracts']
                pos['entry'] = p['entryPrice']
                pos['side'] = p['side']
                pos['unrealizedPnl'] = p.get('unrealizedPnl', 0)
        return pos
    except:
        return {}

def close_all_positions(client, symbol):
    try:
        positions = client.fetch_positions([symbol])
        for p in positions:
            if p.get('contracts', 0) > 0:
                side = 'buy' if p['side'] == 'short' else 'sell'
                size = p['contracts']
                client.create_order(symbol=symbol, type='market', side=side, amount=size,
                                    params={'tdMode': 'isolated', 'posSide': 'net', 'reduceOnly': True})
    except:
        pass

def open_trend_position(client, symbol, capital, direction, price, atr):
    try:
        if news_shock_active():
            send_telegram("⚠️ Новостной шок — вход заблокирован")
            logger.info("Новостной шок — вход заблокирован")
            return False
        
        oi_risk = oi_screen(client, symbol)
        risk_mult = 0.5 if oi_risk else 1.0
        risk_usd = capital * RISK_PER_TRADE * risk_mult
        
        stop_distance = atr * 2.0
        size = risk_usd / stop_distance
        min_size = 0.01
        if size < min_size:
            size = min_size

        order = client.create_order(
            symbol=symbol, type='market', side=direction, amount=size,
            params={'tdMode': 'isolated', 'posSide': 'net'}
        )

        stop_price = stop_voron(
            entry=price,
            atr=atr,
            side=direction,
            k=2.0,
            min_dist_pct=0.005,
            max_dist_pct=0.03,
            spread_pct=0.001
        )

        client.create_order(
            symbol=symbol, type='trigger', side='sell' if direction == 'buy' else 'buy',
            amount=size, price=price,
            params={'triggerPrice': stop_price, 'reduceOnly': True, 'tdMode': 'isolated', 'posSide': 'net'}
        )

        msg = f"🚀 Тренд-фолловинг\n{direction.upper()} {size:.4f} BTC\nСтоп: {stop_price:.1f}"
        if oi_risk:
            msg += "\n⚠️ OI высокий — риск снижен в 2 раза"
        logger.info(msg)
        send_telegram(msg)
        
        log_data = {
            'type': 'open_position',
            'symbol': SYMBOL,
            'side': direction,
            'size': size,
            'entry_price': price,
            'exit_price': '',
            'pnl': '',
            'total_pnl': total_pnl,
            'message': msg
        }
        log_to_sheet(log_data)
        return True
    except Exception as e:
        err_msg = f"⚠️ Ошибка тренд-позиции: {e}"
        logger.error(err_msg)
        send_telegram(err_msg)
        return False

def rebalance_grid():
    global last_positions, last_report_date, daily_start_pnl, total_pnl, total_trades, winning_trades
    
    client = get_okx_demo_client()
    
    try:
        ticker = client.fetch_ticker(SYMBOL)
        price = ticker['last']
    except:
        return

    current_positions = get_positions(client, SYMBOL)
    current_pnl = current_positions.get('unrealizedPnl', 0.0)

    today = date.today()
    if today != last_report_date:
        equity = INITIAL_CAPITAL + total_pnl
        if equity > equity_high:
            equity_high = equity
        drawdown = (equity_high - equity) / equity_high * 100 if equity_high > 0 else 0
        if drawdown > max_drawdown:
            max_drawdown = drawdown
        win_rate = round(winning_trades / total_trades * 100, 1) if total_trades > 0 else 0.0
        report = (
            f"📊 ЕЖЕДНЕВНЫЙ ОТЧЁТ\n"
            f"Дата: {datetime.now().strftime('%d.%m.%Y')}\n"
            f"Общий PnL: {total_pnl:+.2f} USDT\n"
            f"Сделок: {total_trades}\n"
            f"Win Rate: {win_rate}%\n"
            f"Макс. просадка: {max_drawdown:.2f}%"
        )
        logger.info(report)
        send_telegram(report)
        last_report_date = today
        
        log_data = {
            'type': 'daily_report',
            'symbol': SYMBOL,
            'side': '',
            'size': '',
            'entry_price': '',
            'exit_price': '',
            'pnl': '',
            'total_pnl': total_pnl,
            'message': 'Ежедневный отчёт'
        }
        log_to_sheet(log_data)

    if current_positions != last_positions:
        if not last_positions and current_positions:
            side = current_positions['side']
            size = current_positions['size']
            entry = current_positions['entry']
            msg = f"🆕 Позиция открыта\n{side.upper()} {size:.4f} BTC\nЦена входа: {entry:.1f}"
            logger.info(msg)
            send_telegram(msg)
            
            log_data = {
                'type': 'open_position',
                'symbol': SYMBOL,
                'side': side,
                'size': size,
                'entry_price': entry,
                'exit_price': '',
                'pnl': '',
                'total_pnl': total_pnl,
                'message': msg
            }
            log_to_sheet(log_data)
        elif last_positions and not current_positions:
            side = last_positions['side']
            size = last_positions['size']
            entry = last_positions['entry']
            pnl = last_positions.get('unrealizedPnl', 0)
            total_pnl += pnl
            total_trades += 1
            if pnl > 0:
                winning_trades += 1
            result = "✅ Прибыль" if pnl > 0 else "❌ Убыток"
            msg = f"CloseOperation\n{result}\nPnL: {pnl:.2f} USDT\nИтого: {total_pnl:+.2f}\n{side.upper()} {size:.4f} BTC\nВход: {entry:.1f} → Выход: ~{price:.1f}"
            logger.info(msg)
            send_telegram(msg)
            
            log_data = {
                'type': 'close_position',
                'symbol': SYMBOL,
                'side': side,
                'size': size,
                'entry_price': entry,
                'exit_price': price,
                'pnl': pnl,
                'total_pnl': total_pnl,
                'message': msg
            }
            log_to_sheet(log_data)
        last_positions = current_positions

    df = fetch_ohlcv(client, SYMBOL)
    indicators = calculate_ema_rsi_atr(df)
    trend_flag, direction = is_trending(indicators)
    if trend_flag:
        if current_positions:
            close_all_positions(client, SYMBOL)
        cancel_all_orders(client, SYMBOL)
        open_trend_position(client, SYMBOL, INITIAL_CAPITAL, direction, indicators['price'], indicators['atr'])
        return
        
    cancel_all_orders(client, SYMBOL)
    place_grid_orders(client, SYMBOL, INITIAL_CAPITAL)
    
    msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Перебалансировка\nЦена: {price:.1f}\nКапитал: {INITIAL_CAPITAL:.2f} USDT\nОрдеров: {len(client.fetch_open_orders(SYMBOL))}"
    if current_positions:
        msg += f"\nПозиция: {current_positions['side']} {current_positions['size']:.4f} BTC\nPnL: {current_pnl:.2f} USDT"
    logger.info(msg)
    send_telegram(msg)
    
    log_data = {
        'type': 'rebalance',
        'symbol': SYMBOL,
        'side': current_positions.get('side', ''),
        'size': current_positions.get('size', ''),
        'entry_price': '',
        'exit_price': '',
        'pnl': current_pnl,
        'total_pnl': total_pnl,
        'message': msg
    }
    log_to_sheet(log_data)

# Flask health-check
from flask import Flask
import threading
import os

app = Flask(__name__)

@app.route('/health')
def health():
    return 'OK'

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    while True:
        now = time.time()
        if int(now / 3600) != int(last_rebalance / 3600):
            rebalance_grid()
            last_rebalance = now
        time.sleep(60)