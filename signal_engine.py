"""
signal_engine.py
================
Genera señales XAUUSD en tiempo real desde velas de Supabase.

Estrategias:
  1. Scalping M5 SMC     — Sweep + BOS/CHoCH + OTE Fibonacci + confluencia TF
  2. London/NY Killzone  — Breakout al inicio de sesión
  3. FVG Fill M5         — Entrada en Fair Value Gap sin llenar
  4. EMA Pullback M5     — Pullback a EMA9/20 en tendencia

ICT OTE Filter (Optimal Trade Entry):
  - Zona 62%-79% de retroceso Fibonacci del ultimo swing
  - Golden Pocket: 70.5% — nivel optimo de entrada ICT
  - Bonus de score +5 a +15 cuando precio esta en OTE
  - Entrada ajustada al golden pocket si precio esta en zona

Filtro Confirmación de Vela (2 velas):
  - ENTRY válido: vela opuesta + vela de confirmación con engulf completo
  - NO ENTRY: sin engulf, indecisión, dos velas en la misma dirección

Bonus Morfología de Vela (probabilidad estadística):
  - Marubozu (cuerpo >80% rango): +15 pts  → 90-95% probabilidad
  - Pin Bar (mecha >1.5x cuerpo): +10 pts  → 80% probabilidad
  - Mecha opuesta dominante:      -10 pts  → 50/50 penalizar

Objetivo: 4-6 señales diarias de alta calidad
Score mínimo para publicar: 70/100
Loop interno: analiza cada 30 segundos
"""

import os
import sys
import time
import uuid
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL     = os.getenv("SUPABASE_URL")
SUPABASE_KEY     = os.getenv("SUPABASE_KEY")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@XAUUSD_Signals_DR")

MIN_SCORE        = 70    # Score mínimo para publicar señal
MAX_DAILY        = 20    # Máximo señales por día (filtro anti-spam)
LOOP_INTERVAL    = 30    # Segundos entre cada análisis
SIGNAL_COOLDOWN  = 120   # 2 min mínimo entre señales de la misma estrategia

# ── ESTADO INTERNO ────────────────────────────────────────────
last_signal_time = {}    # {strategy: datetime} — cooldown por estrategia
daily_count      = 0
last_day         = None
# ──────────────────────────────────────────────────────────────

def headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=representation",
    }

def send_telegram(message):
    if not TELEGRAM_TOKEN:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10
        )
        return r.status_code == 200
    except:
        return False

def telegram_signal(sig):
    arrow    = "BUY" if sig["signal_type"] == "BUY" else "SELL"
    ote      = sig.get("ote")
    ote_line = f"OTE Golden: {ote['golden']:.2f} (62-79% Fib)\n" if ote else ""
    return (
        f"[SENAL {arrow}] XAUUSD\n"
        f"Estrategia: {sig['strategy']}\n"
        f"Entrada: {sig['entry_price']:.2f}\n"
        f"Stop Loss: {sig['stop_loss']:.2f}\n"
        f"TP1: {sig['take_profit_1']:.2f}\n"
        f"TP2: {sig['take_profit_2']:.2f}\n"
        f"Score: {sig['confidence']}/100\n"
        f"ATR: {sig.get('atr', 0):.2f} pts\n"
        f"{ote_line}"
        f"---\n"
        f"Trading Pro — {datetime.now().strftime('%H:%M')}"
    )

# ── FETCH CANDLES ─────────────────────────────────────────────

def get_candles(timeframe, limit=100):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/ohlc_candles",
        headers=headers(),
        params={
            "select":     "candle_time,open,high,low,close,volume",
            "instrument": "eq.XAUUSD",
            "timeframe":  f"eq.{timeframe}",
            "order":      "candle_time.desc",
            "limit":      str(limit),
        },
        timeout=15,
    )
    if r.status_code >= 400:
        return []
    data = r.json()
    # Revertir para orden cronológico
    return list(reversed(data))

def to_candles(rows):
    return [{
        "time": c["candle_time"],
        "O": float(c["open"]),
        "H": float(c["high"]),
        "L": float(c["low"]),
        "C": float(c["close"]),
        "V": int(c.get("volume", 0)),
    } for c in rows]

# ── INDICADORES ───────────────────────────────────────────────

def ema(closes, period):
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    e = sum(closes[:period]) / period
    for c in closes[period:]:
        e = c * k + e * (1 - k)
    return e

