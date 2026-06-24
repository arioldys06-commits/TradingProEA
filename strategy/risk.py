# strategy/risk.py

class RiskManager:

    def __init__(self):
        self.minimum_rr = 2.0

    def calculate_buy(self, entry, swing_low):

        risk = entry - swing_low

        tp1 = entry + (risk * 2)

        tp2 = entry + (risk * 3)

        return {
            "entry": round(entry,2),
            "stop_loss": round(swing_low,2),
            "risk": round(risk,2),
            "take_profit_1": round(tp1,2),
            "take_profit_2": round(tp2,2),
            "rr":2
        }

    def calculate_sell(self, entry, swing_high):

        risk = swing_high - entry

        tp1 = entry - (risk * 2)

        tp2 = entry - (risk * 3)

        return {
            "entry": round(entry,2),
            "stop_loss": round(swing_high,2),
            "risk": round(risk,2),
            "take_profit_1": round(tp1,2),
            "take_profit_2": round(tp2,2),
            "rr":2
        }
