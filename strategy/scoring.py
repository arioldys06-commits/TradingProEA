# strategy/scoring.py

MIN_SCORE_TO_TRADE = 95

def calculate_score(setup):
    score = 0
    reasons = []

    if setup.get("trend_aligned"):
        score += 20
        reasons.append("Tendencia alineada")

    if setup.get("liquidity_sweep"):
        score += 20
        reasons.append("Barrido de liquidez")

    if setup.get("fvg_detected"):
        score += 15
        reasons.append("Fair Value Gap detectado")

    if setup.get("orderblock_detected"):
        score += 15
        reasons.append("Order Block detectado")

    if setup.get("strong_confirmation"):
        score += 15
        reasons.append("Vela de confirmación fuerte")

    if setup.get("atr_valid"):
        score += 10
        reasons.append("ATR válido")

    if setup.get("session_valid"):
        score += 5
        reasons.append("Horario válido")

    return {
        "score": score,
        "can_trade": score >= MIN_SCORE_TO_TRADE,
        "reasons": reasons
    }


def signal_quality(score):
    if score >= 95:
        return "ELITE"

    if score >= 85:
        return "GOOD"

    if score >= 70:
        return "WEAK"

    return "NO TRADE"