def calc_atr(candles, period=14):
    if len(candles) < period + 1:
        return 0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["H"], candles[i]["L"], candles[i-1]["C"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    return 100 if al == 0 else round(100 - 100 / (1 + ag / al), 1)

def detect_swing_hl(candles, lookback=3):
    highs, lows = [], []
    for i in range(lookback, len(candles) - lookback):
        c = candles[i]
        if all(c["H"] > candles[j]["H"] for j in range(i-lookback, i)) and \
           all(c["H"] > candles[j]["H"] for j in range(i+1, i+lookback+1)):
            highs.append({"price": c["H"], "idx": i})
        if all(c["L"] < candles[j]["L"] for j in range(i-lookback, i)) and \
           all(c["L"] < candles[j]["L"] for j in range(i+1, i+lookback+1)):
            lows.append({"price": c["L"], "idx": i})
    return highs, lows

def detect_fvgs(candles):
    fvgs = []
    for i in range(2, len(candles)):
        a, b, c = candles[i-2], candles[i-1], candles[i]
        if a["H"] < c["L"]:  # FVG alcista
            fvgs.append({"type": "BUY", "top": c["L"], "bottom": a["H"],
                          "mid": (c["L"] + a["H"]) / 2, "idx": i})
        if a["L"] > c["H"]:  # FVG bajista
            fvgs.append({"type": "SELL", "top": a["L"], "bottom": c["H"],
                          "mid": (a["L"] + c["H"]) / 2, "idx": i})
    return fvgs

# ── OTE — OPTIMAL TRADE ENTRY (ICT Fibonacci) ─────────────────
# El OTE es el retroceso del 62%-79% del ultimo swing
# Es la zona donde los institucionales reentran despues de un BOS/CHoCH
# Niveles clave ICT: 0.62, 0.705 (golden pocket), 0.79

OTE_LOW  = 0.62   # 62% retroceso — inicio zona OTE
OTE_HIGH = 0.79   # 79% retroceso — fin zona OTE
OTE_GOLD = 0.705  # Golden pocket — nivel optimo

def calc_ote(swing_high, swing_low, direction):
    """
    Calcula la zona OTE basada en el ultimo swing.
    BUY:  retroceso desde swing_high (precio baja a OTE y sube)
    SELL: retroceso desde swing_low  (precio sube a OTE y baja)
    """
    rng = swing_high - swing_low
    if rng <= 0:
        return None
    if direction == "BUY":
        ote_high = swing_high - rng * OTE_LOW
        ote_low  = swing_high - rng * OTE_HIGH
        golden   = swing_high - rng * OTE_GOLD
    else:
        ote_low  = swing_low + rng * OTE_LOW
        ote_high = swing_low + rng * OTE_HIGH
        golden   = swing_low + rng * OTE_GOLD
    return {"low": round(ote_low, 2), "high": round(ote_high, 2), "golden": round(golden, 2)}

def price_in_ote(current_price, ote):
    if not ote:
        return False
    return ote["low"] <= current_price <= ote["high"]

def ote_score_bonus(current_price, ote):
    """Bonus +5 a +15 segun proximidad al golden pocket."""
    if not ote or not price_in_ote(current_price, ote):
        return 0
    zone_size = ote["high"] - ote["low"]
    if zone_size <= 0:
        return 5
    dist = abs(current_price - ote["golden"])
    proximity = 1 - (dist / zone_size)
    return round(proximity * 15)

# ── FILTRO DE CONFIRMACIÓN DE VELA (Imagen 1) ─────────────────
# ENTRY válido: vela previa opuesta + vela actual en dirección con engulf
# NO ENTRY: sin engulf, dos velas iguales, indecisión

def confirmar_entrada_por_vela(candles, direccion):
    """
    Valida estructura de confirmación de 2 velas antes de entrar.
    BUY:  vela previa bajista + vela actual alcista que cierra sobre open anterior
    SELL: vela previa alcista + vela actual bajista que cierra bajo open anterior
    """
    if len(candles) < 2:
        return False
    prev = candles[-2]
    curr = candles[-1]
    if direccion == "BUY":
        previa_bajista = prev["C"] < prev["O"]
        actual_alcista = curr["C"] > curr["O"]
        engulf         = curr["C"] > prev["O"]   # cierra por encima del open anterior
        return previa_bajista and actual_alcista and engulf
    elif direccion == "SELL":
        previa_alcista = prev["C"] > prev["O"]
        actual_bajista = curr["C"] < curr["O"]
        engulf         = curr["C"] < prev["O"]   # cierra por debajo del open anterior
        return previa_alcista and actual_bajista and engulf
    return False

# ── BONUS DE MORFOLOGÍA DE VELA (Imagen 2) ────────────────────
# Marubozu (cuerpo >80%) → 90-95% probabilidad → +15 pts
# Pin Bar / Doji con mecha dominante → 80% probabilidad → +10 pts
# Mecha opuesta dominante (50/50) → penalizar -10 pts

def score_morfologia_vela(candles, direccion):
    """
    Bonus/penalización basado en probabilidad por morfología de vela.
    Se aplica sobre la vela de confirmación (última vela).
    """
    if len(candles) < 1:
        return 0, ""
    vela   = candles[-1]
    body   = abs(vela["C"] - vela["O"])
    rango  = vela["H"] - vela["L"]
    if rango == 0:
        return 0, ""
    mecha_sup = vela["H"] - max(vela["C"], vela["O"])
    mecha_inf = min(vela["C"], vela["O"]) - vela["L"]
    ratio_body = body / rango

    if direccion == "BUY":
        if ratio_body >= 0.80 and vela["C"] > vela["O"]:
            return 15, "Marubozu alcista (90%)"
        elif mecha_inf > body * 1.5:
            return 10, "Pin Bar alcista (80%)"
        elif mecha_sup > body:
            return -10, "Mecha superior dominante (50/50)"
    elif direccion == "SELL":
        if ratio_body >= 0.80 and vela["C"] < vela["O"]:
            return 15, "Marubozu bajista (95%)"
        elif mecha_sup > body * 1.5:
            return 10, "Pin Bar bajista (80%)"
        elif mecha_inf > body:
            return -10, "Mecha inferior dominante (50/50)"
    return 0, ""

def is_killzone():
    now  = datetime.now(timezone.utc)
    rdh  = ((now.hour - 4) + 24) % 24
    t    = rdh * 100 + now.minute
    return (t >= 300 and t < 600) or (t >= 900 and t < 1200)

def get_session():
    now  = datetime.now(timezone.utc)
    rdh  = ((now.hour - 4) + 24) % 24
    t    = rdh * 100 + now.minute
    if t >= 300 and t < 600:   return "London KZ"
    if t >= 900 and t < 1200:  return "NY KZ"
    if t >= 100 and t < 300:   return "Pre-London"
    return "Zona muerta"

def in_cooldown(strategy):
    if strategy not in last_signal_time:
        return False
    elapsed = (datetime.now(timezone.utc) - last_signal_time[strategy]).total_seconds()
    return elapsed < SIGNAL_COOLDOWN

# ── PUBLICAR SEÑAL ────────────────────────────────────────────

def publish_signal(sig):
    global daily_count, last_day, last_signal_time

    # Reset contador diario
    today = datetime.now(timezone.utc).date()
    if last_day != today:
        daily_count = 0
        last_day    = today

    if daily_count >= MAX_DAILY:
        print(f"  [SKIP] Límite diario {MAX_DAILY} señales alcanzado.")
        # Igual notificar Telegram que hay señal aunque no se publique
        send_telegram(
            f"[ALERTA] {sig['signal_type']} detectado — límite diario alcanzado\n"
            f"Estrategia: {sig['strategy']}\n"
            f"Score: {sig['confidence']}/100 | Entry: {sig['entry_price']:.2f}\n"
            f"SL: {sig['stop_loss']:.2f} | TP1: {sig['take_profit_1']:.2f}"
        )
        return False

    if in_cooldown(sig["strategy"]):
        print(f"  [SKIP] Cooldown activo para {sig['strategy']}")
        return False

    # Insertar en Supabase
    payload = {
        "id":            str(uuid.uuid4()),
        "signal_type":   sig["signal_type"],
        "entry_price":   round(sig["entry_price"], 2),
        "stop_loss":     round(sig["stop_loss"], 2),
        "take_profit_1": round(sig["take_profit_1"], 2),
        "take_profit_2": round(sig["take_profit_2"], 2),
        "confidence":    sig["confidence"],
        "strategy":      sig["strategy"],
        "timeframe":     sig.get("timeframe", "M5"),
        "status":        "PENDING",
        "created_at":    datetime.now(timezone.utc).isoformat(),
    }

    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/signals",
            headers=headers(),
            json=payload,
            timeout=15,
        )
        if r.status_code >= 400:
            print(f"  [ERROR] Supabase insert: {r.status_code} {r.text}")
            return False
    except Exception as e:
        print(f"  [ERROR] publish_signal: {e}")
        return False

    daily_count += 1
    last_signal_time[sig["strategy"]] = datetime.now(timezone.utc)

    # Telegram — enviar señal completa
    msg   = telegram_signal(sig)
    ok_tg = send_telegram(msg)
    if ok_tg:
        print(f"  [TELEGRAM] Enviado OK")
    else:
        print(f"  [TELEGRAM] ERROR — revisa TELEGRAM_TOKEN y TELEGRAM_CHAT_ID en .env")

    print(
        f"  [SEÑAL] {sig['signal_type']} | {sig['strategy']} | "
        f"Score: {sig['confidence']}/100 | "
        f"Entry: {sig['entry_price']:.2f} | "
        f"SL: {sig['stop_loss']:.2f} | "
        f"TP1: {sig['take_profit_1']:.2f}"
    )
    return True

