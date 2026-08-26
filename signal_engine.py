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

Opening Range Breakout con Pullback y VWAP — NUEVO (mejora Estrategia 2):
  - En vez de disparar en la misma vela que rompe el rango de apertura,
    la Estrategia 2 (Killzone Breakout) ahora exige el patron completo
    Breakout -> Pullback -> Reanudacion (ver detect_orb_pullback).
    Reduce frecuencia de señales de esta estrategia a cambio de mejor
    calidad de entrada.
  - Se suma una confirmacion de VWAP de la sesion activa (bono/penal.
    de score +/-10, igual estilo que el filtro DXY): confirma si el
    precio esta del lado esperado del volumen ponderado de la killzone.
    Usa el volumen de ticks de MT5 como proxy (XAUUSD es CFD, no hay
    volumen real negociado).

Filtro de Volatilidad Minima (ATR) — NUEVO:
  - Evita entrar cuando el mercado esta en rango muerto (pre-Londres,
    almuerzo NY, fines de sesion) donde el spread se come el TP.
  - Compara el ATR(14) actual de M5 contra un ATR historico promedio
    de referencia (ATR_PROMEDIO_HISTORICO, calculado una vez sobre 30
    dias y fijado en .env) multiplicado por ATR_MIN_MULTIPLIER.
  - Si el ATR actual cae por debajo de ese umbral, la señal se descarta
    directamente. Se aplica a las estrategias 1, 3 y 4 (la 2 ya exige
    un rango pre-sesion minimo por diseño y no lo necesita).

Validador de Vigencia (filtro anti-manipulación):
  - Compara precio actual vs entrada original antes de publicar
  - Si el precio se alejó más de 0.5x ATR → señal expirada, descarta
  - Los ~90s de latencia actúan como filtro natural de fakeouts/sweeps
  - Timestamp en Telegram muestra vela_time vs publish_time para medir retraso

Killzone — bono de score, ya NO es obligatoria (CAMBIO, ver FIX abajo):
  - Estrategias 1 y 3 pueden publicar señal fuera de killzone si el
    resto de las confluencias dan score suficiente (>= MIN_SCORE). Ya
    no hay bloqueo duro — killzone activa sigue sumando puntos igual
    que antes, solo que ahora es opcional en vez de obligatoria.
  - Estrategia 4 (EMA Pullback M5) tiene una regla especial desde el
    2026-08-17: en killzone NYC solo publica señal si el score es muy
    alto (>= NYC_MIN_SCORE_EMA_PULLBACK, 90 por defecto) — el resto de
    esa ventana horaria queda descartada por evidencia real de
    resultados (ver mas abajo).
  - Estrategia 2 SIGUE exigiendo killzone obligatoria por diseño: su
    logica entera es operar la ruptura del rango justo al abrir sesion
    (London/NY), no tiene sentido estructural fuera de ese contexto.

