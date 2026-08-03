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

Filtro de correlación con el dolar (DXY sintetico):
  - Este broker no expone un simbolo nativo de indice del dolar (DXY),
    asi que se construye uno sintetico a partir de 4 pares que si estan
    disponibles: EURUSD, GBPUSD, USDCHF, USDJPY.
  - EURUSD/GBPUSD: USD es la moneda cotizada -> si el par SUBE, USD se debilita.
  - USDCHF/USDJPY: USD es la moneda base    -> si el par SUBE, USD se fortalece.
  - Se calcula tendencia EMA9/20 en M30 para cada par y se combinan en un
    "voto" (0-4). 3-4 votos a favor del USD fuerte = "BUY" (dolar fuerte),
    0-1 votos = "SELL" (dolar debil), 2 = "NEUTRAL" (sin consenso).
  - Si la señal de XAUUSD esta a favor de la correlacion clasica
    (oro SELL + dolar fuerte, u oro BUY + dolar debil) -> bono +10.
  - Si la contradice -> penalizacion -10.
  - Si el dolar esta en NEUTRAL -> no afecta el score.
  - Esto es un bono/penalizacion adicional, no un filtro duro: una señal
    puede seguir publicandose aunque el dolar no confirme, si el resto
    de las condiciones ya le dan score suficiente.

Filtro Anti-Trampa Institucional del FVG — NUEVO:
  - Un FVG "trampa" es un gap que parece institucional pero en realidad
    es ruido o manipulacion diseñada para atrapar retail.
  - Se valida con 3 criterios sobre la estrategia FVG Fill M5:
      1. Sweep de liquidez previo: ¿hubo un barrido de un swing high/low
         justo antes de que se formara el FVG? (+15 si si, -10 si no)
      2. Momentum de la vela de impulso: cuerpo dominante (>=70% del
         rango) = flujo institucional real (+20). Cuerpo debil (<40%)
         = sospechoso (-15). Tambien penaliza FVGs desproporcionados
         vs el ATR (>2.5x ATR = posible spike de noticia, -15).
      3. Reaccion al retest: si el precio, en las velas posteriores a
         formarse el FVG, lo atraviesa por completo sin reaccionar,
         la señal se descarta directamente (no solo resta puntos).

Validador de Vigencia (filtro anti-manipulación):
  - Compara precio actual vs entrada original antes de publicar
  - Si el precio se alejó más de 0.5x ATR → señal expirada, descarta
  - Los ~90s de latencia actúan como filtro natural de fakeouts/sweeps
  - Timestamp en Telegram muestra vela_time vs publish_time para medir retraso

Killzone obligatoria:
  - Estrategias 1, 3 y 4 solo operan dentro de London/NY KZ
  - Estrategia 2 ya requiere KZ por diseño

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

# TradingPro AI Elite
# Import protegido para que el motor principal no se caiga si el módulo AI tiene error.
try:
    from strategy.tradingpro_ai import analyze_tradingpro_ai
    AI_ENGINE_AVAILABLE = True
    AI_ENGINE_IMPORT_ERROR = None
except Exception as e:
    analyze_tradingpro_ai = None
    AI_ENGINE_AVAILABLE = False
    AI_ENGINE_IMPORT_ERROR = e

load_dotenv()

SUPABASE_URL     = os.getenv("SUPABASE_URL")
SUPABASE_KEY     = os.getenv("SUPABASE_KEY")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@XAUUSD_Signals_DR")

MIN_SCORE        = 75    # Score mínimo para publicar señal (igualado a bot_engine.py MIN_SCORE=75)
MAX_DAILY        = 20    # Máximo señales por día (filtro anti-spam)
LOOP_INTERVAL    = 30    # Segundos entre cada análisis
SIGNAL_COOLDOWN  = 120   # 2 min mínimo entre señales de la misma estrategia