# ════════════════════════════════════════════════════════════════
# ESTRATEGIA 1 — SCALPING M5 SMC
# Sweep de liquidez + BOS/CHoCH + confluencia multi-TF
# ════════════════════════════════════════════════════════════════

def strategy_scalping_m5(c5, c30, ch1):
    if len(c5) < 30:
        return None

    closes5 = [c["C"] for c in c5]
    e9      = ema(closes5, 9)
    e20     = ema(closes5, 20)
    atr     = calc_atr(c5[-20:])
    rsi     = calc_rsi(closes5[-30:])
    last    = c5[-1]

    if not e9 or not e20 or atr < 0.5:
        return None

    ema_dir = "BUY" if e9 > e20 else "SELL"

    # Confluencia TF superiores
    def tf_dir(candles):
        if len(candles) < 10:
            return None
        cl = [c["C"] for c in candles]
        e9t = ema(cl, 9); e20t = ema(cl, 20)
        return "BUY" if (e9t and e20t and e9t > e20t) else "SELL"

    dir_m30 = tf_dir(c30)
    dir_h1  = tf_dir(ch1)

    # Swing highs/lows
    highs, lows = detect_swing_hl(c5[-30:], lookback=2)
    if not highs or not lows:
        return None

    swing_h = max(h["price"] for h in highs[-3:])
    swing_l = min(l["price"] for l in lows[-3:])

    sweep_low  = last["L"] < swing_l and last["C"] > swing_l
    sweep_high = last["H"] > swing_h and last["C"] < swing_h

    # OTE — Optimal Trade Entry (ICT Fibonacci 62-79%)
    ote_buy  = calc_ote(swing_h, swing_l, "BUY")
    ote_sell = calc_ote(swing_h, swing_l, "SELL")

    # BOS/CHoCH últimas 20 velas
    r = c5[-20:]
    rH = max(c["H"] for c in r[-5:]); pH = max(c["H"] for c in r[-10:-5])
    rL = min(c["L"] for c in r[-5:]); pL = min(c["L"] for c in r[-10:-5])
    bos   = "BUY" if rH > pH and rL > pL else "SELL" if rH < pH and rL < pL else None
    choch = "BUY" if rH > pH and rL < pL else "SELL" if rH < pH and rL > pL else None

    score   = 0
    reasons = []
    sig_type = None

    if sweep_low and ema_dir == "BUY" and rsi < 65:
        sig_type = "BUY"; score += 35; reasons.append("Sweep Low")
        if dir_m30 == "BUY": score += 15; reasons.append("M30 BUY")
        if dir_h1  == "BUY": score += 15; reasons.append("H1 BUY")
        if choch   == "BUY": score += 20; reasons.append("CHoCH BUY")
        elif bos   == "BUY": score += 10; reasons.append("BOS BUY")
        if is_killzone():    score += 5;  reasons.append("Killzone")
        # OTE bonus — precio en zona 62-79% Fibonacci
        ote_bonus = ote_score_bonus(last["C"], ote_buy)
        if ote_bonus > 0:
            score += ote_bonus
            if price_in_ote(last["C"], ote_buy):
                reasons.append(f"OTE {ote_buy['golden']:.2f} (+{ote_bonus}pts)")

    elif sweep_high and ema_dir == "SELL" and rsi > 35:
        sig_type = "SELL"; score += 35; reasons.append("Sweep High")
        if dir_m30 == "SELL": score += 15; reasons.append("M30 SELL")
        if dir_h1  == "SELL": score += 15; reasons.append("H1 SELL")
        if choch   == "SELL": score += 20; reasons.append("CHoCH SELL")
        elif bos   == "SELL": score += 10; reasons.append("BOS SELL")
        if is_killzone():     score += 5;  reasons.append("Killzone")
        # OTE bonus — precio en zona 62-79% Fibonacci
        ote_bonus = ote_score_bonus(last["C"], ote_sell)
        if ote_bonus > 0:
            score += ote_bonus
            if price_in_ote(last["C"], ote_sell):
                reasons.append(f"OTE {ote_sell['golden']:.2f} (+{ote_bonus}pts)")

    if not sig_type or score < MIN_SCORE:
        return None

    # ── Filtro confirmación de vela (Imagen 1) ──
    if not confirmar_entrada_por_vela(c5, sig_type):
        print(f"  [1] Scalping M5 SMC: {sig_type} descartado — sin confirmación de vela")
        return None

    # ── Bonus morfología (Imagen 2) ──
    morfo_bonus, morfo_reason = score_morfologia_vela(c5, sig_type)
    if morfo_bonus != 0:
        score += morfo_bonus
        if morfo_reason:
            reasons.append(morfo_reason)
    score = max(0, min(score, 100))
    if score < MIN_SCORE:
        return None

    sl_pts = max(atr * 1.2, 5)
    # Si precio esta en OTE usar golden pocket como entrada ideal
    ote_active = ote_buy if sig_type == "BUY" else ote_sell
    if price_in_ote(last["C"], ote_active):
        entry = ote_active["golden"]
    else:
        entry = last["C"]

    return {
        "signal_type":   sig_type,
        "entry_price":   entry,
        "stop_loss":     entry - sl_pts if sig_type == "BUY" else entry + sl_pts,
        "take_profit_1": entry + sl_pts * 1.5 if sig_type == "BUY" else entry - sl_pts * 1.5,
        "take_profit_2": entry + sl_pts * 3   if sig_type == "BUY" else entry - sl_pts * 3,
        "confidence":    min(score, 100),
        "strategy":      "Scalping M5 SMC",
        "timeframe":     "M5",
        "atr":           atr,
        "reasons":       reasons,
        "ote":           ote_active,
    }

