import time
import requests
import logging
from datetime import datetime, date, time as dt_time
from okx_client import get_okx_demo_client
from strategy import fetch_ohlcv, calculate_ema_rsi_atr, is_trending, cancel_all_orders, place_grid_orders
from config import SYMBOL, REBALANCE_INTERVAL_HOURS, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, GAS_WEBHOOK_URL
from StopVoronPro import StopVoronPro

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', handlers=[logging.StreamHandler()])
logger = logging.getLogger()

# 🔥 ИЗМЕНЕНО: Снижение риска до 0.6%
INITIAL_CAPITAL = 120.0
GRID_CAPITAL = 84.0
TREND_CAPITAL = 36.0
RISK_PER_TRADE = 0.006  # Было 0.008
MAX_EQUITY_PCT = 0.30   # Новое: максимальная экспозиция 30%
MIN_ORDER_SIZE = 0.01
MAX_MARGIN_RATIO = 0.60  # Новое: ограничение margin ratio

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
position_open_time = None
last_flat_time = None

stop_voron = StopVoronPro(**StopVoronPro().get_recommended_settings("crypto"))

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    for _ in range(3):
        try:
            # 🔥 ИЗМЕНЕНО: Исправлен URL без лишних пробелов
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': text}, timeout=10)
            logger.info("✅ Сообщение отправлено в Telegram")
            return
        except Exception as e:
            logger.error(f"❌ Ошибка Telegram: {e}")
            time.sleep(2)

def log_to_sheet(data):
    """Отправка данных в Google Sheets"""
    try:
        if not GAS_WEBHOOK_URL:
            return
        response = requests.post(GAS_WEBHOOK_URL, json=data, timeout=10)
        if response.status_code == 200:
            logger.info("✅ Запись в Google Sheets")
        else:
            logger.error(f"❌ Sheets: {response.status_code} {response.text}")
    except Exception as e:
        logger.error(f"❌ Ошибка Sheets: {e}")

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
    """Закрытие всех позиций с учётом хедж-режима"""
    try:
        positions = client.fetch_positions([symbol])
        for p in positions:
            if p.get('contracts', 0) > 0:
                side = 'buy' if p['side'] == 'short' else 'sell'
                size = p['contracts']
                # 🔥 ИЗМЕНЕНО: Использование хедж-режима
                params = {
                    'tdMode': 'isolated',
                    'posSide': ('long' if side == 'sell' else 'short'),  # Хедж-режим
                    'reduceOnly': True
                }
                client.create_order(
                    symbol=symbol,
                    type='market',
                    side=side,
                    amount=size,
                    params=params
                )
                msg = f"🔴 Закрыта позиция {p['side']} {symbol} size={size} entry={p['entryPrice']} pnl={p.get('unrealizedPnl',0):+.2f}"
                send_telegram(msg)
                log_to_sheet({
                    'timestamp': datetime.now().isoformat(),
                    'type': 'close_position',
                    'symbol': SYMBOL,
                    'side': p['side'],
                    'size': size,
                    'entry_price': p['entryPrice'],
                    'exit_price': client.fetch_ticker(SYMBOL)['last'],
                    'pnl': p.get('unrealizedPnl', 0),
                    'total_pnl': total_pnl,
                    'message': msg
                })
                global last_flat_time
                last_flat_time = datetime.now()  # Запоминаем время закрытия
    except Exception as e:
        logger.error(f"❌ Ошибка закрытия: {e}")

def compute_position_size(entry: float, stop: float, capital: float) -> float:
    """Расчёт размера позиции с ограничением экспозиции"""
    risk_usd = capital * RISK_PER_TRADE
    r_dist = abs(entry - stop)
    if r_dist <= 0:
        return MIN_ORDER_SIZE
    
    size = risk_usd / r_dist
    max_size = (capital * MAX_EQUITY_PCT) / entry if entry > 0 else 0.0
    size = min(size, max_size)
    return max(size, MIN_ORDER_SIZE)

