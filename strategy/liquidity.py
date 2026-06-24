# strategy/liquidity.py

def detect_swing_high(candles, lookback=3):
    if len(candles) < lookback * 2 + 1:
        return None

    index = len(candles) - lookback - 1
    current = candles[index]

    left = candles[index - lookback:index]
    right = candles[index + 1:index + lookback + 1]

    if all(current["high"] > c["high"] for c in left + right):
        return current["high"]

    return None


def detect_swing_low(candles, lookback=3):
    if len(candles) < lookback * 2 + 1:
        return None

    index = len(candles) - lookback - 1
    current = candles[index]

    left = candles[index - lookback:index]
    right = candles[index + 1:index + lookback + 1]

    if all(current["low"] < c["low"] for c in left + right):
        return current["low"]

    return None


def detect_liquidity_sweep(candles, direction="both", lookback=20):
    if len(candles) < lookback + 2:
        return {
            "detected": False,
            "type": None,
            "level": None
        }

    recent = candles[-lookback-1:-1]
    current = candles[-1]

    prev_high = max(c["high"] for c in recent)
    prev_low = min(c["low"] for c in recent)

    if direction in ["sell", "both"]:
        if current["high"] > prev_high and current["close"] < prev_high:
            return {
                "detected": True,
                "type": "buy_side_sweep",
                "level": round(prev_high, 2)
            }

    if direction in ["buy", "both"]:
        if current["low"] < prev_low and current["close"] > prev_low:
            return {
                "detected": True,
                "type": "sell_side_sweep",
                "level": round(prev_low, 2)
            }

    return {
        "detected": False,
        "type": None,
        "level": None
    }


def equal_highs(candles, tolerance=0.5, lookback=20):
    if len(candles) < lookback:
        return False

    highs = [c["high"] for c in candles[-lookback:]]

    for i in range(len(highs)):
        for j in range(i + 1, len(highs)):
            if abs(highs[i] - highs[j]) <= tolerance:
                return True

    return False


def equal_lows(candles, tolerance=0.5, lookback=20):
    if len(candles) < lookback:
        return False

    lows = [c["low"] for c in candles[-lookback:]]

    for i in range(len(lows)):
        for j in range(i + 1, len(lows)):
            if abs(lows[i] - lows[j]) <= tolerance:
                return True

    return False