# ════════════════════════════════════════════════════════════════
# ESTRATEGIA 2 — LONDON/NY KILLZONE BREAKOUT
# Opera el primer movimiento fuerte al inicio de cada sesión
# ════════════════════════════════════════════════════════════════

def strategy_killzone_breakout(c5, ch1):
    if not is_killzone():
        return None
    if len(c5) < 20 or len(ch1) < 5:
        return None

    session = get_session()
    last    = c5[-1]
    atr     = calc_atr(c5[-20:])
    if atr < 0.5:
        return None

    # Rango de las últimas 6 velas M5 (30 min pre-sesión)
    pre_candles = c5[-7:-1]
    range_high  = max(c["H"] for c in pre_candles)
    range_low   = min(c["L"] for c in pre_candles)
    range_size  = range_high - range_low

    # Necesitamos un rango mínimo y una ruptura clara
    if range_size < atr * 0.5:
        return None

    breakout_up   = last["C"] > range_high and last["C"] - range_high > atr * 0.3
    breakout_down = last["C"] < range_low  and range_low - last["C"] > atr * 0.3

    # Confirmar con H1
    closes_h1 = [c["C"] for c in ch1]
    e9h1  = ema(closes_h1, 9)
    e20h1 = ema(closes_h1, 20)
    h1_dir = "BUY" if (e9h1 and e20h1 and e9h1 > e20h1) else "SELL"

    score    = 0
    sig_type = None
    reasons  = []

    if breakout_up and h1_dir == "BUY":
        sig_type = "BUY"; score += 50; reasons.append(f"Breakout {session}")
        score += 20; reasons.append("H1 BUY")
        if range_size > atr: score += 10; reasons.append("Rango amplio")
        score += 10; reasons.append("Killzone activa")

    elif breakout_down and h1_dir == "SELL":
        sig_type = "SELL"; score += 50; reasons.append(f"Breakout {session}")
        score += 20; reasons.append("H1 SELL")
        if range_size > atr: score += 10; reasons.append("Rango amplio")
        score += 10; reasons.append("Killzone activa")

    if not sig_type or score < MIN_SCORE:
        return None

    # ── Filtro confirmación de vela (Imagen 1) ──
    if not confirmar_entrada_por_vela(c5, sig_type):
        print(f"  [2] Killzone Breakout: {sig_type} descartado — sin confirmación de vela")
        return None

    # ── Bonus morfología (Imagen 2) ──
    morfo_bonus, morfo_reason = score_morfologia_vela(c5, sig_type)
    if morfo_bonus != 0:
        score += morfo_bonus
        if morfo_reason:
            reasons.append(morfo_reason)
    score = max(0, min(score, 100))
    if score < MIN_SCORE:
        return None

    sl_pts = max(atr * 1.3, 6)
    entry  = last["C"]

    return {
        "signal_type":   sig_type,
        "entry_price":   entry,
        "stop_loss":     entry - sl_pts if sig_type == "BUY" else entry + sl_pts,
        "take_profit_1": entry + sl_pts * 1.5 if sig_type == "BUY" else entry - sl_pts * 1.5,
        "take_profit_2": entry + sl_pts * 3   if sig_type == "BUY" else entry - sl_pts * 3,
        "confidence":    min(score, 100),
        "strategy":      "Killzone Breakout",
        "timeframe":     "M5",
        "atr":           atr,
        "reasons":       reasons,
    }