# Pares usados para construir el indice sintetico de fuerza del dolar.
# Deben coincidir con los que data_engine.py sube a ohlc_candles.
DOLLAR_PAIRS = ["EURUSD", "GBPUSD", "USDCHF", "USDJPY"]
# EURUSD y GBPUSD son inversos al USD (par sube = USD se debilita).
# USDCHF y USDJPY son directos al USD (par sube = USD se fortalece).
DOLLAR_PAIRS_INVERSOS = {"EURUSD", "GBPUSD"}
DXY_SCORE_BONUS = 10  # puntos que suma/resta la confirmacion/contradiccion del dolar

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
    # Timestamps para medir latencia real
    vela_time    = sig.get("candle_time", "N/A")
    publish_time = datetime.now().strftime("%H:%M:%S")
    latencia     = ""
    if vela_time != "N/A":
        try:
            vt_raw = datetime.fromisoformat(vela_time.replace("Z", "+00:00"))
            # candle_time ya viene en UTC real: data_engine.py ya hizo la
            # correccion EET->UTC antes de guardarlo en Supabase. Restar
            # el offset otra vez aqui duplicaba la correccion y producia
            # un "Delay" inflado en ~1h que no reflejaba la demora real.
            vt_utc = vt_raw if vt_raw.tzinfo else vt_raw.replace(tzinfo=timezone.utc)
            vt_rd  = vt_utc - timedelta(hours=4)  # UTC → RD (UTC-4)
            now_utc = datetime.now(timezone.utc)
            diff = (now_utc - vt_utc).total_seconds()
            latencia = f"Vela: {vt_rd.strftime('%H:%M:%S')} RD | Pub: {publish_time} | Delay: {int(diff)}s\n"
        except:
            latencia = f"Publicado: {publish_time}\n"
    else:
        latencia = f"Publicado: {publish_time}\n"
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
        f"{latencia}"
        f"---\n"
        f"Trading Pro — XAUUSD_Signals_DR"
    )

# ── FETCH CANDLES ─────────────────────────────────────────────