def place_take_profit(client, symbol, side, entry, stop, size):
    """Take-Profit с дистанцией 1.6R вместо 2.0R"""
    try:
        # 🔥 ИЗМЕНЕНО: Дистанция 1.6 вместо 2.0
        tp_distance = abs(entry - stop) * 1.6
        tp_price = entry + tp_distance if side == "buy" else entry - tp_distance
        tp_price = round(tp_price, 1)
        
        # 🔥 ИЗМЕНЕНО: Использование хедж-режима
        params = {
            'reduceOnly': True,
            'tdMode': 'isolated',
            'posSide': ('long' if side == 'buy' else 'short')
        }
        
        client.create_order(
            symbol=symbol,
            type='limit',
            side='sell' if side == 'buy' else 'buy',
            amount=size,
            price=tp_price,
            params=params
        )
        msg = f"✅ Take-Profit установлен\n{symbol} {side.upper()}\nЦель: {tp_price}\nРазмер: {size:.4f} BTC"
        send_telegram(msg)
        log_to_sheet({
            'timestamp': datetime.now().isoformat(),
            'type': 'take_profit',
            'symbol': SYMBOL,
            'side': side,
            'size': size,
            'entry_price': entry,
            'exit_price': tp_price,
            'pnl': '',
            'total_pnl': total_pnl,
            'message': msg
        })
    except Exception as e:
        logger.error(f"❌ Ошибка Take-Profit: {e}")

def trail_stop(client, symbol, side, current_price, atr):
    """Трейлинг-стоп каждый час на 0.75×ATR"""
    if side == 'buy':
        new_sl = current_price - 0.75 * atr
    else:
        new_sl = current_price + 0.75 * atr
    return round(new_sl, 1)

def check_time_stop(open_time):
    """Закрытие позиции через 12 дней"""
    if open_time is None:
        return False
    return (datetime.now() - open_time).days >= 12

def should_rebalance_grid(current_price: float, grid_center: float, grid_range_pct: float) -> bool:
    if grid_center is None:
        return True
    upper = grid_center * (1 + grid_range_pct / 100)
    lower = grid_center * (1 - grid_range_pct / 100)
    return not (lower <= current_price <= upper)

# 🔥 ИЗМЕНЕНО: Новая функция для проверки margin ratio
def check_margin_ratio(client):
    try:
        account = client.private_get_account()
        m_ratio = float(account['data'][0]['mgnRatio'])
        return m_ratio < MAX_MARGIN_RATIO
    except Exception as e:
        logger.error(f"❌ Ошибка проверки margin ratio: {e}")
        return True  # Разрешить вход при ошибке

