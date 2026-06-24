# strategy/orderblock.py

def detect_orderblock(candles, direction):
    if len(candles) < 5:
        return {
            "detected": False,
            "type": None,
            "zone": None
        }

    last_candles = candles[-5:]

    if direction == "buy":
        for candle in reversed(last_candles):
            if candle["close"] < candle["open"]:
                return {
                    "detected": True,
                    "type": "bullish_orderblock",
                    "zone": {
                        "top": round(candle["open"], 2),
                        "bottom": round(candle["low"], 2)
                    }
                }

    if direction == "sell":
        for candle in reversed(last_candles):
            if candle["close"] > candle["open"]:
                return {
                    "detected": True,
                    "type": "bearish_orderblock",
                    "zone": {
                        "top": round(candle["high"], 2),
                        "bottom": round(candle["open"], 2)
                    }
                }

    return {
        "detected": False,
        "type": None,
        "zone": None
    }