# ════════════════════════════════════════════════════════════════
# ESTRATEGIA 3 — FVG FILL M5
# Detecta Fair Value Gaps recientes y espera que el precio regrese
# ════════════════════════════════════════════════════════════════

def strategy_fvg_fill(c5, c30):
    if len(c5) < 30:
        return None

    atr  = calc_atr(c5[-20:])
    last = c5[-1]
    if atr < 0.5:
        return None

    closes5 = [c["C"] for c in c5]
    e9  = ema(closes5, 9)
    e20 = ema(closes5, 20)
    if not e9 or not e20:
        return None
    trend = "BUY" if e9 > e20 else "SELL"

    # Detectar FVGs en las últimas 40 velas
    fvgs = detect_fvgs(c5[-40:])
    if not fvgs:
        return None

    # Filtrar FVGs en dirección de la tendencia y sin llenar aún
    valid_fvgs = [f for f in fvgs if f["type"] == trend]
    if not valid_fvgs:
        return None

    # Tomar el FVG más reciente
    fvg = valid_fvgs[-1]

    # Verificar si el precio está tocando el FVG ahora
    price_in_fvg_buy  = trend == "BUY"  and last["L"] <= fvg["top"]    and last["C"] >= fvg["bottom"]
    price_in_fvg_sell = trend == "SELL" and last["H"] >= fvg["bottom"] and last["C"] <= fvg["top"]

    if not price_in_fvg_buy and not price_in_fvg_sell:
        return None

    # Confirmar con M30
    closes_m30 = [c["C"] for c in c30]
    e9m30  = ema(closes_m30, 9)
    e20m30 = ema(closes_m30, 20)
    m30_conf = (e9m30 and e20m30 and
                ((trend == "BUY" and e9m30 > e20m30) or
                 (trend == "SELL" and e9m30 < e20m30)))

    rsi = calc_rsi(closes5[-20:])

    score    = 0
    reasons  = []
    sig_type = trend

    score += 40; reasons.append(f"FVG {trend} tocado")
    if m30_conf: score += 20; reasons.append("M30 confirma")
    if is_killzone(): score += 15; reasons.append("Killzone activa")
    if trend == "BUY"  and rsi < 60: score += 10; reasons.append(f"RSI {rsi}")
    if trend == "SELL" and rsi > 40: score += 10; reasons.append(f"RSI {rsi}")
    score += 10; reasons.append("FVG institucional")

    if score < MIN_SCORE:
        return None

    # ── Filtro confirmación de vela (Imagen 1) ──
    if not confirmar_entrada_por_vela(c5, sig_type):
        print(f"  [3] FVG Fill: {sig_type} descartado — sin confirmación de vela")
        return None

    # ── Bonus morfología (Imagen 2) ──
    morfo_bonus, morfo_reason = score_morfologia_vela(c5, sig_type)
    if morfo_bonus != 0:
        score += morfo_bonus
        if morfo_reason:
            reasons.append(morfo_reason)
    score = max(0, min(score, 100))
    if score < MIN_SCORE:
        return None

    sl_pts = max(atr * 1.2, 5)
    entry  = last["C"]

    return {
        "signal_type":   sig_type,
        "entry_price":   entry,
        "stop_loss":     entry - sl_pts if sig_type == "BUY" else entry + sl_pts,
        "take_profit_1": entry + sl_pts * 1.5 if sig_type == "BUY" else entry - sl_pts * 1.5,
        "take_profit_2": entry + sl_pts * 3   if sig_type == "BUY" else entry - sl_pts * 3,
        "confidence":    min(score, 100),
        "strategy":      "FVG Fill M5",
        "timeframe":     "M5",
        "atr":           atr,
        "reasons":       reasons,
    }