def get_candles(timeframe, limit=100, instrument="XAUUSD"):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/ohlc_candles",
        headers=headers(),
        params={
            "select":     "candle_time,open,high,low,close,volume",
            "instrument": f"eq.{instrument}",
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

# ── FILTRO ANTI-TRAMPA INSTITUCIONAL DEL FVG — NUEVO ──────────
# Un FVG "trampa" es un gap que parece institucional pero en realidad
# es ruido o manipulacion. Se valida con 3 criterios: si hubo barrido
# de liquidez antes de formarse, si la vela de impulso tiene cuerpo
# dominante y tamano razonable vs ATR, y si el retest reacciona en vez
# de atravesarlo de largo.

def hubo_sweep_antes_del_fvg(candles, fvg, lookback=15):
    """
    Comprueba si, poco antes de formarse el FVG, hubo un barrido de
    liquidez (mecha que rompe un swing high/low reciente y cierra de
    vuelta adentro). Un FVG institucional real casi siempre viene
    despues de cazar stops, no de la nada.
    """
    idx = fvg["idx"]
    start = max(0, idx - lookback)
    previas = candles[start:idx - 1]  # velas antes de la vela de impulso
    if len(previas) < 5:
        return False

    highs, lows = detect_swing_hl(previas, lookback=2)
    if not highs and not lows:
        return False

    impulso = candles[idx - 1]

    if fvg["type"] == "BUY" and lows:
        swing_l = min(l["price"] for l in lows[-2:])
        if impulso["L"] < swing_l and impulso["C"] > swing_l:
            return True

    if fvg["type"] == "SELL" and highs:
        swing_h = max(h["price"] for h in highs[-2:])
        if impulso["H"] > swing_h and impulso["C"] < swing_h:
            return True

    return False


def score_validez_fvg(candles, fvg, atr):
    """
    Evalua si un FVG parece institucional real o una trampa, mirando
    la vela de impulso que lo creo (la del medio, indice fvg['idx']-1)
    y el tamano del gap relativo al ATR.
    Devuelve (score_ajuste, lista_de_razones).
    """
    score, reasons = 0, []
    idx = fvg["idx"]
    if idx < 1 or idx >= len(candles):
        return 0, []

    impulso = candles[idx - 1]
    body    = abs(impulso["C"] - impulso["O"])
    rango   = impulso["H"] - impulso["L"]
    if rango == 0:
        return 0, []
    body_ratio = body / rango

    # Vela de impulso con cuerpo dominante = flujo institucional real
    if body_ratio >= 0.70:
        score += 20; reasons.append(f"Impulso fuerte {body_ratio:.0%}")
    elif body_ratio < 0.40:
        score -= 15; reasons.append(f"Impulso debil {body_ratio:.0%} (sospechoso)")

    # Tamano del FVG vs ATR — un gap gigante suele ser noticia/spike, no flujo ordenado
    gap_size = fvg["top"] - fvg["bottom"]
    if atr > 0:
        ratio_atr = gap_size / atr
        if ratio_atr > 2.5:
            score -= 15; reasons.append(f"FVG anomalo {ratio_atr:.1f}x ATR")
        elif 0.3 <= ratio_atr <= 1.5:
            score += 10; reasons.append("FVG tamano normal")

    return score, reasons


def retest_reacciono_o_atraveso(candles, fvg, max_velas=3):
    """
    Desde que se formo el FVG, revisa las velas siguientes: si el
    precio lo atraviesa por completo sin reaccion (cierre mas alla
    del otro extremo) es trampa. Si rechaza dentro del gap, es valido.
    Devuelve True (valido), False (trampa confirmada), o None (aun
    sin suficientes velas para decidir).
    """
    idx = fvg["idx"]
    posteriores = candles[idx:idx + max_velas + 1]
    if not posteriores:
        return None

    for vela in posteriores:
        if fvg["type"] == "BUY" and vela["C"] < fvg["bottom"]:
            return False
        if fvg["type"] == "SELL" and vela["C"] > fvg["top"]:
            return False
    return True

# ── CONFLUENCIAS PARA SCALPING M5 SMC — NUEVO ──────────────────
# 5 criterios adicionales de confluencia para reforzar la estrategia 1
# (Sweep + BOS/CHoCH): Order Block, FVG cercano, momentum del rechazo
# en la vela de sweep, madurez de la tendencia en TFs superiores, y
# zona de liquidez de sesion (rango asiatico/pre-London).

def detect_last_order_block(candles, direction, lookback=30):
    """
    Encuentra el ultimo Order Block valido: la ultima vela opuesta a la
    direccion buscada, justo antes de un movimiento impulsivo fuerte
    (cuerpo >=60% del rango) que se produjo en esa direccion.
    BUY  -> OB alcista: vela bajista antes de un impulso alcista fuerte.
    SELL -> OB bajista: vela alcista antes de un impulso bajista fuerte.
    Devuelve {"top", "bottom", "idx"} o None si no se encuentra.
    """
    start = max(0, len(candles) - lookback)
    for i in range(len(candles) - 2, start, -1):
        impulso = candles[i]
        prev    = candles[i - 1]
        rango   = impulso["H"] - impulso["L"]
        if rango == 0:
            continue
        body_ratio = abs(impulso["C"] - impulso["O"]) / rango
        if body_ratio < 0.60:
            continue

        if direction == "BUY" and impulso["C"] > impulso["O"] and prev["C"] < prev["O"]:
            return {"top": prev["H"], "bottom": prev["L"], "idx": i - 1}
        if direction == "SELL" and impulso["C"] < impulso["O"] and prev["C"] > prev["O"]:
            return {"top": prev["H"], "bottom": prev["L"], "idx": i - 1}

    return None


def price_near_ob(price, ob, atr, tolerance_atr=0.3):
    """Comprueba si el precio actual esta dentro (o cerca) de la zona del OB."""
    if not ob or atr <= 0:
        return False
    buffer = atr * tolerance_atr
    return (ob["bottom"] - buffer) <= price <= (ob["top"] + buffer)


def fvg_confluencia_cercana(candles, direction, current_price, atr, lookback=15):
    """
    Busca si existe un FVG reciente en la misma direccion del setup,
    cerca del precio actual. Es la secuencia clasica SMC: sweep -> BOS
    -> FVG -> entrada. Si el FVG ya esta ahi, refuerza la confluencia.
    """
    if atr <= 0:
        return False
    recientes = candles[-lookback:]
    fvgs = detect_fvgs(recientes)
    for f in fvgs:
        if f["type"] != direction:
            continue
        if abs(current_price - f["mid"]) <= atr * 1.5:
            return True
    return False


def sweep_rejection_fuerte(vela, direction):
    """
    Valida que la vela que hizo el sweep tenga un rechazo fuerte: mecha
    larga en la direccion barrida y cierre claramente alejado del
    extremo. Un sweep con mecha corta es mas sospechoso de ser ruido.
    BUY  -> sweep de un low -> se espera mecha inferior larga (>=35% del rango)
    SELL -> sweep de un high -> se espera mecha superior larga (>=35% del rango)
    """
    rango = vela["H"] - vela["L"]
    if rango == 0:
        return False
    if direction == "BUY":
        mecha_inf = min(vela["C"], vela["O"]) - vela["L"]
        return (mecha_inf / rango) >= 0.35
    else:
        mecha_sup = vela["H"] - max(vela["C"], vela["O"])
        return (mecha_sup / rango) >= 0.35


def tendencia_madura(candles, period_fast=9, period_slow=20):
    """
    Compara la separacion actual entre EMA9/EMA20 contra la separacion
    de hace 3 velas. Si la separacion se mantiene o crece, la tendencia
    de ese timeframe esta establecida (no es un cruce recien formado,
    mas propenso a fakeout). Devuelve True/False, o None si faltan datos.
    """
    closes = [c["C"] for c in candles]
    if len(closes) < period_slow + 5:
        return None
    e9_now  = ema(closes, period_fast)
    e20_now = ema(closes, period_slow)
    e9_prev  = ema(closes[:-3], period_fast)
    e20_prev = ema(closes[:-3], period_slow)
    if not all([e9_now, e20_now, e9_prev, e20_prev]):
        return None
    sep_now  = abs(e9_now - e20_now)
    sep_prev = abs(e9_prev - e20_prev)
    return sep_now >= sep_prev


def liquidez_sesion_confluencia(candles, direction, current_price, lookback=40):
    """
    Compara el precio actual contra el rango reciente (aprox. sesion
    asiatica / pre-London) como zona de liquidez de referencia. Los
    sweeps que ocurren cerca del limite de ese rango suelen tener mas
    peso, porque ahi es donde se acumulan stops de retail.
    """
    ventana = candles[-lookback:]
    if len(ventana) < 10:
        return False
    rango_high = max(c["H"] for c in ventana)
    rango_low  = min(c["L"] for c in ventana)
    tolerancia = (rango_high - rango_low) * 0.15
    if tolerancia <= 0:
        return False
    if direction == "BUY":
        return abs(current_price - rango_low) <= tolerancia
    else:
        return abs(current_price - rango_high) <= tolerancia

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

# ── FILTRO DE CORRELACIÓN CON EL DOLAR (DXY sintetico) ────────
# Este broker no expone un simbolo nativo de indice del dolar, asi que se
# construye uno sintetico combinando la tendencia de 4 pares mayores.

def get_pair_trend(par, timeframe="M30", limit=60):
    """
    Calcula la tendencia (BUY/SELL) de un par segun EMA9 vs EMA20,
    igual que se hace para XAUUSD. Devuelve None si no hay datos
    suficientes (ej. data_engine.py aun no ha subido velas de este par).
    """
    rows = get_candles(timeframe, limit, instrument=par)
    if len(rows) < 20:
        return None
    closes = [float(r["close"]) for r in rows]
    e9  = ema(closes, 9)
    e20 = ema(closes, 20)
    if not e9 or not e20:
        return None
    return "BUY" if e9 > e20 else "SELL"

def get_dxy_trend():
    """
    Combina la tendencia de EURUSD, GBPUSD, USDCHF y USDJPY en un voto
    de fuerza del dolar:
      - EURUSD/GBPUSD en SELL (bajando) = voto a favor del dolar fuerte
      - USDCHF/USDJPY en BUY  (subiendo) = voto a favor del dolar fuerte
    Devuelve:
      "BUY"     -> dolar fortaleciendose (3-4 votos de 4)
      "SELL"    -> dolar debilitandose   (0-1 votos de 4)
      "NEUTRAL" -> sin consenso claro (2 votos), o datos insuficientes
    """
    votos_dolar_fuerte = 0
    pares_evaluados = 0

    for par in DOLLAR_PAIRS:
        trend = get_pair_trend(par)
        if trend is None:
            continue
        pares_evaluados += 1

        if par in DOLLAR_PAIRS_INVERSOS:
            # EURUSD/GBPUSD: si el par SELL (bajando), el dolar se fortalece
            if trend == "SELL":
                votos_dolar_fuerte += 1
        else:
            # USDCHF/USDJPY: si el par BUY (subiendo), el dolar se fortalece
            if trend == "BUY":
                votos_dolar_fuerte += 1

    if pares_evaluados < 3:
        # Datos insuficientes (ej. data_engine.py recien desplegado, o
        # aun no ha subido suficientes velas de los pares) -> no forzar opinion.
        return "NEUTRAL"

    if votos_dolar_fuerte >= 3:
        return "BUY"    # dolar fortaleciendose
    if votos_dolar_fuerte <= 1:
        return "SELL"   # dolar debilitandose
    return "NEUTRAL"    # 2 de 4 — sin consenso


def dxy_score_bonus(signal_type, dxy_trend):
    """
    Bono/penalizacion segun la correlacion clasica oro-dolar:
      oro SELL + dolar fuerte (BUY)  -> confirma  -> +DXY_SCORE_BONUS
      oro BUY  + dolar debil (SELL)  -> confirma  -> +DXY_SCORE_BONUS
      cualquier otra combinacion (que no sea NEUTRAL) -> contradice -> -DXY_SCORE_BONUS
      dxy_trend NEUTRAL -> no afecta -> 0
    Devuelve (puntos, razon_texto_o_None).
    """
    if dxy_trend == "NEUTRAL":
        return 0, None

    confirma = (signal_type == "SELL" and dxy_trend == "BUY") or \
               (signal_type == "BUY" and dxy_trend == "SELL")

    if confirma:
        return DXY_SCORE_BONUS, f"USD {dxy_trend} confirma {signal_type} (+{DXY_SCORE_BONUS})"
    else:
        return -DXY_SCORE_BONUS, f"USD {dxy_trend} contradice {signal_type} (-{DXY_SCORE_BONUS})"

# ── VALIDADOR DE VIGENCIA (filtro anti-manipulación) ──────────
# Los ~90s de latencia del sistema filtran fakeouts naturalmente
# Si el precio ya se alejó más de 0.5x ATR → señal expirada

def señal_vigente(sig, precio_actual):
    """
    Verifica que el precio sigue cerca de la entrada original.
    Si se alejó más de 0.5x ATR la señal ya expiró (manipulación o movimiento rápido).
    """
    atr       = sig.get("atr", 5)
    distancia = abs(precio_actual - sig["entry_price"])
    limite    = atr * 0.5
    vigente   = distancia <= limite
    if not vigente:
        print(f"  [VIGENCIA] Señal expirada — precio se alejó {distancia:.2f} pts (max {limite:.2f})")
    return vigente

# ── KILLZONE OBLIGATORIA ───────────────────────────────────────
# Estrategias 1, 3 y 4 solo operan en London/NY KZ
# Evita señales en zona muerta donde el mercado no tiene dirección

def killzone_requerida(nombre_estrategia):
    """Bloquea estrategias fuera de killzone."""
    if not is_killzone():
        print(f"  [{nombre_estrategia}] Bloqueado — fuera de Killzone (solo opera en London/NY KZ)")
        return False
    return True

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

    # ── Validador de vigencia anti-manipulación ──
    # Obtener precio actual de la última vela M5
    try:
        rows = get_candles("M5", 1)
        if rows:
            precio_actual = float(rows[0]["close"])
            if not señal_vigente(sig, precio_actual):
                return False
    except:
        pass  # Si falla el check, continuar igual

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

def strategy_scalping_m5(c5, c30, ch1, dxy_trend="NEUTRAL"):
    if not killzone_requerida("1-Scalping SMC"):
        return None
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

    if not sig_type:
        return None

    # ── NUEVO: confluencias adicionales para Scalping M5 SMC ──
    ventana_scalp = c5[-40:]

    # 1. Order Block — el sweep deberia ocurrir cerca de un OB de mayor jerarquia
    ob = detect_last_order_block(ventana_scalp, sig_type)
    if price_near_ob(last["C"], ob, atr):
        score += 15
        reasons.append("Order Block confirmado")

    # 2. FVG cercano en la misma direccion (secuencia sweep -> BOS -> FVG)
    if fvg_confluencia_cercana(ventana_scalp, sig_type, last["C"], atr):
        score += 10
        reasons.append("FVG cercano confluencia")

    # 3. Momentum del rechazo en la vela de sweep
    if sweep_rejection_fuerte(last, sig_type):
        score += 10
        reasons.append("Rechazo fuerte en sweep")
    else:
        score -= 10
        reasons.append("Sweep debil (mecha corta)")

    # 4. Madurez de la tendencia en M30/H1 — evita cruces de EMA recien formados
    madura_m30 = tendencia_madura(c30)
    madura_h1  = tendencia_madura(ch1)
    if madura_m30:
        score += 5
        reasons.append("Tendencia M30 establecida")
    if madura_h1:
        score += 5
        reasons.append("Tendencia H1 establecida")

    # 5. Zona de liquidez de sesion (rango asiatico/pre-London)
    if liquidez_sesion_confluencia(ventana_scalp, sig_type, last["C"]):
        score += 10
        reasons.append("Zona de liquidez de sesion")

    if score < MIN_SCORE:
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

    # ── Bono/penalizacion por correlacion con el dolar ──
    dxy_bonus, dxy_reason = dxy_score_bonus(sig_type, dxy_trend)
    if dxy_bonus != 0:
        score += dxy_bonus
        if dxy_reason:
            reasons.append(dxy_reason)

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
        "candle_time":   last["time"],
    }

