# strategy/tradingpro_elite.py

from strategy.indicators import trend_direction, strong_bullish_candle, strong_bearish_candle, atr
from strategy.liquidity import detect_liquidity_sweep
from strategy.fvg import detect_fvg
from strategy.orderblock import detect_orderblock
from strategy.scoring import calculate_score
from strategy.risk import RiskManager


def analyze_tradingpro_elite(candles_m15, candles_m5):
    if len(candles_m15) < 200 or len(candles_m5) < 50:
        return {
            "signal": "NO TRADE",
            "reason": "No hay suficientes velas para analizar."
        }

    trend_m15 = trend_direction(candles_m15)
    trend_m5 = trend_direction(candles_m5)

    if trend_m15 == "neutral" or trend_m5 == "neutral":
        return {
            "signal": "NO TRADE",
            "reason": "Tendencia neutral."
        }

    direction = "buy" if trend_m15 == "bullish" and trend_m5 == "bullish" else "sell"

    liquidity = detect_liquidity_sweep(candles_m5, direction=direction)
    fvg = detect_fvg(candles_m5)
    orderblock = detect_orderblock(candles_m5, direction)

    last_candle = candles_m5[-1]
    current_atr = atr(candles_m5, 14)

    strong_confirmation = (
        strong_bullish_candle(last_candle)
        if direction == "buy"
        else strong_bearish_candle(last_candle)
    )

    setup = {
        "trend_aligned": trend_m15 == trend_m5,
        "liquidity_sweep": liquidity["detected"],
        "fvg_detected": fvg["detected"],
        "orderblock_detected": orderblock["detected"],
        "strong_confirmation": strong_confirmation,
        "atr_valid": current_atr is not None and current_atr > 0,
        "session_valid": True
    }

    score_result = calculate_score(setup)

    if not score_result["can_trade"]:
        return {
            "signal": "NO TRADE",
            "score": score_result["score"],
            "reasons": score_result["reasons"]
        }

    entry = last_candle["close"]
    risk_manager = RiskManager()

    if direction == "buy":
        swing_low = min(c["low"] for c in candles_m5[-10:])
        levels = risk_manager.calculate_buy(entry, swing_low)

        return {
            "signal": "BUY",
            "score": score_result["score"],
            "entry": levels["entry"],
            "stop_loss": levels["stop_loss"],
            "take_profit_1": levels["take_profit_1"],
            "take_profit_2": levels["take_profit_2"],
            "reasons": score_result["reasons"]
        }

    swing_high = max(c["high"] for c in candles_m5[-10:])
    levels = risk_manager.calculate_sell(entry, swing_high)

    return {
        "signal": "SELL",
        "score": score_result["score"],
        "entry": levels["entry"],
        "stop_loss": levels["stop_loss"],
        "take_profit_1": levels["take_profit_1"],
        "take_profit_2": levels["take_profit_2"],
        "reasons": score_result["reasons"]
    }