Objetivo: 4-6 señales diarias de alta calidad
Score mínimo para publicar: 70/100
Loop interno: analiza cada 30 segundos
"""

# ============================================================
# FIX 2026-08-13 (killzone de bloqueo obligatorio -> bono de score):
#   - Antes, killzone_requerida() cortaba la ejecucion de las
#     estrategias 1, 3 y 4 por completo si is_killzone() era False —
#     ni siquiera llegaban a calcular el resto de las confluencias.
#   - A peticion explicita: se quiere poder tomar operaciones fuera de
#     killzone tambien, siempre que el resto de las condiciones se
#     cumplan igual de exigentes (mismo MIN_SCORE=75, sin relajar
#     ningun otro filtro).
#   - killzone_requerida() ya NO bloquea (siempre devuelve True) — solo
#     deja un aviso informativo en el log cuando la señal se evalua
#     fuera de killzone. La killzone activa sigue sumando su bono de
#     score normal dentro de cada estrategia (linea "if is_killzone():
#     score += N"), exactamente igual que antes — la diferencia es que
#     ahora ese bono es opcional, no una condicion de entrada obligatoria.
#   - Estrategia 2 (Killzone Breakout) NO se toco: sigue con su propio
#     chequeo `if not is_killzone(): return None` al inicio de la
#     funcion, porque su logica completa depende de operar el rango de
#     apertura de sesion — no tiene sentido estructural fuera de eso.
# ============================================================

# ============================================================
# FIX 2026-08-17 (EMA Pullback M5 — killzone NYC + M30/H1 obligatorio):
#   - Analisis del historico real de trades del bot (origen=BOT,
#     strategy='EMA Pullback M5', 25 operaciones): las que cerraron en
#     horario NYC (9:00-12:00 RD) dieron 1W/9L, -$130.46 — el 96% de
#     la perdida total de la estrategia (-$135.40). Fuera de esa
#     ventana la misma estrategia dio 9W/4L, +$21.70 neto.
#   - CAMBIO 1 (v1, bloqueo total -> v2, umbral de score): la primera
#     version bloqueaba NYC por completo para esta estrategia. A
#     pedido explicito de Arioldys, se cambia a un umbral de score mas
#     exigente: en NYC solo se permite operar si el score final es
#     >= NYC_MIN_SCORE_EMA_PULLBACK (90 por defecto). Asi no se
#     descartan de plano señales realmente excelentes que caigan en
#     esa ventana, pero se sigue bloqueando el grueso 75-89 que es
#     donde vivia casi toda la perdida historica.
#   - CAMBIO 2: dir_m30 y dir_h1, que antes sumaban +15 c/u como bono
#     (el score se saturaba en 90-100 con o sin alineacion real de
#     timeframes mayores, perdiendo poder de discriminacion), ahora
#     son FILTRO OBLIGATORIO para EMA Pullback M5: si M30 o H1 no
#     coinciden con la direccion del pullback, la señal se descarta
#     antes de sumar score.
# ============================================================

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
MAX_DAILY        = 12    # Máximo señales por día (bajado de 20 a 12, 2026-08-18: 20 era ruido excesivo en Supabase/Telegram; bot_engine.py tiene su PROPIO MAX_DAILY de ejecucion, este solo limita cuantas señales se publican)
LOOP_INTERVAL    = 30    # Segundos entre cada análisis
SIGNAL_COOLDOWN  = 300   # subido de 120 a 300s (2026-08-18): 120s dejaba abierta la ventana a 2-3 señales de la misma estrategia en la misma zona de liquidez antes de que cierre una vela M5

# Pares usados para construir el indice sintetico de fuerza del dolar.
# Deben coincidir con los que data_engine.py sube a ohlc_candles.
DOLLAR_PAIRS = ["EURUSD", "GBPUSD", "USDCHF", "USDJPY"]
# EURUSD y GBPUSD son inversos al USD (par sube = USD se debilita).
# USDCHF y USDJPY son directos al USD (par sube = USD se fortalece).
DOLLAR_PAIRS_INVERSOS = {"EURUSD", "GBPUSD"}
DXY_SCORE_BONUS = 10  # puntos que suma/resta la confirmacion/contradiccion del dolar

# Filtro de volatilidad minima (ATR) — NUEVO
# ATR_PROMEDIO_HISTORICO se calcula UNA SOLA VEZ (script aparte, 30 dias)
# y se fija en .env. No se recalcula en vivo para no agregar carga al loop.
ATR_PROMEDIO_HISTORICO = float(os.getenv("ATR_PROMEDIO_HISTORICO", "2.0"))
ATR_MIN_MULTIPLIER     = float(os.getenv("ATR_MIN_MULTIPLIER", "0.8"))

# ── Killzone NYC para EMA Pullback M5 — NUEVO 2026-08-17 (v2) ──
# Historico real: 10 trades en NYC (9-12 RD) = 1W/9L, -$130.46.
# El resto del dia (misma estrategia) = 9W/4L, +$21.70. La ventana NYC
# concentra el 96% de la perdida total de esta estrategia.
# v1 bloqueaba NYC por completo; v2 en cambio permite operar en NYC
# solo si el score es muy alto (>= NYC_MIN_SCORE_EMA_PULLBACK), a
# pedido explicito de Arioldys — asi no se pierden señales realmente
# excelentes solo por caer en la ventana horaria mala.
BLOCK_NYC_EMA_PULLBACK = os.getenv("BLOCK_NYC_EMA_PULLBACK", "true").lower() == "true"
NYC_MIN_SCORE_EMA_PULLBACK = int(os.getenv("NYC_MIN_SCORE_EMA_PULLBACK", "90"))

# ── Filtros anti-whipsaw para EMA Pullback M5 — NUEVO 2026-08-18 ──
# Diagnostico: el historico real de la estrategia (10 trades en killzone
# NYC = 1W/9L, -$130.46, 96% de la perdida total) es la firma clasica de
# un sistema de cruce de EMAs operando en rango — el ATR minimo ya
# filtra volatilidad, pero no distingue movimiento DIRECCIONAL de ruido
# lateral. Se agregan dos filtros OBLIGATORIOS (no bono):
#   1. ADX(14) > ADX_MIN_TREND y subiendo — prohibe operar en rango.
#   2. VWAP de sesion como sesgo direccional — solo BUY sobre VWAP,
#      solo SELL bajo VWAP. Si no hay sesion activa (calc_session_vwap
#      devuelve None fuera de killzone), el filtro se omite (no bloquea)
#      porque no hay VWAP de sesion que evaluar.
ADX_PERIOD = 14
ADX_MIN_TREND = float(os.getenv("ADX_MIN_TREND", "18"))  # bajado de 25 a 18 (2026-08-18): oro en M5 rara vez sostiene ADX>25, 25 dejaba casi sin señales

def is_nyc_killzone():
    now = datetime.now(timezone.utc)
    rdh = ((now.hour - 4) + 24) % 24
    t   = rdh * 100 + now.minute
    return 900 <= t < 1200

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
    vela_time    = sig.get("candle_time", "N/A")
    publish_time = datetime.now().strftime("%H:%M:%S")
    latencia     = ""
    if vela_time != "N/A":
        try:
            vt_raw = datetime.fromisoformat(vela_time.replace("Z", "+00:00"))
            vt_utc = vt_raw if vt_raw.tzinfo else vt_raw.replace(tzinfo=timezone.utc)
            vt_rd  = vt_utc - timedelta(hours=4)
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

def calc_adx(candles, period=ADX_PERIOD):
    """ADX(14) estandar (metodo de Wilder). Devuelve (adx_actual, subiendo)
    donde `subiendo` compara el ADX actual contra el de 3 velas atras
    (mas estable que comparar solo contra la vela inmediata anterior).
    Devuelve (None, None) si no hay velas suficientes."""
    if len(candles) < period * 3:
        return None, None

    trs, plus_dms, minus_dms = [], [], []
    for i in range(1, len(candles)):
        high, low = candles[i]["H"], candles[i]["L"]
        prev_high, prev_low, prev_close = candles[i-1]["H"], candles[i-1]["L"], candles[i-1]["C"]

        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)

        up_move = high - prev_high
        down_move = prev_low - low
        plus_dm  = up_move   if (up_move > down_move and up_move > 0) else 0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0
        plus_dms.append(plus_dm)
        minus_dms.append(minus_dm)

    def wilder_smooth(values, period):
        if len(values) < period:
            return []
        smoothed = [sum(values[:period])]
        for v in values[period:]:
            smoothed.append(smoothed[-1] - (smoothed[-1] / period) + v)
        return smoothed

    tr_s    = wilder_smooth(trs, period)
    plus_s  = wilder_smooth(plus_dms, period)
    minus_s = wilder_smooth(minus_dms, period)

    dx_values = []
    for i in range(min(len(tr_s), len(plus_s), len(minus_s))):
        if tr_s[i] == 0:
            dx_values.append(0)
            continue
        plus_di  = 100 * plus_s[i]  / tr_s[i]
        minus_di = 100 * minus_s[i] / tr_s[i]
        di_sum = plus_di + minus_di
        dx_values.append(100 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0)

    if len(dx_values) < period:
        return None, None

    adx_values = [sum(dx_values[:period]) / period]
    for dx in dx_values[period:]:
        adx_values.append((adx_values[-1] * (period - 1) + dx) / period)

    if not adx_values:
        return None, None

    current = round(adx_values[-1], 2)
    ref_idx = -4 if len(adx_values) >= 4 else -2 if len(adx_values) >= 2 else None
    subiendo = adx_values[-1] > adx_values[ref_idx] if ref_idx is not None else None
    return current, subiendo


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

def extension_agotada(candles, direction, current_price, atr, lookback=20, max_atr_multiplo=2.0):
    """
    Rechaza la señal si el precio ya recorrió demasiada distancia (en ATR)
    desde el swing que originó el movimiento — es decir, si la entrada
    llegaría después de que el impulso ya se agotó (entrada tardía,
    "se toma la orden después del movimiento").

    direction: "BUY" o "SELL"
    Devuelve (agotada: bool, detalle: str)
    """
    if atr <= 0:
        return False, "ATR invalido, no se puede evaluar extension"

    highs, lows = detect_swing_hl(candles[-lookback:], lookback=3)

    if direction == "SELL":
        if not highs:
            return False, "sin swing high de referencia"
        origen = max(h["price"] for h in highs)
        recorrido = origen - current_price
    else:
        if not lows:
            return False, "sin swing low de referencia"
        origen = min(l["price"] for l in lows)
        recorrido = current_price - origen

    ratio = recorrido / atr
    agotada = ratio > max_atr_multiplo
    if agotada:
        print(
            f"  [EXTENSION] Movimiento ya recorrio {recorrido:.2f} pts "
            f"({ratio:.1f}x ATR, max {max_atr_multiplo}x) — entrada tardia, se descarta"
        )
    return agotada, f"{ratio:.1f}x ATR"

def spike_reciente(candles, atr, max_atr_multiplo=2.0):
    """Rechaza la señal si la ultima vela ya tuvo un rango (H-L) anormalmente
    grande vs el ATR promedio — señal de spread ensanchado / spike de
    volatilidad (noticia, apertura de sesion) donde el SL calculado con
    ATR promedio (que no reacciona rapido) queda demasiado ajustado y
    puede ser barrido casi de inmediato tras abrir la posicion."""
    if atr <= 0 or not candles:
        return False, "ATR invalido, no se puede evaluar spike"

    ultima = candles[-1]
    rango  = ultima["H"] - ultima["L"]
    ratio  = rango / atr
    es_spike = ratio > max_atr_multiplo
    if es_spike:
        print(
            f"  [SPIKE] Ultima vela con rango {rango:.2f} pts "
            f"({ratio:.1f}x ATR, max {max_atr_multiplo}x) — posible spread "
            f"ensanchado/spike de volatilidad, se descarta"
        )
    return es_spike, f"{ratio:.1f}x ATR"

def detect_fvgs(candles):
    fvgs = []
    for i in range(2, len(candles)):
        a, b, c = candles[i-2], candles[i-1], candles[i]
        if a["H"] < c["L"]:
            fvgs.append({"type": "BUY", "top": c["L"], "bottom": a["H"],
                          "mid": (c["L"] + a["H"]) / 2, "idx": i})
        if a["L"] > c["H"]:
            fvgs.append({"type": "SELL", "top": a["L"], "bottom": c["H"],
                          "mid": (a["L"] + c["H"]) / 2, "idx": i})
    return fvgs

def detect_orb_pullback(candles, range_high, range_low, direction, lookback=6):
    ventana = candles[-lookback:]
    if len(ventana) < 3:
        return False

    if direction == "BUY":
        breakout_idx = None
        for i, c in enumerate(ventana[:-1]):
            if c["C"] > range_high:
                breakout_idx = i
                break
        if breakout_idx is None:
            return False

        pullback_candles = ventana[breakout_idx + 1:-1]
        if not pullback_candles:
            return False
        if any(c["C"] < range_high for c in pullback_candles):
            return False

        pullback_high = max(c["H"] for c in pullback_candles)
        last = ventana[-1]
        return last["C"] > pullback_high and last["C"] > range_high

    else:
        breakout_idx = None
        for i, c in enumerate(ventana[:-1]):
            if c["C"] < range_low:
                breakout_idx = i
                break
        if breakout_idx is None:
            return False

        pullback_candles = ventana[breakout_idx + 1:-1]
        if not pullback_candles:
            return False
        if any(c["C"] > range_low for c in pullback_candles):
            return False

        pullback_low = min(c["L"] for c in pullback_candles)
        last = ventana[-1]
        return last["C"] < pullback_low and last["C"] < range_low


def get_session_start_utc():
    now = datetime.now(timezone.utc)
    rdh = ((now.hour - 4) + 24) % 24
    t = rdh * 100 + now.minute

    if 300 <= t < 600:
        start_rdh = 3
    elif 900 <= t < 1200:
        start_rdh = 9
    else:
        return None

    start_utc_hour = (start_rdh + 4) % 24
    start = now.replace(hour=start_utc_hour, minute=0, second=0, microsecond=0)
    if start > now:
        start -= timedelta(days=1)
    return start


def calc_session_vwap(candles):
    session_start = get_session_start_utc()
    if session_start is None:
        return None

    session_candles = []
    for c in candles:
        try:
            ct = datetime.fromisoformat(c["time"].replace("Z", "+00:00"))
        except Exception:
            continue
        if ct >= session_start:
            session_candles.append(c)

    if len(session_candles) < 3:
        return None

    total_vol = sum(c["V"] for c in session_candles)
    if total_vol <= 0:
        return None

    return sum(c["C"] * c["V"] for c in session_candles) / total_vol

def filtro_volatilidad_ok(atr_actual):
    umbral = ATR_PROMEDIO_HISTORICO * ATR_MIN_MULTIPLIER
    return atr_actual >= umbral, umbral

def hubo_sweep_antes_del_fvg(candles, fvg, lookback=15):
    idx = fvg["idx"]
    start = max(0, idx - lookback)
    previas = candles[start:idx - 1]
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

    if body_ratio >= 0.70:
        score += 20; reasons.append(f"Impulso fuerte {body_ratio:.0%}")
    elif body_ratio < 0.40:
        score -= 15; reasons.append(f"Impulso debil {body_ratio:.0%} (sospechoso)")

    gap_size = fvg["top"] - fvg["bottom"]
    if atr > 0:
        ratio_atr = gap_size / atr
        if ratio_atr > 2.5:
            score -= 15; reasons.append(f"FVG anomalo {ratio_atr:.1f}x ATR")
        elif 0.3 <= ratio_atr <= 1.5:
            score += 10; reasons.append("FVG tamano normal")

    return score, reasons


# ── Filtros "A+" para FVG Fill M5 — NUEVO 2026-08-18 ──
# Basado en la metodologia institucional: no todo FVG vale, solo los
# que nacen de un barrido de liquidez REAL (nivel clave, no cualquier
# swing), rompen estructura de verdad (no solo cuerpo grande), y estan
# ubicados en zona de buen precio (Discount para compras, Premium para
# ventas). Se implementan como BONOS/PENALIZACIONES de score (no
# bloqueo duro) para no cortar la frecuencia de la unica estrategia del
# bot sin perdidas — excepcion: la regla del 50% (CE) SI es bloqueo
# duro, igual que ya hace retest_reacciono_o_atraveso con el FVG
# completo, porque es una invalidacion real del setup, no una
# preferencia de calidad.

def institutional_sweep_before_fvg(c5, c15, fvg, lookback=15):
    """Version mas estricta de hubo_sweep_antes_del_fvg(): exige que el
    barrido previo haya sido sobre un nivel institucional real (rango
    asiatico, PDH/PDL, equal highs/lows) — no un swing menor cualquiera.
    Devuelve (True/False, nombre_del_nivel_o_None)."""
    idx = fvg["idx"]
    start = max(0, idx - lookback)
    previas = c5[start:idx]
    if not previas:
        return False, None

    asia_high, asia_low = get_asian_range(c15)
    pdh, pdl = get_pdh_pdl(c15)
    eq_high, eq_low = get_equal_levels(c5)

    # FIX 2026-08-20: antes solo exigia que ALGUNA vela tocara el nivel
    # (any(c["L"] < nivel ...)), sin comprobar que hubo rechazo real. Eso
    # califica como "sweep institucional" (+20 score) cualquier ruptura
    # con continuacion, no solo un barrido de liquidez real. Se alinea
    # con el mismo criterio que ya usa hubo_sweep_antes_del_fvg(): exige
    # que la vela que perfora el nivel tambien CIERRE de vuelta al otro
    # lado (rechazo), no solo que lo toque con la mecha.
    if fvg["type"] == "BUY":
        niveles = [("rango asiatico", asia_low), ("PDL", pdl), ("equal lows", eq_low)]
        for nombre, nivel in niveles:
            if nivel is not None and any(c["L"] < nivel and c["C"] > nivel for c in previas):
                return True, nombre
    else:
        niveles = [("rango asiatico", asia_high), ("PDH", pdh), ("equal highs", eq_high)]
        for nombre, nivel in niveles:
            if nivel is not None and any(c["H"] > nivel and c["C"] < nivel for c in previas):
                return True, nombre

    return False, None


def mss_confirmado_fvg(c5, fvg, lookback=20):
    """Confirma que la vela de impulso que genero el FVG realmente
    ROMPIO estructura (cerro mas alla del ultimo swing relevante), no
    solo que tuvo cuerpo grande (eso ya lo mide score_validez_fvg)."""
    idx = fvg["idx"]
    start = max(0, idx - lookback)
    previas = c5[start:idx - 1]
    if len(previas) < 5 or idx < 1:
        return False

    highs, lows = detect_swing_hl(previas, lookback=2)
    impulso = c5[idx - 1]

    if fvg["type"] == "BUY" and highs:
        swing_h = max(h["price"] for h in highs[-2:])
        return impulso["C"] > swing_h
    if fvg["type"] == "SELL" and lows:
        swing_l = min(l["price"] for l in lows[-2:])
        return impulso["C"] < swing_l
    return False


def zona_premium_discount(c5, fvg, nivel_barrido=None, lookback=15):
    """Compras solo en Discount (FVG por debajo del 50% del impulso),
    ventas solo en Premium (FVG por encima del 50%). El rango del
    impulso va desde el nivel barrido (o el swing mas cercano si no se
    detecto nivel institucional) hasta el extremo de la vela de impulso.
    Devuelve True/False, o None si no se pudo calcular (no penaliza)."""
    idx = fvg["idx"]
    if idx < 1:
        return None
    impulso = c5[idx - 1]

    if fvg["type"] == "BUY":
        start = nivel_barrido if nivel_barrido is not None else (
            min((c["L"] for c in c5[max(0, idx - lookback):idx]), default=None)
        )
        end = impulso["H"]
        if start is None or end <= start:
            return None
        mid = start + (end - start) * 0.5
        fvg_mid = (fvg["top"] + fvg["bottom"]) / 2
        return fvg_mid <= mid
    else:
        start = nivel_barrido if nivel_barrido is not None else (
            max((c["H"] for c in c5[max(0, idx - lookback):idx]), default=None)
        )
        end = impulso["L"]
        if start is None or end >= start:
            return None
        mid = start - (start - end) * 0.5
        fvg_mid = (fvg["top"] + fvg["bottom"]) / 2
        return fvg_mid >= mid


def ce_no_violado(candles, fvg, max_velas=3):
    """Regla del 50% (Consequent Encroachment): ninguna vela M5, desde
    que se formo el FVG, puede CERRAR mas alla del 50% del gap. Mecha
    esta permitida, cuerpo no. Si se viola, el FVG perdio su respeto
    algoritmico — bloqueo duro, igual criterio que retest_reacciono_o_atraveso."""
    idx = fvg["idx"]
    ce = (fvg["top"] + fvg["bottom"]) / 2
    posteriores = candles[idx:idx + max_velas + 1]
    for vela in posteriores:
        if fvg["type"] == "BUY" and vela["C"] < ce:
            return False
        if fvg["type"] == "SELL" and vela["C"] > ce:
            return False
    return True


def retest_reacciono_o_atraveso(candles, fvg, max_velas=3):
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

def detect_last_order_block(candles, direction, lookback=30):
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
    if not ob or atr <= 0:
        return False
    buffer = atr * tolerance_atr
    return (ob["bottom"] - buffer) <= price <= (ob["top"] + buffer)


def fvg_confluencia_cercana(candles, direction, current_price, atr, lookback=15):
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

OTE_LOW  = 0.62
OTE_HIGH = 0.79
OTE_GOLD = 0.705

def calc_ote(swing_high, swing_low, direction):
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
    if not ote or not price_in_ote(current_price, ote):
        return 0
    zone_size = ote["high"] - ote["low"]
    if zone_size <= 0:
        return 5
    dist = abs(current_price - ote["golden"])
    proximity = 1 - (dist / zone_size)
    return round(proximity * 15)

def confirmar_entrada_por_vela(candles, direccion):
    if len(candles) < 2:
        return False
    prev = candles[-2]
    curr = candles[-1]
    if direccion == "BUY":
        previa_bajista = prev["C"] < prev["O"]
        actual_alcista = curr["C"] > curr["O"]
        engulf         = curr["C"] > prev["O"]
        return previa_bajista and actual_alcista and engulf
    elif direccion == "SELL":
        previa_alcista = prev["C"] > prev["O"]
        actual_bajista = curr["C"] < curr["O"]
        engulf         = curr["C"] < prev["O"]
        return previa_alcista and actual_bajista and engulf
    return False

def score_morfologia_vela(candles, direccion):
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

def get_pair_trend(par, timeframe="M30", limit=60):
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
    votos_dolar_fuerte = 0
    pares_evaluados = 0

    for par in DOLLAR_PAIRS:
        trend = get_pair_trend(par)
        if trend is None:
            continue
        pares_evaluados += 1

        if par in DOLLAR_PAIRS_INVERSOS:
            if trend == "SELL":
                votos_dolar_fuerte += 1
        else:
            if trend == "BUY":
                votos_dolar_fuerte += 1

    if pares_evaluados < 3:
        return "NEUTRAL"

    if votos_dolar_fuerte >= 3:
        return "BUY"
    if votos_dolar_fuerte <= 1:
        return "SELL"
    return "NEUTRAL"


def dxy_score_bonus(signal_type, dxy_trend):
    if dxy_trend == "NEUTRAL":
        return 0, None

    confirma = (signal_type == "SELL" and dxy_trend == "BUY") or \
               (signal_type == "BUY" and dxy_trend == "SELL")

    if confirma:
        return DXY_SCORE_BONUS, f"USD {dxy_trend} confirma {signal_type} (+{DXY_SCORE_BONUS})"
    else:
        return -DXY_SCORE_BONUS, f"USD {dxy_trend} contradice {signal_type} (-{DXY_SCORE_BONUS})"

def señal_vigente(sig, precio_actual):
    atr       = sig.get("atr", 5)
    distancia = abs(precio_actual - sig["entry_price"])
    limite    = atr * 0.5
    vigente   = distancia <= limite
    if not vigente:
        print(f"  [VIGENCIA] Señal expirada — precio se alejó {distancia:.2f} pts (max {limite:.2f})")
    return vigente

# ── KILLZONE — BONO DE SCORE, YA NO BLOQUEO OBLIGATORIO ────────
# CAMBIO 2026-08-13: killzone_requerida() ya no bloquea las estrategias
# 1, 3 y 4 fuera de killzone — solo deja un aviso informativo en el
# log y SIEMPRE devuelve True. La killzone activa sigue sumando su
# bono de score normal (linea "if is_killzone(): score += N" dentro de
# cada estrategia), pero ahora es opcional, no obligatoria.
#
# Estrategia 2 (Killzone Breakout) NO usa esta funcion — sigue con su
# propio chequeo `if not is_killzone(): return None` al inicio, porque
# su logica completa depende de operar el rango de apertura de sesion.
#
# CAMBIO 2026-08-17: EMA Pullback M5 tiene ADEMAS su propia regla de
# killzone NYC (ver BLOCK_NYC_EMA_PULLBACK, NYC_MIN_SCORE_EMA_PULLBACK
# e is_nyc_killzone() arriba) — killzone_requerida() sigue sin bloquear
# nada por si sola, la regla de NYC vive dentro de
# strategy_ema_pullback(), evaluada como umbral de score al final de
# la funcion, no como bloqueo temprano.

def killzone_requerida(nombre_estrategia):
    """Ya no bloquea — deja aviso en el log cuando opera fuera de killzone."""
    if not is_killzone():
        print(f"  [{nombre_estrategia}] Fuera de Killzone — evaluando igual (killzone es bono de score, no bloqueo obligatorio)")
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

def publish_signal(sig):
    global daily_count, last_day, last_signal_time

    today = datetime.now(timezone.utc).date()
    if last_day != today:
        daily_count = 0
        last_day    = today

    if daily_count >= MAX_DAILY:
        print(f"  [SKIP] Límite diario {MAX_DAILY} señales alcanzado.")
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

    try:
        rows = get_candles("M5", 1)
        if rows:
            precio_actual = float(rows[0]["close"])
            if not señal_vigente(sig, precio_actual):
                return False
    except:
        pass

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

def strategy_scalping_m5(c5, c30, ch1, dxy_trend="NEUTRAL"):
    killzone_requerida("1-Scalping SMC")  # ya no bloquea, solo informa en log
    if en_blackout_de_noticias(buffer_minutos=NEWS_BLACKOUT_MINUTES_GENERAL):
        print("  [1] Scalping M5 SMC: descartado — blackout de noticias de alto impacto")
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

    vol_ok, vol_umbral = filtro_volatilidad_ok(atr)
    if not vol_ok:
        print(f"  [1] Scalping M5 SMC: descartado — volatilidad insuficiente (ATR={atr:.2f} < umbral={vol_umbral:.2f})")
        return None

    ema_dir = "BUY" if e9 > e20 else "SELL"

    def tf_dir(candles):
        if len(candles) < 10:
            return None
        cl = [c["C"] for c in candles]
        e9t = ema(cl, 9); e20t = ema(cl, 20)
        return "BUY" if (e9t and e20t and e9t > e20t) else "SELL"

    dir_m30 = tf_dir(c30)
    dir_h1  = tf_dir(ch1)

    highs, lows = detect_swing_hl(c5[-30:], lookback=2)
    if not highs or not lows:
        return None

    swing_h = max(h["price"] for h in highs[-3:])
    swing_l = min(l["price"] for l in lows[-3:])

    sweep_low  = last["L"] < swing_l and last["C"] > swing_l
    sweep_high = last["H"] > swing_h and last["C"] < swing_h

    ote_buy  = calc_ote(swing_h, swing_l, "BUY")
    ote_sell = calc_ote(swing_h, swing_l, "SELL")

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
        ote_bonus = ote_score_bonus(last["C"], ote_sell)
        if ote_bonus > 0:
            score += ote_bonus
            if price_in_ote(last["C"], ote_sell):
                reasons.append(f"OTE {ote_sell['golden']:.2f} (+{ote_bonus}pts)")

    if not sig_type:
        return None

    ventana_scalp = c5[-40:]

    ob = detect_last_order_block(ventana_scalp, sig_type)
    if price_near_ob(last["C"], ob, atr):
        score += 15
        reasons.append("Order Block confirmado")

    if fvg_confluencia_cercana(ventana_scalp, sig_type, last["C"], atr):
        score += 10
        reasons.append("FVG cercano confluencia")

    if sweep_rejection_fuerte(last, sig_type):
        score += 10
        reasons.append("Rechazo fuerte en sweep")
    else:
        score -= 10
        reasons.append("Sweep debil (mecha corta)")

    madura_m30 = tendencia_madura(c30)
    madura_h1  = tendencia_madura(ch1)
    if madura_m30:
        score += 5
        reasons.append("Tendencia M30 establecida")
    if madura_h1:
        score += 5
        reasons.append("Tendencia H1 establecida")

    if liquidez_sesion_confluencia(ventana_scalp, sig_type, last["C"]):
        score += 10
        reasons.append("Zona de liquidez de sesion")

    if score < MIN_SCORE:
        return None

    if not confirmar_entrada_por_vela(c5, sig_type):
        print(f"  [1] Scalping M5 SMC: {sig_type} descartado — sin confirmación de vela")
        return None

    morfo_bonus, morfo_reason = score_morfologia_vela(c5, sig_type)
    if morfo_bonus != 0:
        score += morfo_bonus
        if morfo_reason:
            reasons.append(morfo_reason)

    dxy_bonus, dxy_reason = dxy_score_bonus(sig_type, dxy_trend)
    if dxy_bonus != 0:
        score += dxy_bonus
        if dxy_reason:
            reasons.append(dxy_reason)

    score = max(0, min(score, 100))
    if score < MIN_SCORE:
        return None

    sl_pts = max(atr * 1.2, 5)
    ote_active = ote_buy if sig_type == "BUY" else ote_sell
    if price_in_ote(last["C"], ote_active):
        entry = ote_active["golden"]
    else:
        entry = last["C"]

    agotada, detalle_ext = extension_agotada(c5, sig_type, entry, atr)
    if agotada:
        return None

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

def strategy_killzone_breakout(c5, ch1, dxy_trend="NEUTRAL"):
    # NO se toco — sigue exigiendo killzone obligatoria por diseño.
    if not is_killzone():
        return None
    if en_blackout_de_noticias(buffer_minutos=NEWS_BLACKOUT_MINUTES_GENERAL):
        print("  [2] Killzone Breakout: descartado — blackout de noticias de alto impacto")
        return None
    if len(c5) < 20 or len(ch1) < 5:
        return None

    session = get_session()
    last    = c5[-1]
    atr     = calc_atr(c5[-20:])
    if atr < 0.5:
        return None

    pre_candles = c5[-7:-1]
    range_high  = max(c["H"] for c in pre_candles)
    range_low   = min(c["L"] for c in pre_candles)
    range_size  = range_high - range_low

    if range_size < atr * 0.5:
        return None

    breakout_up   = detect_orb_pullback(c5, range_high, range_low, "BUY")
    breakout_down = detect_orb_pullback(c5, range_high, range_low, "SELL")

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

    vwap = calc_session_vwap(c5)
    if vwap is not None:
        if sig_type == "BUY" and last["C"] > vwap:
            score += 10; reasons.append(f"VWAP {vwap:.2f} confirma BUY")
        elif sig_type == "SELL" and last["C"] < vwap:
            score += 10; reasons.append(f"VWAP {vwap:.2f} confirma SELL")
        else:
            score -= 10; reasons.append(f"VWAP {vwap:.2f} contradice {sig_type}")

    if not confirmar_entrada_por_vela(c5, sig_type):
        print(f"  [2] Killzone Breakout: {sig_type} descartado — sin confirmación de vela")
        return None

    morfo_bonus, morfo_reason = score_morfologia_vela(c5, sig_type)
    if morfo_bonus != 0:
        score += morfo_bonus
        if morfo_reason:
            reasons.append(morfo_reason)

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

    agotada, detalle_ext = extension_agotada(c5, sig_type, entry, atr)
    if agotada:
        return None

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

def strategy_fvg_fill(c5, c30, c15, dxy_trend="NEUTRAL"):
    killzone_requerida("3-FVG Fill")  # ya no bloquea, solo informa en log
    if en_blackout_de_noticias(buffer_minutos=NEWS_BLACKOUT_MINUTES_GENERAL):
        print("  [3] FVG Fill: descartado — blackout de noticias de alto impacto")
        return None
    if len(c5) < 30:
        return None

    atr  = calc_atr(c5[-20:])
    last = c5[-1]
    if atr < 0.5:
        return None

    vol_ok, vol_umbral = filtro_volatilidad_ok(atr)
    if not vol_ok:
        print(f"  [3] FVG Fill: descartado — volatilidad insuficiente (ATR={atr:.2f} < umbral={vol_umbral:.2f})")
        return None

    closes5 = [c["C"] for c in c5]
    e9  = ema(closes5, 9)
    e20 = ema(closes5, 20)
    if not e9 or not e20:
        return None
    trend = "BUY" if e9 > e20 else "SELL"

    ventana_fvg = c5[-40:]
    fvgs = detect_fvgs(ventana_fvg)
    if not fvgs:
        return None

    valid_fvgs = [f for f in fvgs if f["type"] == trend]
    if not valid_fvgs:
        return None

    fvg = valid_fvgs[-1]

    price_in_fvg_buy  = trend == "BUY"  and last["L"] <= fvg["top"]    and last["C"] >= fvg["bottom"]
    price_in_fvg_sell = trend == "SELL" and last["H"] >= fvg["bottom"] and last["C"] <= fvg["top"]

    if not price_in_fvg_buy and not price_in_fvg_sell:
        return None

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

    fvg_sweep_ok  = hubo_sweep_antes_del_fvg(ventana_fvg, fvg)
    fvg_bonus, fvg_reasons = score_validez_fvg(ventana_fvg, fvg, atr)
    fvg_retest_ok = retest_reacciono_o_atraveso(ventana_fvg, fvg)

    if fvg_retest_ok is False:
        print(f"  [3] FVG Fill: {sig_type} descartado — precio atraveso el FVG sin reaccionar (trampa)")
        return None

    # NUEVO 2026-08-18 (FVG A+, filtro 5 — bloqueo duro): regla del 50%
    # (Consequent Encroachment). Mas estricta que el retest de arriba
    # (que usa el FVG completo) — ninguna vela puede CERRAR mas alla
    # del punto medio del gap desde que se formo.
    if not ce_no_violado(ventana_fvg, fvg):
        print(f"  [3] FVG Fill: {sig_type} descartado — vela cerro mas alla del 50% CE del FVG (setup invalidado)")
        return None

    # NUEVO 2026-08-18 (FVG A+, filtro 1 — bono/penalizacion reforzada):
    # sweep sobre nivel institucional real (asia/PDH-PDL/equal highs-lows)
    # vale mas que el sweep generico que ya existia.
    inst_sweep_ok, inst_level_name = institutional_sweep_before_fvg(ventana_fvg, c15, fvg)
    if inst_sweep_ok:
        score += 20
        reasons.append(f"Sweep institucional de {inst_level_name} previo al FVG")
    elif fvg_sweep_ok:
        score += 10
        reasons.append("Sweep generico previo al FVG (no en nivel institucional clave)")
    else:
        score -= 10
        reasons.append("Sin sweep previo (FVG dudoso)")

    # NUEVO 2026-08-18 (FVG A+, filtro 2 — bono/penalizacion): MSS real,
    # no solo cuerpo grande (eso ya lo suma fvg_bonus mas abajo).
    nivel_barrido = None
    if inst_sweep_ok:
        _asia_h, _asia_l = get_asian_range(c15)
        _pdh, _pdl = get_pdh_pdl(c15)
        _eq_h, _eq_l = get_equal_levels(c5)
        candidatos = {"rango asiatico": _asia_l if sig_type == "BUY" else _asia_h,
                      "PDL": _pdl, "PDH": _pdh,
                      "equal lows": _eq_l, "equal highs": _eq_h}
        nivel_barrido = candidatos.get(inst_level_name)

    if mss_confirmado_fvg(ventana_fvg, fvg):
        score += 15
        reasons.append("MSS confirmado (ruptura real de estructura)")
    else:
        score -= 10
        reasons.append("Sin MSS confirmado (vela de impulso no rompio estructura)")

    # NUEVO 2026-08-18 (FVG A+, filtro 3 — bono/penalizacion): zona de
    # buen precio (Discount para compras, Premium para ventas).
    en_buena_zona = zona_premium_discount(ventana_fvg, fvg, nivel_barrido)
    if en_buena_zona is True:
        score += 15
        zona_nombre = "Discount" if sig_type == "BUY" else "Premium"
        reasons.append(f"FVG en zona {zona_nombre} (buen precio)")
    elif en_buena_zona is False:
        score -= 15
        zona_nombre = "Premium" if sig_type == "BUY" else "Discount"
        reasons.append(f"FVG en zona {zona_nombre} (precio caro, penalizado)")

    score += fvg_bonus
    reasons.extend(fvg_reasons)

    if score < MIN_SCORE:
        return None

    if not confirmar_entrada_por_vela(c5, sig_type):
        print(f"  [3] FVG Fill: {sig_type} descartado — sin confirmación de vela")
        return None

    morfo_bonus, morfo_reason = score_morfologia_vela(c5, sig_type)
    if morfo_bonus != 0:
        score += morfo_bonus
        if morfo_reason:
            reasons.append(morfo_reason)

    dxy_bonus, dxy_reason = dxy_score_bonus(sig_type, dxy_trend)
    if dxy_bonus != 0:
        score += dxy_bonus
        if dxy_reason:
            reasons.append(dxy_reason)

    score = max(0, min(score, 100))
    if score < MIN_SCORE:
        return None

    # NUEVO 2026-08-18 (FVG A+, filtro 5 — SL quirurgico): en vez de un
    # SL generico por ATR, se ubica detras del 50% (CE) del FVG — el
    # mismo nivel que ya usamos como invalidacion dura arriba. Si por
    # algun motivo da un riesgo absurdamente chico (FVG muy angosto),
    # se usa un piso minimo de ATR x0.4 para no quedar con un SL
    # pegado al precio.
    entry = last["C"]
    ce_level = (fvg["top"] + fvg["bottom"]) / 2
    CE_BUFFER = 0.15
    if sig_type == "BUY":
        sl_ce = ce_level - CE_BUFFER
        sl_pts_ce = entry - sl_ce
    else:
        sl_ce = ce_level + CE_BUFFER
        sl_pts_ce = sl_ce - entry

    if sl_pts_ce > 0:
        sl_pts = max(sl_pts_ce, atr * 0.4)
        reasons.append(f"SL quirurgico en 50% CE del FVG ({ce_level:.2f})")
    else:
        sl_pts = max(atr * 1.2, 5)

    agotada, detalle_ext = extension_agotada(c5, sig_type, entry, atr)
    if agotada:
        return None

    es_spike, detalle_spike = spike_reciente(c5, atr)
    if es_spike:
        return None

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

def strategy_ema_pullback(c5, c30, ch1, dxy_trend="NEUTRAL"):
    killzone_requerida("4-EMA Pullback")  # ya no bloquea, solo informa en log

    if en_blackout_de_noticias(buffer_minutos=NEWS_BLACKOUT_MINUTES_GENERAL):
        print("  [4] EMA Pullback: descartado — blackout de noticias de alto impacto")
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

    vol_ok, vol_umbral = filtro_volatilidad_ok(atr)
    if not vol_ok:
        print(f"  [4] EMA Pullback: descartado — volatilidad insuficiente (ATR={atr:.2f} < umbral={vol_umbral:.2f})")
        return None

    trend = "BUY" if e9 > e20 else "SELL"

    strong_trend = False
    if e50:
        strong_trend = (trend == "BUY"  and e9 > e20 > e50) or \
                       (trend == "SELL" and e9 < e20 < e50)

    last = c5[-1]
    prev = c5[-2]

    ema_zone_top = max(e9, e20) + atr * 0.1
    ema_zone_bot = min(e9, e20) - atr * 0.1

    touched_ema_buy  = trend == "BUY"  and prev["L"] <= ema_zone_top and last["C"] > e9
    touched_ema_sell = trend == "SELL" and prev["H"] >= ema_zone_bot and last["C"] < e9

    if not touched_ema_buy and not touched_ema_sell:
        return None

    last_body = abs(last["C"] - last["O"])
    last_range = last["H"] - last["L"]
    strong_candle = last_body > last_range * 0.5

    if not strong_candle:
        return None

    # NUEVO 2026-08-18: filtro ADX obligatorio (anti-rango/whipsaw).
    # El historico real (killzone NYC: 1W/9L, -$130.46) tiene la firma
    # clasica de un cruce de EMAs operando en lateral — ATR ya filtra
    # volatilidad pero no distingue movimiento direccional de ruido.
    # AJUSTE 2026-08-18 (v2): exigir fuerza minima Y pendiente positiva
    # a la vez, en oro/M5, dejaba la estrategia casi sin señales. Se
    # mantiene el umbral de fuerza como bloqueo duro (es lo que evita
    # el rango real) y "subiendo" pasa a ser bono de score, no rechazo.
    adx, adx_subiendo = calc_adx(c5, period=ADX_PERIOD)
    if adx is None:
        return None
    if adx < ADX_MIN_TREND:
        print(f"  [4] EMA Pullback: {trend} descartado — ADX {adx:.1f} < {ADX_MIN_TREND} (mercado en rango)")
        return None

    # NUEVO 2026-08-18: filtro VWAP direccional obligatorio.
    # Solo BUY con precio sobre el VWAP de sesion, solo SELL por debajo.
    # Si no hay sesion activa (fuera de killzone), calc_session_vwap
    # devuelve None y el filtro se omite (no bloquea).
    vwap = calc_session_vwap(c5)
    if vwap is not None:
        if trend == "BUY" and last["C"] <= vwap:
            print(f"  [4] EMA Pullback: BUY descartado — precio {last['C']:.2f} bajo VWAP {vwap:.2f}")
            return None
        if trend == "SELL" and last["C"] >= vwap:
            print(f"  [4] EMA Pullback: SELL descartado — precio {last['C']:.2f} sobre VWAP {vwap:.2f}")
            return None

    def tf_dir(candles):
        if len(candles) < 10: return None
        cl = [c["C"] for c in candles]
        e9t = ema(cl, 9); e20t = ema(cl, 20)
        return "BUY" if (e9t and e20t and e9t > e20t) else "SELL"

    dir_m30 = tf_dir(c30)
    dir_h1  = tf_dir(ch1)

    # NUEVO 2026-08-17: M30 + H1 alineados ahora es FILTRO OBLIGATORIO,
    # no bono. Antes sumaban +15 c/u pero no bloqueaban — el score se
    # saturaba en 90-100 igual con o sin alineacion real de timeframes
    # mayores, y esa falta de discriminacion coincide con el patron de
    # perdidas del historico real.
    if dir_m30 != trend or dir_h1 != trend:
        print(f"  [4] EMA Pullback: {trend} descartado — M30({dir_m30}) o H1({dir_h1}) no alineados")
        return None

    score    = 0
    reasons  = []
    sig_type = trend

    score += 30; reasons.append(f"Pullback EMA {trend}")
    if strong_trend: score += 15; reasons.append("Tendencia fuerte")
    score += 15; reasons.append("M30 confirma")
    score += 15; reasons.append("H1 confirma")
    if strong_candle:    score += 10; reasons.append("Vela confirmación")
    if is_killzone():    score += 10; reasons.append("Killzone activa")
    if trend == "BUY"  and rsi < 65: score += 5; reasons.append(f"RSI {rsi}")
    if trend == "SELL" and rsi > 35: score += 5; reasons.append(f"RSI {rsi}")
    if adx_subiendo:
        score += 8
        reasons.append(f"ADX {adx:.1f} subiendo (> {ADX_MIN_TREND})")
    else:
        reasons.append(f"ADX {adx:.1f} estable/bajando (> {ADX_MIN_TREND} igual)")
    if vwap is not None:
        reasons.append(f"VWAP {vwap:.2f} confirma sesgo {trend}")

    if score < MIN_SCORE:
        return None

    if not confirmar_entrada_por_vela(c5, sig_type):
        print(f"  [4] EMA Pullback: {sig_type} descartado — sin confirmación de vela")
        return None

    morfo_bonus, morfo_reason = score_morfologia_vela(c5, sig_type)
    if morfo_bonus != 0:
        score += morfo_bonus
        if morfo_reason:
            reasons.append(morfo_reason)

    dxy_bonus, dxy_reason = dxy_score_bonus(sig_type, dxy_trend)
    if dxy_bonus != 0:
        score += dxy_bonus
        if dxy_reason:
            reasons.append(dxy_reason)

    score = max(0, min(score, 100))
    if score < MIN_SCORE:
        return None

    # CAMBIO 2026-08-17 (v2): el bloqueo duro de killzone NYC se
    # reemplaza por un umbral de score mas exigente SOLO en esa
    # ventana. Historico real: 10 trades en NYC = 1W/9L, -$130.46
    # (96% de la perdida total de la estrategia). En vez de excluir
    # NYC por completo, se permite operar ahi unicamente si la señal
    # es de muy alta calidad (score >= NYC_MIN_SCORE_EMA_PULLBACK,
    # 90 por defecto) — el resto de la ventana (score 75-89) se sigue
    # descartando, que es donde vivia casi toda la perdida historica.
    if BLOCK_NYC_EMA_PULLBACK and is_nyc_killzone() and score < NYC_MIN_SCORE_EMA_PULLBACK:
        print(
            f"  [4] EMA Pullback: {sig_type} descartado en killzone NYC — "
            f"score {score} < {NYC_MIN_SCORE_EMA_PULLBACK} requerido en esta ventana "
            f"(winrate historico 10% en NYC, solo se permite con score muy alto)"
        )
        return None

    sl_pts = max(atr * 1.1, 4)
    entry  = last["C"]

    agotada, detalle_ext = extension_agotada(c5, sig_type, entry, atr)
    if agotada:
        return None

    es_spike, detalle_spike = spike_reciente(c5, atr)
    if es_spike:
        return None

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

# ════════════════════════════════════════════════════════════════
# ESTRATEGIA 5: Institutional Sweep & Displacement Scalp (M1)
# ════════════════════════════════════════════════════════════════
# Contexto (15m/5m): barrido de un nivel clave de liquidez (rango
# asiatico, PDH/PDL, equal highs/lows).
# Confirmacion (1m): mecha de rechazo + Market Structure Shift (MSS)
# con vela de desplazamiento (cuerpo grande vs ATR) + spike de volumen.
# Entrada: NO en el breakout — se espera el retroceso a un FVG
# generado por el desplazamiento, o al VWAP de sesion, con la primera
# señal de rechazo confirmada por vela.
# Salidas: SL detras del extremo barrido + buffer; TP1/TP2 en
# multiplos del riesgo (mismo estilo que las demas estrategias —
# TP1 es el unico TP real que ejecuta bot_engine.py en MT5; TP2 queda
# como referencia y el "correr la posicion" lo hace el trailing por
# ATR que ya existe en bot_engine.py, igual que en las otras 4).
# Horario: SOLO aperturas de Londres/NY (ventana mas angosta que la
# killzone general del resto de estrategias) — es un filtro OBLIGATORIO,
# no un bono, porque la logica de "caza de stops institucional" pierde
# sentido fuera de esas aperturas de alta liquidez.
# ════════════════════════════════════════════════════════════════

SWEEP_SL_BUFFER = 0.30            # "1-2 pips" detras del extremo barrido, en precio de GOLD
SWEEP_MIN_DISPLACEMENT_ATR = 1.2  # cuerpo de la vela de desplazamiento vs ATR(14) M1
SWEEP_VOLUME_MULT = 1.5           # volumen de la vela de rebote vs promedio de las ultimas 20 velas M1
SWEEP_SWING_LOOKBACK = 8          # velas M1 hacia atras para el swing menor pre-impulso
SWEEP_EQUAL_TOLERANCE = 0.30      # tolerancia en precio para equal highs/lows (M5)
SWEEP_ENTRY_TOLERANCE_ATR = 0.5   # tolerancia (en ATR M1) para considerar precio "en" el FVG o VWAP

# Noticias de alto impacto — integracion opcional con news_engine.py.
# Import protegido: si el modulo o la funcion no existen todavia, el
# filtro de noticias simplemente se desactiva (no rompe el motor).
# NUEVO 2026-08-18: antes SOLO la Estrategia 5 (Sweep Displacement M1)
# usaba este blackout. Se globaliza a las 4 estrategias restantes
# (Scalping M5 SMC, Killzone Breakout, FVG Fill M5, EMA Pullback M5)
# con una ventana mas amplia (15 min) porque son menos sensibles al
# milisegundo que el sweep en M1, pero igual de vulnerables al spike
# de volatilidad y spread que genera una noticia de alto impacto.
NEWS_BLACKOUT_MINUTES_GENERAL = 15
try:
    from news_engine import get_high_impact_news_times
    NEWS_FILTER_AVAILABLE = True
except Exception:
    get_high_impact_news_times = None
    NEWS_FILTER_AVAILABLE = False


def is_sweep_killzone():
    """Ventana propia y mas angosta que is_killzone(): Londres 08:00-11:00 UTC
    y NY 13:30-16:30 UTC == 04:00-07:00 RD y 09:30-12:30 RD."""
    now = datetime.now(timezone.utc)
    rdh = ((now.hour - 4) + 24) % 24
    t   = rdh * 100 + now.minute
    return (400 <= t < 700) or (930 <= t < 1230)


def en_blackout_de_noticias(buffer_minutos=5):
    if not NEWS_FILTER_AVAILABLE:
        return False
    try:
        eventos = get_high_impact_news_times()  # se espera lista de datetime UTC
        now = datetime.now(timezone.utc)
        return any(abs((now - ev).total_seconds()) <= buffer_minutos * 60 for ev in eventos)
    except Exception as e:
        print(f"  [5] Aviso: no se pudo evaluar blackout de noticias ({e}) — filtro omitido este ciclo")
        return False


def get_asian_range(c15):
    """Rango de la sesion asiatica (00:00-08:00 UTC) del dia actual."""
    hoy = datetime.now(timezone.utc).date()
    sesion = []
    for c in c15:
        try:
            ct = datetime.fromisoformat(c["time"].replace("Z", "+00:00"))
        except Exception:
            continue
        if ct.date() == hoy and 0 <= ct.hour < 8:
            sesion.append(c)
    if not sesion:
        return None, None
    return max(c["H"] for c in sesion), min(c["L"] for c in sesion)


def get_pdh_pdl(c15):
    """Maximo/minimo del dia UTC anterior, usando velas M15."""
    ayer = datetime.now(timezone.utc).date() - timedelta(days=1)
    sesion = []
    for c in c15:
        try:
            ct = datetime.fromisoformat(c["time"].replace("Z", "+00:00"))
        except Exception:
            continue
        if ct.date() == ayer:
            sesion.append(c)
    if not sesion:
        return None, None
    return max(c["H"] for c in sesion), min(c["L"] for c in sesion)


def get_equal_levels(c5, lookback=40, tolerance=SWEEP_EQUAL_TOLERANCE):
    """Equal highs / equal lows recientes en M5 (aproximacion: dos
    maximos o minimos dentro de `tolerance` en precio)."""
    highs, lows = detect_swing_hl(c5[-lookback:], lookback=2)
    eq_high, eq_low = None, None
    for i in range(len(highs)):
        for j in range(i + 1, len(highs)):
            if abs(highs[i]["price"] - highs[j]["price"]) <= tolerance:
                eq_high = max(highs[i]["price"], highs[j]["price"])
                break
        if eq_high:
            break
    for i in range(len(lows)):
        for j in range(i + 1, len(lows)):
            if abs(lows[i]["price"] - lows[j]["price"]) <= tolerance:
                eq_low = min(lows[i]["price"], lows[j]["price"])
                break
        if eq_low:
            break
    return eq_high, eq_low


def find_minor_swing_m1(c1, direction, lookback=SWEEP_SWING_LOOKBACK):
    """Swing high/low menor en M1, excluyendo la ultima vela (el impulso)."""
    ventana = c1[-(lookback + 1):-1]
    if len(ventana) < 3:
        return None
    if direction == "BUY":
        return max(c["H"] for c in ventana)
    return min(c["L"] for c in ventana)


def detect_sweep_mss_m1(c1, key_level, direction, atr1, search_window=20):
    """Busca en una ventana hacia atras (excluyendo la vela actual, que se
    asume parte del retroceso/entrada) la vela de DESPLAZAMIENTO que:
      - viene precedida (1-3 velas antes) de un barrido del `key_level`
        con cierre de rechazo (rejection) de vuelta,
      - rompe el swing menor previo (MSS),
      - tiene cuerpo >= ATR x SWEEP_MIN_DISPLACEMENT_ATR,
      - tiene volumen >= promedio x SWEEP_VOLUME_MULT.
    Devuelve el evento MAS RECIENTE que cumple todo (no necesariamente la
    ultima vela del array), para poder evaluar el retroceso por separado."""
    n = len(c1)
    start = max(SWEEP_SWING_LOOKBACK + 1, n - search_window)

    # Recorre de la vela mas reciente hacia atras, EXCLUYENDO la ultima
    # (se asume que la ultima vela es el retroceso/entrada en curso, no
    # el impulso de desplazamiento en si).
    for idx in range(n - 2, start - 1, -1):
        candle = c1[idx]
        body = abs(candle["C"] - candle["O"])
        if atr1 <= 0 or body < SWEEP_MIN_DISPLACEMENT_ATR * atr1:
            continue

        prev_window = c1[max(0, idx - 20):idx]
        avg_vol = sum(c["V"] for c in prev_window) / len(prev_window) if prev_window else candle["V"]
        vol_spike = avg_vol > 0 and candle["V"] >= SWEEP_VOLUME_MULT * avg_vol
        if not vol_spike:
            continue

        swing = find_minor_swing_m1(c1[:idx + 1], direction)
        if swing is None:
            continue

        # El barrido puede ser la propia vela de desplazamiento o alguna
        # de las 1-2 velas justo antes (mecha de rechazo previa al impulso).
        ventana_sweep = c1[max(0, idx - 2):idx + 1]

        if direction == "BUY":
            swept_candidates = [c for c in ventana_sweep if c["L"] < key_level]
            if not swept_candidates:
                continue
            sweep_candle = min(swept_candidates, key=lambda c: c["L"])
            rejection = sweep_candle["C"] > key_level
            mss = candle["C"] > swing and candle["C"] > candle["O"]
            swept_extreme = sweep_candle["L"]
        else:
            swept_candidates = [c for c in ventana_sweep if c["H"] > key_level]
            if not swept_candidates:
                continue
            sweep_candle = max(swept_candidates, key=lambda c: c["H"])
            rejection = sweep_candle["C"] < key_level
            mss = candle["C"] < swing and candle["C"] < candle["O"]
            swept_extreme = sweep_candle["H"]

        if rejection and mss:
            return {
                "valid": True,
                "swept_extreme": swept_extreme,
                "displacement_idx": idx,
                "checks": {
                    "swept": True, "rejection": True, "mss": True,
                    "displacement": True, "vol_spike": True,
                },
            }

    return {"valid": False}


def precio_en_zona_entrada(candles_recientes, fvg, vwap, atr1, direction):
    """El rechazo hace que el CIERRE de la vela de confirmacion salga de la
    zona (es justamente lo que confirma el rechazo) — lo que debe tocar la
    zona es la MECHA (high/low) de alguna de las ultimas 2 velas, igual que
    strategy_fvg_fill ya hace con sus propios FVGs."""
    tol = atr1 * SWEEP_ENTRY_TOLERANCE_ATR if atr1 > 0 else 0.5
    recientes = candles_recientes[-2:]

    if fvg and fvg["type"] == direction:
        zona_lo, zona_hi = fvg["bottom"] - tol, fvg["top"] + tol
        if direction == "BUY":
            tocada = any(c["L"] <= zona_hi and c["C"] >= zona_lo for c in recientes)
        else:
            tocada = any(c["H"] >= zona_lo and c["C"] <= zona_hi for c in recientes)
        if tocada:
            return True, f"FVG {direction} en {fvg['bottom']:.2f}-{fvg['top']:.2f}"

    if vwap is not None:
        if direction == "BUY" and any(c["L"] <= vwap + tol and c["C"] >= vwap - tol for c in recientes):
            return True, f"VWAP sesion {vwap:.2f}"
        if direction == "SELL" and any(c["H"] >= vwap - tol and c["C"] <= vwap + tol for c in recientes):
            return True, f"VWAP sesion {vwap:.2f}"

    return False, ""


def strategy_sweep_displacement(c15, c5, dxy_trend="NEUTRAL"):
    """Estrategia 5: Institutional Sweep & Displacement Scalp.
    Contexto en c15/c5, confirmacion y entrada en M1 (fetch propio)."""

    # ── Filtro duro de horario: SOLO aperturas Londres/NY ──
    if not is_sweep_killzone():
        return None

    # ── Filtro duro de noticias de alto impacto (±5 min) ──
    if en_blackout_de_noticias(buffer_minutos=5):
        print("  [5] Sweep Displacement: descartado — blackout de noticias de alto impacto")
        return None

    if len(c15) < 20 or len(c5) < 40:
        return None

    rows_m1 = get_candles("M1", 60)
    if len(rows_m1) < SWEEP_SWING_LOOKBACK + 5:
        return None
    c1 = to_candles(rows_m1)

    atr1 = calc_atr(c1, period=14)
    if atr1 <= 0:
        return None

    vwap = calc_session_vwap(c5)
    last_price = c1[-1]["C"]

    asia_high, asia_low = get_asian_range(c15)
    pdh, pdl = get_pdh_pdl(c15)
    eq_high, eq_low = get_equal_levels(c5)

    niveles_compra = [("rango asiatico", asia_low), ("PDL", pdl), ("equal lows", eq_low)]
    niveles_venta  = [("rango asiatico", asia_high), ("PDH", pdh), ("equal highs", eq_high)]

    candidato = None  # (direction, level_name, level, result)

    for level_name, level in niveles_compra:
        if level is None:
            continue
        result = detect_sweep_mss_m1(c1, level, "BUY", atr1)
        if result.get("valid"):
            candidato = ("BUY", level_name, level, result)
            break

    if not candidato:
        for level_name, level in niveles_venta:
            if level is None:
                continue
            result = detect_sweep_mss_m1(c1, level, "SELL", atr1)
            if result.get("valid"):
                candidato = ("SELL", level_name, level, result)
                break

    if not candidato:
        return None

    sig_type, level_name, level, result = candidato
    disp_idx = result["displacement_idx"]

    # ── Entrada: esperar retroceso al FVG generado por el desplazamiento, o al VWAP ──
    fvgs = detect_fvgs(c1)
    fvg = next(
        (f for f in fvgs if f["type"] == sig_type and abs(f["idx"] - disp_idx) <= 1),
        None,
    )
    en_zona, zona_desc = precio_en_zona_entrada(c1, fvg, vwap, atr1, sig_type)
    if not en_zona:
        return None

    # ── Confirmacion de vela en el retroceso (misma logica que las otras 4) ──
    if not confirmar_entrada_por_vela(c1, sig_type):
        print(f"  [5] Sweep Displacement: {sig_type} descartado — sin confirmación de vela en el retroceso")
        return None

    checks = result["checks"]
    score = 0
    reasons = [f"Sweep de {level_name} ({level:.2f})"]
    score += 20; reasons.append("Rechazo tras el barrido")
    score += 20; reasons.append("MSS confirmado en M1")
    score += 15; reasons.append("Desplazamiento fuerte (cuerpo >= ATR x1.2)")
    score += 15; reasons.append("Volumen en spike")
    score += 15; reasons.append(f"Entrada en retroceso: {zona_desc}")
    score += 5;  reasons.append("Killzone Londres/NY (apertura)")

    morfo_bonus, morfo_reason = score_morfologia_vela(c1, sig_type)
    if morfo_bonus != 0:
        score += morfo_bonus
        if morfo_reason:
            reasons.append(morfo_reason)

    dxy_bonus, dxy_reason = dxy_score_bonus(sig_type, dxy_trend)
    if dxy_bonus != 0:
        score += dxy_bonus
        if dxy_reason:
            reasons.append(dxy_reason)

    score = max(0, min(score, 100))
    if score < MIN_SCORE:
        return None

    swept_extreme = result["swept_extreme"]
    entry = last_price

    if sig_type == "BUY":
        sl = swept_extreme - SWEEP_SL_BUFFER
        sl_pts = entry - sl
        if sl_pts <= 0:
            return None
        tp1 = entry + sl_pts * 1.5
        tp2 = entry + sl_pts * 3
    else:
        sl = swept_extreme + SWEEP_SL_BUFFER
        sl_pts = sl - entry
        if sl_pts <= 0:
            return None
        tp1 = entry - sl_pts * 1.5
        tp2 = entry - sl_pts * 3

    agotada, detalle_ext = extension_agotada(c1, sig_type, entry, atr1)
    if agotada:
        return None

    return {
        "signal_type":   sig_type,
        "entry_price":   entry,
        "stop_loss":     sl,
        "take_profit_1": tp1,
        "take_profit_2": tp2,
        "confidence":    score,
        "strategy":      "Sweep Displacement M1",
        "timeframe":     "M1",
        "atr":           atr1,
        "reasons":       reasons,
        "candle_time":   c1[-1]["time"],
    }


def analyze():
    now_str = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{now_str}] Analizando mercado...")

    rows_m5  = get_candles("M5",  100)
    rows_m30 = get_candles("M30", 60)
    rows_h1  = get_candles("H1",  60)
    rows_m15 = get_candles("M15", 250)  # NUEVO: reutilizado por AI Elite y por Sweep Displacement M1

    if not rows_m5:
        print("  Sin velas M5 disponibles.")
        return

    c5  = to_candles(rows_m5)
    c30 = to_candles(rows_m30)
    ch1 = to_candles(rows_h1)
    c15 = to_candles(rows_m15)

    dxy_trend = get_dxy_trend()

    last_price = c5[-1]["C"]
    session    = get_session()
    kz         = "KZ ACTIVA" if is_killzone() else "fuera de KZ"
    print(f"  Precio: {last_price:.2f} | Sesión: {session} | {kz} | Señales hoy: {daily_count}/{MAX_DAILY}")
    print(f"  Dolar (DXY sintetico): {dxy_trend}")

    signals_found = 0

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

    try:
        sig = strategy_fvg_fill(c5, c30, c15, dxy_trend)
        if sig:
            if publish_signal(sig):
                signals_found += 1
            else:
                print(f"  [3] FVG Fill: señal detectada pero no publicada")
        else:
            print(f"  [3] FVG Fill M5: sin FVG activo")
    except Exception as e:
        print(f"  [3] Error FVG Fill: {e}")

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

    try:
        sig = strategy_sweep_displacement(c15, c5, dxy_trend)
        if sig:
            if publish_signal(sig):
                signals_found += 1
            else:
                print(f"  [5] Sweep Displacement: señal detectada pero no publicada")
        else:
            print(f"  [5] Sweep Displacement M1: sin setup (fuera de killzone, sin sweep+MSS, o sin retest a FVG/VWAP)")
    except Exception as e:
        print(f"  [5] Error Sweep Displacement: {e}")

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
    print(f"    2. Killzone Breakout (London/NY) con Pullback + VWAP")
    print(f"    3. FVG Fill M5 (con filtro anti-trampa institucional)")
    print(f"    4. EMA Pullback M5 (killzone NYC exige score >= {NYC_MIN_SCORE_EMA_PULLBACK}, M30+H1 obligatorio)")
    print(f"    5. Sweep Displacement M1 (barrido + MSS + retroceso a FVG/VWAP, killzone Londres/NY obligatoria)")
    print(f"  ICT OTE: Golden Pocket 70.5% | Zona 62-79% Fibonacci")
    print(f"  Filtro DXY sintetico: {', '.join(DOLLAR_PAIRS)} (bono/penalizacion +/-{DXY_SCORE_BONUS}pts)")
    print(f"  Filtro FVG anti-trampa: sweep previo + momentum impulso + retest")
    print(f"  Filtro volatilidad ATR: umbral {ATR_PROMEDIO_HISTORICO * ATR_MIN_MULTIPLIER:.2f} (hist={ATR_PROMEDIO_HISTORICO} x mult={ATR_MIN_MULTIPLIER})")
    print(f"  Killzone: BONO de score en estrategias 1/3 (ya no bloqueo obligatorio) | Estrategia 2 SIGUE exigiendo killzone")
    print(f"  EMA Pullback M5: killzone NYC exige score >= {NYC_MIN_SCORE_EMA_PULLBACK} (BLOCK_NYC_EMA_PULLBACK={BLOCK_NYC_EMA_PULLBACK}) + M30/H1 obligatorio")
    print(f"{'='*55}\n")

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: Faltan SUPABASE_URL o SUPABASE_KEY en .env")
        sys.exit(1)

    if TELEGRAM_TOKEN:
        ok = send_telegram(
            f"Trading Pro XAUUSD — Signal Engine INICIADO\n"
            f"Score min: {MIN_SCORE} | Max diario: {MAX_DAILY}\n"
            f"Cooldown: {SIGNAL_COOLDOWN}s | Loop: {LOOP_INTERVAL}s\n"
            f"Filtro DXY sintetico activo ({', '.join(DOLLAR_PAIRS)})\n"
            f"Filtro FVG anti-trampa activo\n"
            f"Filtro volatilidad ATR activo (umbral {ATR_PROMEDIO_HISTORICO * ATR_MIN_MULTIPLIER:.2f})\n"
            f"Killzone: bono de score en 1/3, ya no bloqueo obligatorio\n"
            f"EMA Pullback M5: killzone NYC exige score >= {NYC_MIN_SCORE_EMA_PULLBACK} + M30/H1 obligatorio\n"
            f"Hora: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        if ok:
            print("  [TELEGRAM] Conectado OK — mensaje de inicio enviado")
        else:
            print("  [TELEGRAM] ERROR — no se pudo enviar mensaje de prueba")
            print(f"  Token: {TELEGRAM_TOKEN[:10]}... | Chat: {TELEGRAM_CHAT_ID}")
    else:
        print("  [TELEGRAM] Sin TELEGRAM_TOKEN — notificaciones desactivadas")

    while True:
        try:
            analyze()
        except Exception as e:
            print(f"[ERROR] analyze: {e}")
        print(f"  Próximo análisis en {LOOP_INTERVAL}s...")
        time.sleep(LOOP_INTERVAL)

if __name__ == "__main__":
    main()