def rebalance_grid():
    global last_positions, last_report_date, total_pnl, total_trades, winning_trades, grid_center, current_trend, trend_confirmation, position_open_time, last_flat_time
    
    client = get_okx_demo_client()
    
    # 🔥 ИЗМЕНЕНО: Проверка margin ratio перед началом
    if not check_margin_ratio(client):
        logger.warning("⚠️ Margin ratio too high - skip rebalance")
        return
    
    try:
        ticker = client.fetch_ticker(SYMBOL)
        price = ticker['last']
    except Exception as e:
        logger.error(f"❌ Ошибка цены: {e}")
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
    
    # 🔥 ИЗМЕНЕНО: Смягченные условия для тренда
    if trend_flag and trend_direction == current_trend:
        trend_confirmation += 1
    elif trend_flag:
        current_trend = trend_direction
        trend_confirmation = 1
    else:
        current_trend = None
        trend_confirmation = 0

    # 🔥 ИЗМЕНЕНО: Подтверждение тренда 1 вместо 2, волатильность 1% вместо 3%
    confirmed_trend = trend_confirmation >= 1 and current_volatility < 0.01
    
    # 🔥 ИЗМЕНЕНО: Проверка на утренний гэп
    now = datetime.utcnow().time()
    if now < dt_time(0, 15):
        logger.info("⏰ Утренний гэп - пропускаем вход")
        confirmed_trend = False

    current_positions = get_positions(client, SYMBOL)
    
    # 🔥 ИЗМЕНЕНО: Кулдаун 6 часов после закрытия
    if last_flat_time and (datetime.now() - last_flat_time).seconds < 6 * 3600:
        logger.info("⏰ Кулдаун после закрытия - пропускаем вход")
        confirmed_trend = False
    
    # 🔥 ИЗМЕНЕНО: Пересчёт просадки внутри дня
    current_eq = INITIAL_CAPITAL + total_pnl
    if current_positions:
        current_eq += current_positions.get('unrealizedPnl', 0)
    
    global equity_high, max_drawdown
    equity_high = max(equity_high, current_eq)
    drawdown = (equity_high - current_eq) / equity_high * 100 if equity_high > 0 else 0
    max_drawdown = max(max_drawdown, drawdown)
    
    # 🔥 ИЗМЕНЕНО: Проверка time-stop и трейлинга
    if current_positions and position_open_time:
        if check_time_stop(position_open_time):
            logger.info("⏰ Time-stop сработал (12 дней)")
            send_telegram("⏰ Time-stop: позиция закрыта по времени (12 дней)")
            close_all_positions(client, SYMBOL)
            current_positions = get_positions(client, SYMBOL)
            position_open_time = None
        else:
            # Трейлинг каждый час
            new_stop = trail_stop(client, SYMBOL, current_positions['side'], price, indicators['atr'])
            logger.info(f"🔄 Трейлинг-стоп обновлён: {new_stop}")
            # Обновление стоп-ордера

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
            position_open_time = None

    if confirmed_trend:
        # 🔥 ИЗМЕНЕНО: Проверка минимального ATR перед входом
        if indicators['atr'] < price * 0.003:
            logger.info('ATR too low – skip entry')
        else:
            if not current_positions:
                # 🔥 ИЗМЕНЕНО: Отмена всех ордеров ПЕРЕД входом
                cancel_all_orders(client, SYMBOL)
                
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
                        # 🔥 ИЗМЕНЕНО: Использование хедж-режима
                        params = {
                            'tdMode': 'isolated',
                            'posSide': ('long' if current_trend == 'buy' else 'short')
                        }
                        client.create_order(
                            symbol=SYMBOL,
                            type='market',
                            side=current_trend,
                            amount=size,
                            params=params
                        )
                        send_telegram(f"📲 Вход в сделку\n{SYMBOL} {current_trend.upper()}\nВход: {price:.1f}\nСтоп: {stop_price:.1f}\nРазмер: {size:.4f} BTC")
                        log_to_sheet({
                            'timestamp': datetime.now().isoformat(),
                            'type': 'open_position',
                            'symbol': SYMBOL,
                            'side': current_trend,
                            'size': size,
                            'entry_price': price,
                            'exit_price': '',
                            'pnl': '',
                            'total_pnl': total_pnl,
                            'message': f"Вход в сделку"
                        })
                        
                        # 🔥 ИЗМЕНЕНО: Использование хедж-режима для стопа
                        stop_params = {
                            'triggerPrice': stop_price,
                            'reduceOnly': True,
                            'tdMode': 'isolated',
                            'posSide': ('long' if current_trend == 'buy' else 'short')
                        }
                        client.create_order(
                            symbol=SYMBOL,
                            type='trigger',
                            side='sell' if current_trend == 'buy' else 'buy',
                            amount=size,
                            price=price,
                            params=stop_params
                        )
                        place_take_profit(client, SYMBOL, current_trend, price, stop_price, size)
                        position_open_time = datetime.now()  # Сохраняем дату открытия
                    except Exception as e:
                        logger.error(f"❌ Ошибка открытия: {e}")
                        send_telegram(f"❌ Ошибка открытия: {e}")
            
            # 🔥 ИЗМЕНЕНО: Асимметричные границы сетки
            if current_trend == "buy":
                cancel_all_orders(client, SYMBOL)
                place_grid_orders(
                    client, SYMBOL, GRID_CAPITAL, 
                    upper_pct=18.0,  # Увеличен верхний диапазон
                    lower_pct=6.0    # Уменьшен нижний диапазон
                )
            else:
                cancel_all_orders(client, SYMBOL)
                place_grid_orders(
                    client, SYMBOL, GRID_CAPITAL, 
                    upper_pct=6.0,   # Уменьшен верхний диапазон
                    lower_pct=18.0   # Увеличен нижний диапазон
                )
            grid_center = price
    else:
        if current_positions:
            # 🔥 ИЗМЕНЕНО: Двухфакторный выход из тренда
            if (current_trend != current_positions['side']) and (abs(price - indicators['ema']) > 1.5 * indicators['atr']):
                logger.info("🔄 Двухфакторный выход из тренда")
                close_all_positions(client, SYMBOL)
                current_positions = get_positions(client, SYMBOL)
                position_open_time = None
        
        if current_positions:
            close_all_positions(client, SYMBOL)
            position_open_time = None
        
        cancel_all_orders(client, SYMBOL)
        current_atr_pct = indicators['atr'] / indicators['price'] * 100
        dynamic_range = max(8.0, min(15.0, current_atr_pct * 1.2))
        
        # 🔥 ИЗМЕНЕНО: Асимметричные границы сетки вне тренда
        last_trend_skew = 0.0
        if current_trend:
            last_trend_skew = 0.5 if current_trend == 'buy' else -0.5
        
        upper_pct = dynamic_range * (1 + last_trend_skew * 0.3)
        lower_pct = dynamic_range * (1 - last_trend_skew * 0.3)
        
        place_grid_orders(client, SYMBOL, INITIAL_CAPITAL, upper_pct=upper_pct, lower_pct=lower_pct)
        grid_center = price

    # Событие ребалансировки
    msg = f"🔄 Ребаланс {datetime.now().strftime('%Y-%m-%d %H:%M')}\nЦена: {price}"
    send_telegram(msg)
    log_to_sheet({
        'timestamp': datetime.now().isoformat(),
        'type': 'rebalance',
        'symbol': SYMBOL,
        'side': current_positions.get('side', ''),
        'size': current_positions.get('size', ''),
        'entry_price': '',
        'exit_price': '',
        'pnl': current_positions.get('unrealizedPnl', 0),
        'total_pnl': total_pnl,
        'message': msg
    })

    # Обновление статистики
    if current_positions != last_positions:
        if last_positions and not current_positions:
            pnl = last_positions.get('unrealizedPnl', 0)
            total_pnl += pnl
            total_trades += 1
            if pnl > 0:
                winning_trades += 1
                
            equity = INITIAL_CAPITAL + total_pnl
            if equity > equity_high:
                equity_high = equity
            drawdown = (equity_high - equity) / equity_high * 100 if equity_high > 0 else 0
            if drawdown > max_drawdown:
                max_drawdown = drawdown
                
            side = last_positions['side']
            size = last_positions['size']
            entry = last_positions['entry']
            msg = f"📲 Выход из сделки\n{SYMBOL} {side.upper()}\nВход: {entry:.1f}\nВыход: ~{price:.1f}\nPnL: {pnl:+.2f} USDT\nИтого: {total_pnl:+.2f} USDT"
            send_telegram(msg)
            log_to_sheet({
                'timestamp': datetime.now().isoformat(),
                'type': 'close_position',
                'symbol': SYMBOL,
                'side': side,
                'size': size,
                'entry_price': entry,
                'exit_price': price,
                'pnl': pnl,
                'total_pnl': total_pnl,
                'message': msg
            })
            
        last_positions = current_positions.copy() if current_positions else {}

    # Ежедневный отчёт
    today = date.today()
    if today != last_report_date:
        win_rate = round(winning_trades / total_trades * 100, 1) if total_trades > 0 else 0.0
        report = (f"📈 ЕЖЕДНЕВНЫЙ ОТЧЁТ\n"
                 f"Дата: {datetime.now().strftime('%d.%m.%Y')}\n"
                 f"Общий PnL: {total_pnl:+.2f} USDT\n"
                 f"Сделок: {total_trades}\n"
                 f"Win Rate: {win_rate}%\n"
                 f"Макс. просадка: {max_drawdown:.2f}%")
        send_telegram(report)
        log_to_sheet({
            'timestamp': datetime.now().isoformat(),
            'type': 'daily_report',
            'symbol': SYMBOL,
            'side': '',
            'size': '',
            'entry_price': '',
            'exit_price': '',
            'pnl': '',
            'total_pnl': total_pnl,
            'message': report
        })
        last_report_date = today

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
    logger.info(f"🚀 Запуск бота | Капитал: {INITIAL_CAPITAL} USDT")
    logger.info(f"📊 Сетка: {GRID_CAPITAL} USDT | Тренд: {TREND_CAPITAL} USDT")
    
    threading.Thread(target=run_flask, daemon=True).start()
    last_rebalance = 0
    while True:
        now = time.time()
        if int(now / 3600) != int(last_rebalance / 3600):
            rebalance_grid()
            last_rebalance = now
        time.sleep(60)