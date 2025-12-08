# ---------- OKX ----------
API_KEY        = os.getenv("OKX_API_KEY")
API_SECRET     = os.getenv("OKX_SECRET")
API_PASSPHRASE = os.getenv("OKX_PASS")
API_DEMO       = True
SYMBOL         = "BTC-USDT-SWAP"

# ---------- TELEGRAM ----------
TELEGRAM_TOKEN   = os.getenv("TG_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TG_CHAT")

# ---------- GOOGLE SHEETS ----------
GAS_WEBHOOK_URL = os.getenv("GAS_URL")

# ---------- CORE ----------
INITIAL_CAPITAL   = 120.0
GRID_CAPITAL      = 84.0
TREND_CAPITAL     = 36.0
RISK_PER_TRADE    = 0.008
MAX_EQUITY_PCT    = 0.30
MIN_ORDER_SIZE    = 0.01
COOLDOWN_HOURS    = 6
TIMEOUT_DAYS      = 12
TRAIL_ATR_MUL     = 0.75

# ---------- ADAPTIVE ----------
# 2. динамический margin-ratio
VOL_THRESHOLD_MR    = 0.012        # 1.2 %
MR_HIGH_VOL         = 0.55
MR_LOW_VOL          = 0.65

# 3. адаптивный минимальный ATR
MIN_ATR_BASE        = 0.0030
MIN_ATR_RANGE       = 0.0005       # ±0.05 %

# 5. асимметрия
ASYMMETRY_LEVELS    = 5
ASYMMETRY_RANGE     = 30.0         # ±30 %

# 8. walk-forward
OPTIMIZE_SCHEDULE   = "0 0 1 */3 *"   # cron: каждые 3 мес
OPTIMIZE_FILE       = "optimizer.py"

# ---------- DERIVED ----------
REBALANCE_INTERVAL_HOURS = 1