# strategy/confluence_engine.py

class ConfluenceEngine:

    def __init__(self):
        self.score = 0
        self.reasons = []

    def add(self, condition, points, reason):

        if condition:
            self.score += points
            self.reasons.append(reason)

    def result(self):

        probability = min(
            99,
            round(self.score * 0.95)
        )

        return {

            "score": self.score,

            "probability": probability,

            "can_trade": self.score >= 95,

            "reasons": self.reasons

        }