# ════════════════════════════════════════════════════════════════
# ESTRATEGIA 2 — LONDON/NY KILLZONE BREAKOUT
# Opera el primer movimiento fuerte al inicio de cada sesión
# ════════════════════════════════════════════════════════════════

def strategy_killzone_breakout(c5, ch1, dxy_trend="NEUTRAL"):
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

    # ── Bono/penalizacion por correlacion con el dolar ──
    dxy_bonus, dxy_reason = dxy_score_bonus(sig_type, dxy_trend)
    if dxy_bonus != 0:
        score += dxy_bonus
        if dxy_reason:
            reasons.append(dxy_reason)

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
        "candle_time":   last["time"],
    }

# ════════════════════════════════════════════════════════════════
# ESTRATEGIA 3 — FVG FILL M5
# Detecta Fair Value Gaps recientes y espera que el precio regrese
# Incluye filtro anti-trampa institucional (sweep previo, momentum
# de la vela de impulso, tamano vs ATR, y reaccion en el retest)
# ════════════════════════════════════════════════════════════════

def strategy_fvg_fill(c5, c30, dxy_trend="NEUTRAL"):
    if not killzone_requerida("3-FVG Fill"):
        return None
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
    ventana_fvg = c5[-40:]
    fvgs = detect_fvgs(ventana_fvg)
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

    # ── Filtro anti-trampa institucional del FVG ──
    fvg_sweep_ok  = hubo_sweep_antes_del_fvg(ventana_fvg, fvg)
    fvg_bonus, fvg_reasons = score_validez_fvg(ventana_fvg, fvg, atr)
    fvg_retest_ok = retest_reacciono_o_atraveso(ventana_fvg, fvg)

    if fvg_retest_ok is False:
        print(f"  [3] FVG Fill: {sig_type} descartado — precio atraveso el FVG sin reaccionar (trampa)")
        return None

    if fvg_sweep_ok:
        score += 15
        reasons.append("Sweep de liquidez previo al FVG")
    else:
        score -= 10
        reasons.append("Sin sweep previo (FVG dudoso)")

    score += fvg_bonus
    reasons.extend(fvg_reasons)

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

    # ── Bono/penalizacion por correlacion con el dolar ──
    dxy_bonus, dxy_reason = dxy_score_bonus(sig_type, dxy_trend)
    if dxy_bonus != 0:
        score += dxy_bonus
        if dxy_reason:
            reasons.append(dxy_reason)

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
        "candle_time":   last["time"],
    }