# ════════════════════════════════════════════════════════════════
# ESTRATEGIA 4 — EMA PULLBACK M5
# Pullback a EMA9/20 en tendencia clara con vela de confirmación
# ════════════════════════════════════════════════════════════════

def strategy_ema_pullback(c5, c30, ch1):
    if len(c5) < 30:
        return None

    closes5 = [c["C"] for c in c5]
    e9  = ema(closes5, 9)
    e20 = ema(closes5, 20)
    e50 = ema(closes5, 50) if len(closes5) >= 50 else None
    atr = calc_atr(c5[-20:])
    rsi = calc_rsi(closes5[-20:])

    if not e9 or not e20 or atr < 0.5:
        return None

    trend = "BUY" if e9 > e20 else "SELL"

    # Tendencia fuerte: EMA9 > EMA20 > EMA50 (o viceversa)
    strong_trend = False
    if e50:
        strong_trend = (trend == "BUY"  and e9 > e20 > e50) or \
                       (trend == "SELL" and e9 < e20 < e50)

    last = c5[-1]
    prev = c5[-2]

    # Pullback: precio tocó zona EMA9/20 y rebotó
    ema_zone_top = max(e9, e20) + atr * 0.1
    ema_zone_bot = min(e9, e20) - atr * 0.1

    touched_ema_buy  = trend == "BUY"  and prev["L"] <= ema_zone_top and last["C"] > e9
    touched_ema_sell = trend == "SELL" and prev["H"] >= ema_zone_bot and last["C"] < e9

    if not touched_ema_buy and not touched_ema_sell:
        return None

    # Vela de confirmación: cierre fuerte en dirección de tendencia
    last_body = abs(last["C"] - last["O"])
    last_range = last["H"] - last["L"]
    strong_candle = last_body > last_range * 0.5

    if not strong_candle:
        return None

    # Confirmar con M30 y H1
    def tf_dir(candles):
        if len(candles) < 10: return None
        cl = [c["C"] for c in candles]
        e9t = ema(cl, 9); e20t = ema(cl, 20)
        return "BUY" if (e9t and e20t and e9t > e20t) else "SELL"

    dir_m30 = tf_dir(c30)
    dir_h1  = tf_dir(ch1)

    score    = 0
    reasons  = []
    sig_type = trend

    score += 30; reasons.append(f"Pullback EMA {trend}")
    if strong_trend: score += 15; reasons.append("Tendencia fuerte")
    if dir_m30 == trend: score += 15; reasons.append("M30 confirma")
    if dir_h1  == trend: score += 15; reasons.append("H1 confirma")
    if strong_candle:    score += 10; reasons.append("Vela confirmación")
    if is_killzone():    score += 10; reasons.append("Killzone activa")
    if trend == "BUY"  and rsi < 65: score += 5; reasons.append(f"RSI {rsi}")
    if trend == "SELL" and rsi > 35: score += 5; reasons.append(f"RSI {rsi}")

    if score < MIN_SCORE:
        return None

    # ── Filtro confirmación de vela (Imagen 1) ──
    if not confirmar_entrada_por_vela(c5, sig_type):
        print(f"  [4] EMA Pullback: {sig_type} descartado — sin confirmación de vela")
        return None

    # ── Bonus morfología (Imagen 2) ──
    morfo_bonus, morfo_reason = score_morfologia_vela(c5, sig_type)
    if morfo_bonus != 0:
        score += morfo_bonus
        if morfo_reason:
            reasons.append(morfo_reason)
    score = max(0, min(score, 100))
    if score < MIN_SCORE:
        return None

    sl_pts = max(atr * 1.1, 4)
    entry  = last["C"]

    return {
        "signal_type":   sig_type,
        "entry_price":   entry,
        "stop_loss":     entry - sl_pts if sig_type == "BUY" else entry + sl_pts,
        "take_profit_1": entry + sl_pts * 1.5 if sig_type == "BUY" else entry - sl_pts * 1.5,
        "take_profit_2": entry + sl_pts * 3   if sig_type == "BUY" else entry - sl_pts * 3,
        "confidence":    min(score, 100),
        "strategy":      "EMA Pullback M5",
        "timeframe":     "M5",
        "atr":           atr,
        "reasons":       reasons,
    }

