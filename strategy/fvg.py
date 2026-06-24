# strategy/fvg.py

def detect_fvg(candles):
    if len(candles) < 3:
        return {
            "detected": False,
            "type": None,
            "zone": None
        }

    c1 = candles[-3]
    c2 = candles[-2]
    c3 = candles[-1]

    # Bullish FVG
    if c1["high"] < c3["low"]:
        return {
            "detected": True,
            "type": "bullish_fvg",
            "zone": {
                "top": round(c3["low"], 2),
                "bottom": round(c1["high"], 2)
            }
        }

    # Bearish FVG
    if c1["low"] > c3["high"]:
        return {
            "detected": True,
            "type": "bearish_fvg",
            "zone": {
                "top": round(c1["low"], 2),
                "bottom": round(c3["high"], 2)
            }
        }

    return {
        "detected": False,
        "type": None,
        "zone": None
    }


def is_fvg_filled(price, fvg):
    if not fvg or not fvg.get("detected"):
        return False

    zone = fvg.get("zone")

    if not zone:
        return False

    return zone["bottom"] <= price <= zone["top"]
