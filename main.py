import time
import requests
import logging
from datetime import datetime, date
from okx_client import get_okx_demo_client
from strategy import fetch_ohlcv, calculate_ema_rsi_atr, is_trending, cancel_all_orders, place_grid_orders
from config import SYMBOL, REBALANCE_INTERVAL_HOURS, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
from StopVoronPro import StopVoronPro

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', handlers=[logging.StreamHandler()])
logger = logging.getLogger()

INITIAL_CAPITAL = 120.0
GRID_CAPITAL = 84.0
TREND_CAPITAL = 36.0
RISK_PER_TRADE = 0.008
MIN_ORDER_SIZE = 0.01

last_positions = {}
last_report_date = date.today()
total_pnl = 0.0
total_trades = 0
winning_trades = 0
max_drawdown = 0.0
equity_high = INITIAL_CAPITAL
grid_center = None
current_trend = None
trend_confirmation = 0

stop_voron = StopVoronPro(**StopVoronPro().get_recommended_settings("crypto"))

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("❌ TELEGRAM_TOKEN или TELEGRAM_CHAT_ID не заданы")
        return
    for _ in range(3):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': text}, timeout=10)
            logger.info("✅ Сообщение отправлено в Telegram")
            return
        except Exception as e:
            logger.error(f"❌ Ошибка отправки в Telegram: {e}")
            time.sleep(2)

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
    except Exception as e:
        logger.error(f"❌ Ошибка получения позиций: {e}")
        return {}

def close_all_positions(client, symbol):
    try:
        positions = client.fetch_positions([symbol])
        for p in positions:
            if p.get('contracts', 0) > 0:
                side = 'buy' if p['side'] == 'short' else 'sell'
                size = p['contracts']
                client.create_order(
                    symbol=symbol,
                    type='market',
                    side=side,
                    amount=size,
                    params={'tdMode': 'isolated', 'posSide': 'net', 'reduceOnly': True}
                )
                send_telegram(f"🔴 Закрыта позиция {p['side']} {symbol} size={size} entry={p['entryPrice']} pnl={p.get('unrealizedPnl',0):+.2f}")
    except Exception as e:
        logger.error(f"❌ Ошибка закрытия позиции: {e}")

def compute_position_size(entry: float, stop: float, capital: float, max_exposure_pct: float = 0.3) -> float:
    risk_usd = capital * RISK_PER_TRADE
    r_dist = abs(entry - stop)
    if r_dist <= 0:
        return MIN_ORDER_SIZE
    size = risk_usd / r_dist
    max_size = (capital * max_exposure_pct) / entry if entry > 0 else 0.0
    size = min(size, max_size)
    return max(size, MIN_ORDER_SIZE)

def place_take_profit(client, symbol, side, entry, stop, size):
    try:
        risk_distance = abs(entry - stop)
        tp_distance = risk_distance * 2.0
        tp_price = entry + tp_distance if side == "buy" else entry - tp_distance
        tp_price = round(tp_price, 1)
        client.create_order(
            symbol=symbol,
            type='limit',
            side='sell' if side == 'buy' else 'buy',
            amount=size,
            price=tp_price,
            params={'reduceOnly': True, 'tdMode': 'isolated', 'posSide': 'net'}
        )
        send_telegram(f"✅ Take-Profit установлен\n{symbol} {side.upper()}\nЦель: {tp_price}\nРазмер: {size:.4f} BTC")
    except Exception as e:
        logger.error(f"❌ Ошибка Take-Profit: {e}")

def should_rebalance_grid(current_price: float, grid_center: float, grid_range_pct: float) -> bool:
    if grid_center is None:
        return True
    upper = grid_center * (1 + grid_range_pct / 100)
    lower = grid_center * (1 - grid_range_pct / 100)
    return not (lower <= current_price <= upper)

def rebalance_grid():
    global last_positions, last_report_date, total_pnl, total_trades, winning_trades, grid_center, current_trend, trend_confirmation
    
    client = get_okx_demo_client()
    try:
        ticker = client.fetch_ticker(SYMBOL)
        price = ticker['last']
    except Exception as e:
        logger.error(f"❌ Ошибка получения цены: {e}")
        return

    try:
        m1_data = client.fetch_ohlcv(SYMBOL, '1m', limit=5)
        bar_low = min(candle[3] for candle in m1_data)
        bar_high = max(candle[2] for candle in m1_data)
        current_volatility = (bar_high - bar_low) / price
    except:
        bar_low = bar_high = price
        current_volatility = 0

    df = fetch_ohlcv(client, SYMBOL)
    indicators = calculate_ema_rsi_atr(df)
    trend_flag, trend_direction = is_trending(indicators)
    
    if trend_flag and trend_direction == current_trend:
        trend_confirmation += 1
    elif trend_flag:
        current_trend = trend_direction
        trend_confirmation = 1
    else:
        current_trend = None
        trend_confirmation = 0

    confirmed_trend = trend_confirmation >= 2 and current_trend is not None
    current_positions = get_positions(client, SYMBOL)
    
    if current_positions:
        position_side = current_positions['side']
        position_entry = current_positions['entry']
        stop_level = stop_voron.calculate_stop(
            entry=position_entry,
            atr=indicators['atr'],
            side=position_side,
            current_price=price,
            volatility_ratio=indicators['atr'] / price,
            market_regime="trending" if confirmed_trend else "normal"
        )
        if stop_voron.check_exit(price, stop_level, position_side, bar_low, bar_high):
            logger.info("🔴 Сработал Stop-Loss по защите от гэпа")
            send_telegram(f"🔴 Stop-Loss\n{SYMBOL} {position_side.upper()}\nВход: {position_entry:.1f}\nСтоп: {stop_level:.1f}\nТекущая: {price:.1f}")
            close_all_positions(client, SYMBOL)
            current_positions = get_positions(client, SYMBOL)

    if confirmed_trend:
        volatility_threshold = 0.03
        if current_volatility < volatility_threshold:
            if not current_positions:
                stop_price = stop_voron.calculate_stop(
                    entry=price,
                    atr=indicators['atr'],
                    side=current_trend,
                    current_price=price,
                    volatility_ratio=indicators['atr'] / price,
                    market_regime="trending"
                )
                size = compute_position_size(price, stop_price, TREND_CAPITAL)
                if size > 0:
                    try:
                        client.create_order(