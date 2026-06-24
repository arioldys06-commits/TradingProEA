# strategy/indicators.py

def ema(values, period):
    if not values or len(values) < period:
        return None

    multiplier = 2 / (period + 1)
    ema_value = sum(values[:period]) / period

    for price in values[period:]:
        ema_value = (price - ema_value) * multiplier + ema_value

    return ema_value


def sma(values, period):
    if not values or len(values) < period:
        return None

    return sum(values[-period:]) / period


def atr(candles, period=14):
    if not candles or len(candles) < period + 1:
        return None

    true_ranges = []

    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )

        true_ranges.append(tr)

    return sum(true_ranges[-period:]) / period


def rsi(values, period=14):
    if not values or len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def highest_high(candles, period=20):
    if not candles or len(candles) < period:
        return None

    return max(candle["high"] for candle in candles[-period:])


def lowest_low(candles, period=20):
    if not candles or len(candles) < period:
        return None

    return min(candle["low"] for candle in candles[-period:])


def is_bullish_candle(candle):
    return candle["close"] > candle["open"]


def is_bearish_candle(candle):
    return candle["close"] < candle["open"]


def candle_body(candle):
    return abs(candle["close"] - candle["open"])


def candle_range(candle):
    return candle["high"] - candle["low"]


def strong_bullish_candle(candle):
    body = candle_body(candle)
    total_range = candle_range(candle)

    if total_range == 0:
        return False

    return is_bullish_candle(candle) and body >= total_range * 0.6


def strong_bearish_candle(candle):
    body = candle_body(candle)
    total_range = candle_range(candle)

    if total_range == 0:
        return False

    return is_bearish_candle(candle) and body >= total_range * 0.6


def trend_direction(candles):
    if not candles or len(candles) < 200:
        return "neutral"

    closes = [c["close"] for c in candles]

    ema50 = ema(closes, 50)
    ema200 = ema(closes, 200)
    price = closes[-1]

    if price > ema50 > ema200:
        return "bullish"

    if price < ema50 < ema200:
        return "bearish"

    return "neutral"