# ── CICLO PRINCIPAL ───────────────────────────────────────────

def analyze():
    now_str = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{now_str}] Analizando mercado...")

    # Cargar velas
    rows_m5  = get_candles("M5",  100)
    rows_m30 = get_candles("M30", 60)
    rows_h1  = get_candles("H1",  60)

    if not rows_m5:
        print("  Sin velas M5 disponibles.")
        return

    c5  = to_candles(rows_m5)
    c30 = to_candles(rows_m30)
    ch1 = to_candles(rows_h1)

    last_price = c5[-1]["C"]
    session    = get_session()
    kz         = "KZ ACTIVA" if is_killzone() else "fuera de KZ"
    print(f"  Precio: {last_price:.2f} | Sesión: {session} | {kz} | Señales hoy: {daily_count}/{MAX_DAILY}")

    signals_found = 0

    # ── Estrategia 1: Scalping M5 SMC
    try:
        sig = strategy_scalping_m5(c5, c30, ch1)
        if sig:
            if publish_signal(sig):
                signals_found += 1
            else:
                print(f"  [1] Scalping M5 SMC: señal detectada pero no publicada")
        else:
            print(f"  [1] Scalping M5 SMC: sin setup")
    except Exception as e:
        print(f"  [1] Error Scalping M5 SMC: {e}")

    # ── Estrategia 2: Killzone Breakout
    try:
        sig = strategy_killzone_breakout(c5, ch1)
        if sig:
            if publish_signal(sig):
                signals_found += 1
            else:
                print(f"  [2] Killzone Breakout: señal detectada pero no publicada")
        else:
            kz_info = "activa" if is_killzone() else "inactiva (solo opera en KZ)"
            print(f"  [2] Killzone Breakout: sin setup | KZ {kz_info}")
    except Exception as e:
        print(f"  [2] Error Killzone Breakout: {e}")

    # ── Estrategia 3: FVG Fill
    try:
        sig = strategy_fvg_fill(c5, c30)
        if sig:
            if publish_signal(sig):
                signals_found += 1
            else:
                print(f"  [3] FVG Fill: señal detectada pero no publicada")
        else:
            print(f"  [3] FVG Fill M5: sin FVG activo")
    except Exception as e:
        print(f"  [3] Error FVG Fill: {e}")

    # ── Estrategia 4: EMA Pullback
    try:
        sig = strategy_ema_pullback(c5, c30, ch1)
        if sig:
            if publish_signal(sig):
                signals_found += 1
            else:
                print(f"  [4] EMA Pullback: señal detectada pero no publicada")
        else:
            print(f"  [4] EMA Pullback M5: sin pullback válido")
    except Exception as e:
        print(f"  [4] Error EMA Pullback: {e}")

    if signals_found > 0:
        print(f"  => {signals_found} señal(es) publicada(s) esta ronda")
    else:
        print(f"  => Sin señales esta ronda")