# ════════════════════════════════════════════════════════════════
# ESTRATEGIA 4 — EMA PULLBACK M5
# Pullback a EMA9/20 en tendencia clara con vela de confirmación
# ════════════════════════════════════════════════════════════════

def strategy_ema_pullback(c5, c30, ch1, dxy_trend="NEUTRAL"):
    if not killzone_requerida("4-EMA Pullback"):
        return None
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

    # ── Bono/penalizacion por correlacion con el dolar ──
    dxy_bonus, dxy_reason = dxy_score_bonus(sig_type, dxy_trend)
    if dxy_bonus != 0:
        score += dxy_bonus
        if dxy_reason:
            reasons.append(dxy_reason)

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
        "candle_time":   last["time"],
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

    # Tendencia del dolar (indice sintetico), una sola vez por ciclo
    # y se pasa a las 4 estrategias — evita recalcularla 4 veces por ronda.
    dxy_trend = get_dxy_trend()

    last_price = c5[-1]["C"]
    session    = get_session()
    kz         = "KZ ACTIVA" if is_killzone() else "fuera de KZ"
    print(f"  Precio: {last_price:.2f} | Sesión: {session} | {kz} | Señales hoy: {daily_count}/{MAX_DAILY}")
    print(f"  Dolar (DXY sintetico): {dxy_trend}")

    signals_found = 0

    # ── TradingPro AI Elite
    try:
        if not AI_ENGINE_AVAILABLE or analyze_tradingpro_ai is None:
            print(f"  [AI] TradingPro AI Elite desactivado: {AI_ENGINE_IMPORT_ERROR}")
        else:
            c5_ai = [{
                "time": c["time"],
                "open": c["O"],
                "high": c["H"],
                "low": c["L"],
                "close": c["C"],
                "volume": c["V"],
            } for c in c5]

            c15_rows = get_candles("M15", 250)
            c15 = to_candles(c15_rows)

            c15_ai = [{
                "time": c["time"],
                "open": c["O"],
                "high": c["H"],
                "low": c["L"],
                "close": c["C"],
                "volume": c["V"],
            } for c in c15]

            sig = analyze_tradingpro_ai(c15_ai, c5_ai)

            if sig:
                if publish_signal(sig):
                    signals_found += 1
                else:
                    print("  [AI] TradingPro AI Elite: señal detectada pero no publicada")
            else:
                print("  [AI] TradingPro AI Elite: sin setup")
    except Exception as e:
        print(f"  [AI] Error TradingPro AI Elite: {e}")

    # ── Estrategia 1: Scalping M5 SMC
    try:
        sig = strategy_scalping_m5(c5, c30, ch1, dxy_trend)
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
        sig = strategy_killzone_breakout(c5, ch1, dxy_trend)
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
        sig = strategy_fvg_fill(c5, c30, dxy_trend)
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
        sig = strategy_ema_pullback(c5, c30, ch1, dxy_trend)
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
    print(f"    AI. TradingPro AI Elite (Confluence Engine)")
    print(f"    1. Scalping M5 SMC (Sweep + BOS/CHoCH + OTE Fib + OB/FVG/liquidez)")
    print(f"    2. Killzone Breakout (London/NY)")
    print(f"    3. FVG Fill M5 (con filtro anti-trampa institucional)")
    print(f"    4. EMA Pullback M5")
    print(f"  ICT OTE: Golden Pocket 70.5% | Zona 62-79% Fibonacci")
    print(f"  Filtro DXY sintetico: {', '.join(DOLLAR_PAIRS)} (bono/penalizacion +/-{DXY_SCORE_BONUS}pts)")
    print(f"  Filtro FVG anti-trampa: sweep previo + momentum impulso + retest")
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
            f"Filtro DXY sintetico activo ({', '.join(DOLLAR_PAIRS)})\n"
            f"Filtro FVG anti-trampa activo\n"
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
