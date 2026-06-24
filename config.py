# config.py

SYMBOL = "XAUUSD"

TIMEFRAME_MAIN = "M15"
TIMEFRAME_ENTRY = "M5"

MIN_SCORE_TO_TRADE = 95

MAX_TRADES_PER_DAY = 3
MAX_LOSSES_PER_DAY = 2

RISK_PER_TRADE_PERCENT = 1.0

USE_NEWS_FILTER = False
USE_SESSION_FILTER = True

ALLOWED_SESSIONS = {
    "london": {
        "start": "03:00",
        "end": "05:30"
    },
    "new_york": {
        "start": "08:30",
        "end": "11:00"
    }
}