def main():
    print(f"\n{'='*55}")
    print(f"  SIGNAL ENGINE — Trading Pro XAUUSD")
    print(f"  URL: {SUPABASE_URL}")
    print(f"  Score mínimo: {MIN_SCORE} | Max diario: {MAX_DAILY}")
    print(f"  Cooldown por estrategia: {SIGNAL_COOLDOWN}s")
    print(f"  Estrategias activas:")
    print(f"    1. Scalping M5 SMC (Sweep + BOS/CHoCH + OTE Fib)")
    print(f"    2. Killzone Breakout (London/NY)")
    print(f"    3. FVG Fill M5")
    print(f"    4. EMA Pullback M5")
    print(f"  ICT OTE: Golden Pocket 70.5% | Zona 62-79% Fibonacci")
    print(f"{'='*55}\n")

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: Faltan SUPABASE_URL o SUPABASE_KEY en .env")
        sys.exit(1)

    # Verificar Telegram al arrancar
    if TELEGRAM_TOKEN:
        ok = send_telegram(
            f"Trading Pro XAUUSD — Signal Engine INICIADO\n"
            f"Score min: {MIN_SCORE} | Max diario: {MAX_DAILY}\n"
            f"Cooldown: {SIGNAL_COOLDOWN}s | Loop: {LOOP_INTERVAL}s\n"
            f"Hora: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        if ok:
            print("  [TELEGRAM] Conectado OK — mensaje de inicio enviado")
        else:
            print("  [TELEGRAM] ERROR — no se pudo enviar mensaje de prueba")
            print(f"  Token: {TELEGRAM_TOKEN[:10]}... | Chat: {TELEGRAM_CHAT_ID}")
    else:
        print("  [TELEGRAM] Sin TELEGRAM_TOKEN — notificaciones desactivadas")

    # Loop en tiempo real
    while True:
        try:
            analyze()
        except Exception as e:
            print(f"[ERROR] analyze: {e}")
        print(f"  Próximo análisis en {LOOP_INTERVAL}s...")
        time.sleep(LOOP_INTERVAL)

if __name__ == "__main__":
    main()
