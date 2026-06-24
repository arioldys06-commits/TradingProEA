# strategy/tradingpro_ai.py

from strategy.indicators import trend_direction, strong_bullish_candle, strong_bearish_candle, atr
from strategy.liquidity import detect_liquidity_sweep
from strategy.fvg import detect_fvg
from strategy.orderblock import detect_orderblock
from strategy.risk import RiskManager
from strategy.confluence_engine import ConfluenceEngine


def analyze_tradingpro_ai(candles_m15, candles_m5):
    if len(candles_m15) < 200 or len(candles_m5) < 50:
        return None

    trend_m15 = trend_direction(candles_m15)
    trend_m5 = trend_direction(candles_m5)

    if trend_m15 == "neutral" or trend_m5 == "neutral":
        return None

    if trend_m15 != trend_m5:
        return None

    direction = "buy" if trend_m15 == "bullish" else "sell"
    signal_type = "BUY" if direction == "buy" else "SELL"

    liquidity = detect_liquidity_sweep(candles_m5, direction=direction)
    fvg = detect_fvg(candles_m5)
    orderblock = detect_orderblock(candles_m5, direction)

    last_candle = candles_m5[-1]
    current_atr = atr(candles_m5, 14)

    confirmation = (
        strong_bullish_candle(last_candle)
        if direction == "buy"
        else strong_bearish_candle(last_candle)
    )

    engine = ConfluenceEngine()

    engine.add(True, 20, "M15 y M5 alineados")
    engine.add(liquidity["detected"], 20, "Barrido de liquidez")
    engine.add(fvg["detected"], 15, "FVG detectado")
    engine.add(orderblock["detected"], 15, "Order Block detectado")
    engine.add(confirmation, 15, "Vela fuerte de confirmación")
    engine.add(current_atr is not None and current_atr > 0, 10, "ATR válido")
    engine.add(True, 5, "Sesión válida")

    result = engine.result()

    if not result["can_trade"]:
        return None

    entry = last_candle["close"]
    risk = RiskManager()

    if signal_type == "BUY":
        swing_low = min(c["low"] for c in candles_m5[-10:])
        levels = risk.calculate_buy(entry, swing_low)
    else:
        swing_high = max(c["high"] for c in candles_m5[-10:])
        levels = risk.calculate_sell(entry, swing_high)

    return {
        "signal_type": signal_type,
        "entry_price": levels["entry"],
        "stop_loss": levels["stop_loss"],
        "take_profit_1": levels["take_profit_1"],
        "take_profit_2": levels["take_profit_2"],
        "confidence": result["score"],
        "strategy": "TradingPro AI Elite",
        "timeframe": "M5",
        "atr": current_atr,
        "reasons": result["reasons"],
        "probability": result["probability"],
        "candle_time": last_candle.get("time")
    }
