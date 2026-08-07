"""
bot_engine.py
=============
Ejecuta ordenes reales en MT5 basado en señales PENDING de Supabase.

TradingProEA - Execution Engine

Reglas:
- Score minimo: 75
- Lotaje: FIJO por defecto (ver USE_FIXED_LOT/FIXED_LOT_SIZE), o dinamico
  segun balance real y % de riesgo (position_sizing.py) si USE_FIXED_LOT=false
- Vigencia: rechaza señales de mas de 10 min o con precio ya movido (anti señal-vieja)
- Maximo N perdidas por dia (MAX_LOSSES_PER_DAY, real, leido del historial de MT5)
  — protege la cuenta si se deja corriendo sin supervision
- NUEVO: Maximo 2 perdidas POR KILLZONE (Londres 3-6 AM RD / NYC 9-12 PM RD),
  independiente del limite diario general — protege cada sesion por separado
- SL anti-hunt: 20 puntos extra
- Maximo 1 operacion abierta a la vez
- Maximo 6 operaciones por dia (configurable via MAX_DAILY en .env)
- Solo ejecuta estrategias permitidas
- Notifica a Telegram al abrir

ADVERTENCIA:
Este script ejecuta ordenes REALES en MT5.
Usalo solo en la PC donde MetaTrader 5 este abierto y conectado.

CAMBIOS EN ESTA VERSION (breakeven real al 70% del camino a TP1):
- NUEVO: mientras hay una posicion real abierta, si el precio ya
  recorrio BREAKEVEN_TRIGGER_PCT (70% por defecto) de la distancia
  entre la entrada y el TP1, el bot mueve el SL real en MT5 al precio
  de entrada (TRADE_ACTION_SLTP). A partir de ahi, lo peor que puede
  pasar en esa posicion es cerrar en $0 — nunca convierte una
  operacion que iba ganando en perdida.
- Se aplica una sola vez por posicion (rastreado en memoria via
  _breakeven_applied) para no reenviar la misma modificacion en cada
  ciclo. Se aplica a TODAS las estrategias, no solo a las que tienen
  gestion de salida por CHoCH — es proteccion de riesgo general.
- Antes de este cambio, el breakeven SOLO existia en la simulacion de
  result_tracker.py (para estimar resultados de señales), nunca se
  aplicaba de verdad sobre la posicion real en la cuenta.

CAMBIOS EN ESTA VERSION (gestion de salida por cambio de estructura):
- NUEVO: mientras hay una posicion real abierta de una estrategia marcada
  en EARLY_EXIT_STRATEGIES (por ahora solo "EMA Pullback M5"), cada ciclo
  el bot revisa las velas M5 recientes de Supabase buscando un CHoCH
  (cambio de estructura) en contra de la direccion de la posicion abierta.
  Si lo detecta, cierra la posicion completa a mercado de inmediato en vez
  de esperar a que toque SL o TP1, y avisa por Telegram con el motivo y el
  profit con el que cerro.
- La deteccion de CHoCH es la MISMA logica que ya usa signal_engine.py en
  la Estrategia 1 (Scalping M5 SMC): compara maximos/minimos de las
  ultimas 5 velas M5 contra las 5 anteriores, sobre una ventana de 20
  velas. Se replica aqui tal cual (ver detect_choch) para que el criterio
  de salida no contradiga el criterio con el que se genero la señal.
- Esto vive dentro de bot_engine.py (no en un proceso aparte) porque
  run_cycle() ya revisa posiciones abiertas en cada ciclo — es el punto
  natural para engancharlo, sin abrir una segunda conexion a MT5.

CAMBIOS EN ESTA VERSION (limite de perdidas por killzone):
- Antes MAX_LOSSES_PER_DAY paraba el bot para TODO el dia apenas se
  alcanzaban N perdidas, sin distinguir si esas perdidas fueron todas
  en una sola sesion (ej. Londres de madrugada) o repartidas.
- Ahora, ademas del limite diario general (que se mantiene igual, como
  red de seguridad de todo el dia), se agrega un limite independiente
  de MAX_LOSSES_PER_KILLZONE (2 por defecto) que solo se evalua cuando
  la hora actual cae dentro de la killzone de Londres (3:00-6:00 RD) o
  la de NYC (9:00-12:00 RD). Si una killzone llega a su limite, el bot
  deja de operar SOLO durante esa ventana — si luego entra la otra
  killzone, se evalua por separado desde cero.
- Fuera de ambas killzones sigue rigiendo unicamente el limite diario
  general (MAX_LOSSES_PER_DAY).

CAMBIOS EN VERSION ANTERIOR (fix: auto-limpieza de señales vencidas):
- Antes, cuando una señal PENDING se descartaba por vigencia (mas de
  MAX_SIGNAL_AGE_MINUTES o con el precio ya movido), el bot solo la
  ignoraba EN MEMORIA (imprimia "[VIGENCIA] ... descartada" y seguia).
  Nunca actualizaba su estado en Supabase, asi que la señal se quedaba
  PENDING para siempre y se volvia a revisar (y descartar) en cada ciclo,
  acumulandose sin limite.
- Ahora, apenas se detecta que una señal esta vencida, se marca como
  "EXPIRED" en Supabase de una vez (misma funcion update_signal_status
  que ya se usaba para FAILED/EXECUTING). Asi la cola de PENDING se
  autolimpia sola y no vuelve a aparecer en la siguiente consulta.

CAMBIOS EN VERSION ANTERIOR (parche de diagnostico + robustez de notificaciones):
- Cuando una orden falla (order_send devuelve None o retcode != DONE), ahora
  se captura el motivo exacto (retcode, comment, last_error de MT5) y:
    1. se guarda en bot_errors.log (no se pierde aunque cierres la consola)
    2. se envia por Telegram
  Antes ese detalle solo se imprimia en consola y se perdia.
- send_telegram() y update_signal_status() ahora reintentan hasta 3 veces
  ante fallos de red antes de rendirse. Si aun asi fallan las 3 veces, el
  detalle completo (mensaje de Telegram, o señal + estado que no se pudo
  guardar en Supabase) se registra en bot_errors.log. Antes, un solo corte
  de red momentaneo hacia que un aviso de "ORDEN ABIERTA" desapareciera sin
  dejar rastro, o que una señal ya ejecutada en MT5 se quedara marcada
  PENDING en Supabase para siempre.
"""

import os
import sys
import time
import requests
from datetime import datetime, date, time as dtime
from dotenv import load_dotenv

try:
    import MetaTrader5 as mt5
except ImportError:
    print("ERROR: Instala MetaTrader5: pip install MetaTrader5")
    sys.exit(1)

from position_sizing import calculate_lot_size

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@XAUUSD_Signals_DR")
MT5_SYMBOL = os.getenv("MT5_SYMBOL", "GOLD")

# ─── PARAMETROS DEL BOT ───────────────────────────────────────
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "0.5"))  # % del balance arriesgado por operacion
LOT_SIZE_FALLBACK = 0.02  # solo se usa si el calculo dinamico falla (ver execute_order)

# ── Lote fijo (opcional) ──
# Si USE_FIXED_LOT=true, el bot ignora el calculo dinamico por % de riesgo
# y usa siempre FIXED_LOT_SIZE. Poner USE_FIXED_LOT=false para volver al
# calculo dinamico basado en RISK_PERCENT.
USE_FIXED_LOT = os.getenv("USE_FIXED_LOT", "false").lower() == "true"
FIXED_LOT_SIZE = float(os.getenv("FIXED_LOT_SIZE", "0.02"))
MIN_SCORE = 75
MAX_DAILY = int(os.getenv("MAX_DAILY", "6"))  # antes fijo en 3
MAX_LOSSES_PER_DAY = int(os.getenv("MAX_LOSSES_PER_DAY", "2"))  # corta el dia tras N perdidas (limite general)
SL_EXTRA_PTS = 20
MAGIC_NUMBER = 20260601
DEVIATION = 20
DAILY_FILE = "bot_daily_count.txt"
ERROR_LOG_FILE = "bot_errors.log"  # NUEVO: guarda el motivo real de cada fallo de ejecucion
LOOP_INTERVAL = int(os.getenv("BOT_LOOP_INTERVAL", "60"))  # segundos entre cada chequeo

# ── Filtro de vigencia (anti señal-vieja) ──
MAX_SIGNAL_AGE_MINUTES = 10
MAX_PRICE_DRIFT_RATIO = 0.6

# ── Killzones (hora local RD, la misma que usa datetime.now() en esta PC) ──
KILLZONE_LONDON = ("LONDON", dtime(3, 0), dtime(6, 0))
KILLZONE_NYC    = ("NYC", dtime(9, 0), dtime(12, 0))
KILLZONES = [KILLZONE_LONDON, KILLZONE_NYC]

# Limite de perdidas POR KILLZONE, independiente de MAX_LOSSES_PER_DAY.
# Si se alcanza dentro de una ventana, el bot pausa SOLO esa ventana —
# la otra killzone se evalua por separado, desde cero.
MAX_LOSSES_PER_KILLZONE = int(os.getenv("MAX_LOSSES_PER_KILLZONE", "2"))

# ── Gestion de salida por cambio de estructura (CHoCH) ──
# Estrategias que, mientras tienen una posicion real abierta, se
# monitorean en cada ciclo por si aparece un CHoCH en contra — de ser
# asi, se cierra la posicion completa antes de esperar SL/TP1.
EARLY_EXIT_STRATEGIES = ["EMA Pullback M5"]
# Misma ventana que usa signal_engine.py para detectar CHoCH (20 velas M5).
CHOCH_WINDOW = 20

# ── Breakeven real al 80% del camino a TP1 ──
# Se aplica a TODAS las estrategias (no solo a las de EARLY_EXIT_STRATEGIES):
# es proteccion de riesgo general, independiente de la gestion por CHoCH.
BREAKEVEN_TRIGGER_PCT = float(os.getenv("BREAKEVEN_TRIGGER_PCT", "0.7"))
# ──────────────────────────────────────────────────────────────

ALLOWED_STRATEGIES = [
    "Scalping M5",
    "Scalping M1",
    "SMC H1",
    "Ruptura y confirmación",
    "Scalping M5 Engine",
    "H1 Liquidity Engulfing CHOCH",
    "Scalping M5 SMC",
    "Killzone Breakout",
    "FVG Fill M5",
    "EMA Pullback M5",
    "TradingPro AI Elite",
]


def headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def send_telegram(message, intentos=3):
    if not TELEGRAM_TOKEN:
        return False

    ultimo_error = None
    for intento in range(1, intentos + 1):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
                timeout=10,
            )
            if r.status_code == 200:
                return True
            ultimo_error = f"HTTP {r.status_code}: {r.text}"
        except Exception as e:
            ultimo_error = str(e)

        print(f"  [TELEGRAM] Intento {intento}/{intentos} fallo: {ultimo_error}")
        if intento < intentos:
            time.sleep(2)

    log_error_to_file(
        f"TELEGRAM NO ENVIADO tras {intentos} intentos. Error: {ultimo_error} | Mensaje: {message}"
    )
    return False


def log_error_to_file(message):
    try:
        with open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception as e:
        print(f"  [LOG] Error escribiendo {ERROR_LOG_FILE}: {e}")


def get_daily_count():
    try:
        if os.path.exists(DAILY_FILE):
            with open(DAILY_FILE, "r", encoding="utf-8") as f:
                raw = f.read().strip()

            data = raw.split(",")
            if len(data) == 2 and data[0] == str(date.today()):
                return int(data[1])
    except Exception:
        pass

    return 0


def increment_daily_count(intentos=3):
    """
    NUEVO: reintenta hasta `intentos` veces si el archivo esta bloqueado un
    instante (ej. antivirus o OneDrive sincronizando la carpeta justo en ese
    momento — error tipico en Windows: PermissionError / Errno 13).
    Antes, un solo bloqueo pasajero tumbaba toda la funcion con una excepcion
    sin capturar, cortando el ciclo ANTES de imprimir "ORDEN EJECUTADA" y
    ANTES de enviar el aviso de Telegram — aunque la orden ya se hubiera
    abierto bien en MT5.
    Si tras todos los intentos sigue sin poder escribir, se registra en
    bot_errors.log y se devuelve el conteo de todos modos (sin persistir),
    para que el resto del ciclo (impresion + Telegram) siga su curso normal.
    """
    count = get_daily_count() + 1
    ultimo_error = None

    for intento in range(1, intentos + 1):
        try:
            with open(DAILY_FILE, "w", encoding="utf-8") as f:
                f.write(f"{date.today()},{count}")
            return count
        except Exception as e:
            ultimo_error = str(e)
            print(f"  [DAILY_COUNT] Intento {intento}/{intentos} fallo al escribir {DAILY_FILE}: {ultimo_error}")
            if intento < intentos:
                time.sleep(1)

    log_error_to_file(
        f"NO SE PUDO ESCRIBIR {DAILY_FILE} tras {intentos} intentos. "
        f"Error: {ultimo_error} | Conteo no persistido (se uso {count} solo en memoria)."
    )
    return count


def connect_mt5():
    if not mt5.initialize():
        raise RuntimeError(f"No se pudo conectar a MT5: {mt5.last_error()}")

    account = mt5.account_info()
    if account is None:
        raise RuntimeError("MT5 sin cuenta conectada.")

    symbol = mt5.symbol_info(MT5_SYMBOL)
    if symbol is None:
        raise RuntimeError(f"Simbolo {MT5_SYMBOL} no encontrado en MT5.")

    if not symbol.visible:
        selected = mt5.symbol_select(MT5_SYMBOL, True)
        if not selected:
            raise RuntimeError(f"No se pudo activar el simbolo {MT5_SYMBOL}.")

    return account


def _closing_deals_between(start_dt, end_dt):
    """Deals de cierre (MAGIC_NUMBER, DEAL_ENTRY_OUT) entre dos datetime locales."""
    deals = mt5.history_deals_get(start_dt, end_dt)
    if deals is None:
        return []
    return [
        d for d in deals
        if getattr(d, "magic", None) == MAGIC_NUMBER
        and getattr(d, "entry", None) == mt5.DEAL_ENTRY_OUT
    ]


def get_daily_losses():
    today_start = datetime.combine(date.today(), datetime.min.time())
    closing_deals = _closing_deals_between(today_start, datetime.now())
    return sum(1 for d in closing_deals if d.profit < 0)


def get_daily_wins():
    today_start = datetime.combine(date.today(), datetime.min.time())
    closing_deals = _closing_deals_between(today_start, datetime.now())
    return sum(1 for d in closing_deals if d.profit >= 0)


def get_current_killzone(now=None):
    """
    Devuelve (nombre, hora_inicio, hora_fin) de la killzone en la que cae
    `now` (hora local RD), o None si esta fuera de ambas ventanas.
    """
    now = now or datetime.now()
    t = now.time()
    for name, start, end in KILLZONES:
        if start <= t < end:
            return name, start, end
    return None


def get_killzone_losses(start_time, end_time):
    """
    Cuenta perdidas reales (magic number, deal de cierre, profit<0) cuyo
    cierre cayo dentro de la ventana horaria [start_time, end_time) de HOY.
    Se usa para el limite MAX_LOSSES_PER_KILLZONE, independiente del
    limite diario general.
    """
    today = date.today()
    window_start = datetime.combine(today, start_time)
    window_end = datetime.combine(today, end_time)
    closing_deals = _closing_deals_between(window_start, window_end)
    return sum(1 for d in closing_deals if d.profit < 0)


def get_open_positions():
    positions = mt5.positions_get(symbol=MT5_SYMBOL)

    if positions is None:
        return []

    return [p for p in positions if getattr(p, "magic", None) == MAGIC_NUMBER]


# ── Gestion de salida por cambio de estructura (CHoCH) ─────────

def get_recent_m5_candles(limit=30):
    """Trae las ultimas velas M5 de Supabase (mismo origen que usa
    signal_engine.py) para poder evaluar CHoCH sobre una posicion
    ya abierta."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/ohlc_candles",
            headers=headers(),
            params={
                "select":     "candle_time,high,low,close",
                "instrument": "eq.XAUUSD",
                "timeframe":  "eq.M5",
                "order":      "candle_time.desc",
                "limit":      str(limit),
            },
            timeout=15,
        )
        if r.status_code >= 400:
            return []
        return list(reversed(r.json()))  # orden cronologico
    except Exception:
        return []


def detect_choch(candles):
    """
    Replica EXACTA de la deteccion de CHoCH que usa signal_engine.py
    en la Estrategia 1 (Scalping M5 SMC): compara el maximo/minimo de
    las ultimas 5 velas contra el maximo/minimo de las 5 anteriores,
    dentro de una ventana de CHOCH_WINDOW velas M5.
    Devuelve "BUY", "SELL" o None.
    """
    if len(candles) < CHOCH_WINDOW:
        return None

    r = candles[-CHOCH_WINDOW:]
    rH = max(float(c["high"]) for c in r[-5:])
    pH = max(float(c["high"]) for c in r[-10:-5])
    rL = min(float(c["low"]) for c in r[-5:])
    pL = min(float(c["low"]) for c in r[-10:-5])

    if rH > pH and rL < pL:
        return "BUY"
    if rH < pH and rL > pL:
        return "SELL"
    return None


def get_strategy_by_position_comment(comment):
    """
    El comment de la orden real en MT5 es 'TradingPro_{signal_id[:8]}'
    (ver execute_order). Busca en Supabase la señal cuyo id empieza
    con ese prefijo para saber de que estrategia es la posicion
    abierta — asi la gestion de salida por CHoCH solo se aplica a las
    estrategias listadas en EARLY_EXIT_STRATEGIES.
    """
    if not comment or not comment.startswith("TradingPro_"):
        return None
    prefix = comment.replace("TradingPro_", "").strip()
    if not prefix:
        return None
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/signals",
            headers=headers(),
            params={
                "id":     f"like.{prefix}*",
                "select": "strategy",
                "limit":  "1",
            },
            timeout=15,
        )
        if r.status_code >= 400:
            return None
        data = r.json()
        return data[0]["strategy"] if data else None
    except Exception:
        return None


def close_position_market(position, motivo=""):
    """
    Cierra a mercado el 100% de una posicion abierta (orden inversa
    sobre el mismo ticket). Se usa para el cierre anticipado por CHoCH
    en contra — no espera a que el precio toque SL o TP1.
    """
    tick = mt5.symbol_info_tick(MT5_SYMBOL)
    if tick is None:
        return False, None, "Sin precio disponible en MT5 para cerrar la posicion"

    is_buy = position.type == mt5.POSITION_TYPE_BUY
    close_price = tick.bid if is_buy else tick.ask

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       MT5_SYMBOL,
        "volume":       position.volume,
        "type":         mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
        "position":     position.ticket,
        "price":        close_price,
        "deviation":    DEVIATION,
        "magic":        MAGIC_NUMBER,
        "comment":      "CHoCH_exit",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)

    if result is None:
        error_code, error_desc = mt5.last_error()
        detalle = f"order_send devolvio None al cerrar. last_error: {error_code} - {error_desc}"
        return False, None, detalle

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        detalle = f"Cierre rechazado. Retcode: {result.retcode} - {result.comment}"
        return False, None, detalle

    return True, close_price, None


# ── Breakeven real al 80% del camino a TP1 ──────────────────────

_breakeven_applied = set()  # tickets ya movidos a breakeven en esta sesion


def modify_position_sltp(position, new_sl, new_tp=None):
    """Modifica el SL (y opcionalmente TP) de una posicion real ya
    abierta en MT5, sin cerrarla."""
    request = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "symbol":   MT5_SYMBOL,
        "position": position.ticket,
        "sl":       round(new_sl, 2),
        "tp":       round(new_tp if new_tp is not None else position.tp, 2),
    }
    result = mt5.order_send(request)

    if result is None:
        error_code, error_desc = mt5.last_error()
        return False, f"order_send devolvio None. last_error: {error_code} - {error_desc}"

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return False, f"Modificacion rechazada. Retcode: {result.retcode} - {result.comment}"

    return True, None


def check_breakeven(position, side, now_str):
    """
    Si el precio ya recorrio BREAKEVEN_TRIGGER_PCT (80% por defecto)
    de la distancia entre la entrada y el TP1 (position.tp, el mismo
    TP1 que se fijo al abrir la orden), mueve el SL real a la entrada.
    Se aplica una sola vez por ticket. Independiente de la estrategia
    y de la gestion por CHoCH.
    """
    if position.ticket in _breakeven_applied:
        return False

    entry = position.price_open
    tp1 = position.tp

    if position.sl and abs(position.sl - entry) < 0.01:
        # Ya esta en breakeven (ej. si el bot se reinicio despues de aplicarlo)
        _breakeven_applied.add(position.ticket)
        return False

    if not tp1:
        return False

    distancia_total = abs(tp1 - entry)
    if distancia_total <= 0:
        return False

    tick = mt5.symbol_info_tick(MT5_SYMBOL)
    if tick is None:
        return False

    # Precio conservador: el que realmente obtendrias si cerraras ahora
    precio_actual = tick.bid if side == "BUY" else tick.ask
    recorrido = (precio_actual - entry) if side == "BUY" else (entry - precio_actual)
    progreso = recorrido / distancia_total

    if progreso < BREAKEVEN_TRIGGER_PCT:
        return False

    ok, error_detail = modify_position_sltp(position, new_sl=entry)

    if not ok:
        print(f"  [BREAKEVEN] Error moviendo SL a breakeven en ticket {position.ticket}: {error_detail}")
        log_error_to_file(f"Breakeven fallo — ticket {position.ticket}: {error_detail}")
        return False

    _breakeven_applied.add(position.ticket)
    print(
        f"  [BREAKEVEN] Ticket {position.ticket} — SL movido a entrada ({entry}) "
        f"al {progreso*100:.0f}% del camino a TP1"
    )
    send_telegram(
        f"[BOT] BREAKEVEN ACTIVADO\n"
        f"Ticket: {position.ticket}\n"
        f"SL movido a precio de entrada: {entry}\n"
        f"Progreso hacia TP1: {progreso*100:.0f}%\n"
        f"Riesgo restante en esta operacion: $0\n"
        f"Hora: {now_str}"
    )
    return True


def check_choch_exit(position, side, strategy, now_str):
    """
    Si la posicion abierta pertenece a una estrategia con gestion de
    salida temprana (EARLY_EXIT_STRATEGIES) y aparece un CHoCH en
    contra de su direccion, la cierra de inmediato. Devuelve True si
    cerro la posicion (para que run_cycle no siga evaluando nada mas
    ese ciclo), False si no hizo nada.
    """
    if strategy not in EARLY_EXIT_STRATEGIES:
        return False

    candles = get_recent_m5_candles(30)
    choch = detect_choch(candles)

    if not choch or choch == side:
        return False

    profit_antes = round(position.profit, 2)
    ok, close_price, error_detail = close_position_market(position)

    if not ok:
        print(f"  [CHoCH EXIT] Error cerrando ticket {position.ticket}: {error_detail}")
        log_error_to_file(
            f"CHoCH exit fallo — ticket {position.ticket} ({strategy}, {side}): {error_detail}"
        )
        return False

    print(
        f"  [CHoCH EXIT] {strategy} — CHoCH {choch} detectado en contra de {side}. "
        f"Posicion {position.ticket} cerrada @ {close_price} | Profit aprox: ${profit_antes}"
    )
    send_telegram(
        f"[BOT] CIERRE ANTICIPADO — {strategy}\n"
        f"Motivo: cambio de estructura (CHoCH {choch}) en contra de {side}\n"
        f"Ticket: {position.ticket}\n"
        f"Precio de cierre: {close_price}\n"
        f"Profit aprox.: ${profit_antes}\n"
        f"Hora: {now_str}"
    )
    return True


def is_signal_stale(signal, current_price):
    created_at_raw = signal.get("created_at")
    if not created_at_raw:
        return True, "sin created_at"

    try:
        created_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
        now = datetime.now(created_at.tzinfo)
        age_minutes = (now - created_at).total_seconds() / 60
    except Exception:
        return True, "created_at invalido"

    if age_minutes > MAX_SIGNAL_AGE_MINUTES:
        return True, f"tiene {age_minutes:.0f} min (max {MAX_SIGNAL_AGE_MINUTES})"

    if current_price is not None:
        try:
            entry = float(signal["entry_price"])
            sl = float(signal["stop_loss"])
            riesgo_original = abs(entry - sl)
            if riesgo_original > 0:
                deriva = abs(current_price - entry)
                ratio = deriva / riesgo_original
                if ratio > MAX_PRICE_DRIFT_RATIO:
                    return True, (
                        f"precio se alejo {deriva:.2f} pts "
                        f"({ratio:.1f}x el riesgo original de {riesgo_original:.2f} pts)"
                    )
        except (KeyError, ValueError, ZeroDivisionError):
            pass

    return False, "OK"


def get_pending_signals(current_price=None):
    """Obtiene señales PENDING con confidence >= MIN_SCORE, estrategia permitida y vigentes."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: Faltan SUPABASE_URL o SUPABASE_KEY en .env")
        return []

    url = (
        f"{SUPABASE_URL}/rest/v1/signals"
        f"?status=eq.PENDING"
        f"&result=is.null"
        f"&confidence=gte.{MIN_SCORE}"
        f"&select=id,signal_type,entry_price,stop_loss,take_profit_1,take_profit_2,confidence,strategy,created_at"
        f"&order=confidence.desc,created_at.desc"
        f"&limit=10"
    )

    try:
        r = requests.get(url, headers=headers(), timeout=20)
    except Exception as e:
        print(f"  Error conectando a Supabase: {e}")
        return []

    if r.status_code >= 400:
        print(f"  Error Supabase: {r.status_code} {r.text}")
        return []

    all_signals = r.json()

    filtered = []
    excluded = []
    stale = []

    for signal in all_signals:
        strategy = signal.get("strategy")
        if strategy not in ALLOWED_STRATEGIES:
            excluded.append(strategy)
            continue

        is_stale, reason = is_signal_stale(signal, current_price)
        if is_stale:
            stale.append((signal.get("id", "")[:8], reason))
            # FIX: antes esto solo se ignoraba en memoria y la señal se
            # quedaba PENDING para siempre en Supabase. Ahora se marca
            # EXPIRED de una vez para que la cola se autolimpie sola.
            update_signal_status(signal["id"], "EXPIRED")
            continue

        filtered.append(signal)

    if excluded:
        unique_excluded = sorted(set(str(x) for x in excluded))
        print(
            f"  Filtradas {len(excluded)} señal(es) de estrategias no permitidas: "
            f"{', '.join(unique_excluded)}"
        )

    if stale:
        for sig_id, reason in stale:
            print(f"  [VIGENCIA] Señal {sig_id} descartada y marcada EXPIRED — {reason}")

    return filtered


def get_current_price():
    tick = mt5.symbol_info_tick(MT5_SYMBOL)

    if tick is None:
        return None, None

    return tick.ask, tick.bid


def calc_anti_hunt_sl(signal_type, original_sl):
    symbol = mt5.symbol_info(MT5_SYMBOL)

    if symbol is None:
        return round(original_sl, 2)

    extra = SL_EXTRA_PTS * symbol.point * 10

    if signal_type == "BUY":
        return round(original_sl - extra, 2)

    return round(original_sl + extra, 2)


def validate_signal(signal):
    required = [
        "id",
        "signal_type",
        "entry_price",
        "stop_loss",
        "take_profit_1",
        "take_profit_2",
        "confidence",
        "strategy",
    ]

    missing = [field for field in required if field not in signal or signal[field] is None]
    if missing:
        return False, f"Señal incompleta. Faltan campos: {', '.join(missing)}"

    if signal["signal_type"] not in ["BUY", "SELL"]:
        return False, f"Tipo de señal invalido: {signal['signal_type']}"

    try:
        float(signal["entry_price"])
        float(signal["stop_loss"])
        float(signal["take_profit_1"])
        float(signal["take_profit_2"])
        int(signal["confidence"])
    except Exception:
        return False, "La señal tiene valores numericos invalidos."

    return True, "OK"


def execute_order(signal):
    is_valid, reason = validate_signal(signal)
    if not is_valid:
        print(f"  Señal invalida: {reason}")
        return None, f"Señal invalida: {reason}"

    ask, bid = get_current_price()
    if ask is None or bid is None:
        print("  Sin precio disponible en MT5.")
        return None, "Sin precio disponible en MT5 (symbol_info_tick devolvio None)"

    signal_type = signal["signal_type"]
    original_sl = float(signal["stop_loss"])
    tp1 = float(signal["take_profit_1"])
    sl = calc_anti_hunt_sl(signal_type, original_sl)

    order_type = mt5.ORDER_TYPE_BUY if signal_type == "BUY" else mt5.ORDER_TYPE_SELL
    price = ask if signal_type == "BUY" else bid

    account = mt5.account_info()

    if USE_FIXED_LOT:
        lote = FIXED_LOT_SIZE
        print(f"  [RIESGO] Lote fijo activado: {lote} (USE_FIXED_LOT=true)")
    else:
        lote, detalle = calculate_lot_size(
            mt5=mt5,
            symbol=MT5_SYMBOL,
            entry_price=price,
            stop_loss=sl,
            balance=account.balance if account else 0,
            risk_percent=RISK_PERCENT,
        )

        if lote is None:
            print(f"  [RIESGO] No se pudo calcular lote dinamico: {detalle}")
            print(f"  [RIESGO] Usando lote de respaldo: {LOT_SIZE_FALLBACK}")
            lote = LOT_SIZE_FALLBACK
        else:
            print(
                f"  [RIESGO] Balance: ${detalle['balance']} | "
                f"Riesgo: {detalle['risk_percent']}% (${detalle['riesgo_dinero']}) | "
                f"SL: {detalle['distancia_stop_pts']} pts | Lote: {lote}"
            )
            if "aviso" in detalle:
                print(f"  [RIESGO] AVISO: {detalle['aviso']}")

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": MT5_SYMBOL,
        "volume": lote,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp1,
        "deviation": DEVIATION,
        "magic": MAGIC_NUMBER,
        "comment": f"TradingPro_{signal['id'][:8]}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)

    if result is None:
        error_code, error_desc = mt5.last_error()
        detalle_error = (
            f"order_send devolvio None. MT5 last_error: {error_code} - {error_desc} | "
            f"volumen={lote}, precio={price}, sl={sl}, tp={tp1}, "
            f"balance=${account.balance if account else '?'}"
        )
        print(f"  Error enviando orden: {detalle_error}")
        return None, detalle_error

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        detalle_error = (
            f"Orden rechazada. Retcode: {result.retcode} - {result.comment} | "
            f"volumen={lote}, precio={price}, sl={sl}, tp={tp1}, "
            f"balance=${account.balance if account else '?'}"
        )
        print(f"  {detalle_error}")
        return None, detalle_error

    return result, None


def update_signal_status(sig_id, status, intentos=3):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False

    ultimo_error = None
    for intento in range(1, intentos + 1):
        try:
            r = requests.patch(
                f"{SUPABASE_URL}/rest/v1/signals?id=eq.{sig_id}",
                headers=headers(),
                json={"status": status},
                timeout=15,
            )

            if r.status_code < 400:
                return True

            ultimo_error = f"HTTP {r.status_code}: {r.text}"
        except Exception as e:
            ultimo_error = str(e)

        print(f"  [SUPABASE] Intento {intento}/{intentos} fallo al actualizar señal {sig_id}: {ultimo_error}")
        if intento < intentos:
            time.sleep(2)

    log_error_to_file(
        f"NO SE PUDO ACTUALIZAR ESTADO tras {intentos} intentos. "
        f"Señal: {sig_id} -> {status} | Error: {ultimo_error}"
    )
    return False


_loss_alert_sent_date = None
_kz_loss_alert_sent = {}  # {(date, killzone_name): True} — evita spamear Telegram cada ciclo


def run_cycle():
    global _loss_alert_sent_date
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    daily_count = get_daily_count()
    if daily_count >= MAX_DAILY:
        print(f"  [{now_str}] Limite diario alcanzado ({daily_count}/{MAX_DAILY}). Esperando al dia siguiente.")
        return

    daily_losses = get_daily_losses()
    if daily_losses >= MAX_LOSSES_PER_DAY:
        print(
            f"  [{now_str}] Limite de perdidas diarias alcanzado "
            f"({daily_losses}/{MAX_LOSSES_PER_DAY}). Se detiene por hoy para proteger la cuenta."
        )
        if _loss_alert_sent_date != date.today():
            send_telegram(
                f"[BOT] Limite de perdidas alcanzado — {daily_losses}/{MAX_LOSSES_PER_DAY}\n"
                f"El bot deja de operar por hoy para proteger la cuenta.\n"
                f"Hora: {now_str}"
            )
            _loss_alert_sent_date = date.today()
        return

    # ── NUEVO: limite de perdidas POR KILLZONE (independiente del diario) ──
    kz = get_current_killzone(now)
    if kz is not None:
        kz_name, kz_start, kz_end = kz
        kz_losses = get_killzone_losses(kz_start, kz_end)
        if kz_losses >= MAX_LOSSES_PER_KILLZONE:
            print(
                f"  [{now_str}] Limite de perdidas de killzone {kz_name} alcanzado "
                f"({kz_losses}/{MAX_LOSSES_PER_KILLZONE}). Se pausa esta ventana."
            )
            alert_key = (date.today(), kz_name)
            if _kz_loss_alert_sent.get(alert_key) is not True:
                send_telegram(
                    f"[BOT] Limite de perdidas en killzone {kz_name} — "
                    f"{kz_losses}/{MAX_LOSSES_PER_KILLZONE}\n"
                    f"El bot pausa esta ventana ({kz_start.strftime('%H:%M')}-"
                    f"{kz_end.strftime('%H:%M')} RD). Se reactiva en la siguiente killzone.\n"
                    f"Hora: {now_str}"
                )
                _kz_loss_alert_sent[alert_key] = True
            return

    open_positions = get_open_positions()
    if open_positions:
        pos = open_positions[0]
        side = "BUY" if pos.type == 0 else "SELL"
        print(
            f"  [{now_str}] Posicion abierta: ticket {pos.ticket} | {side} | "
            f"Profit: ${round(pos.profit, 2)}"
        )

        # ── NUEVO: breakeven real al 80% del camino a TP1 (todas las estrategias) ──
        check_breakeven(pos, side, now_str)

        # ── NUEVO: gestion de salida por cambio de estructura (CHoCH) ──
        strategy = get_strategy_by_position_comment(getattr(pos, "comment", None))
        if strategy in EARLY_EXIT_STRATEGIES:
            check_choch_exit(pos, side, strategy, now_str)

        return

    signals = get_pending_signals(current_price=get_current_price()[0])
    if not signals:
        print(f"  [{now_str}] Sin señales pendientes con score suficiente, estrategia permitida y vigentes.")
        return

    best = signals[0]
    sig_id = best["id"]
    sig_type = best["signal_type"]
    score = best["confidence"]
    strategy = best["strategy"]

    print(f"\n  [{now_str}] Señal encontrada: {sig_type} | Score: {score}/100 | {strategy}")
    print("  Ejecutando orden en MT5...")

    result, error_detail = execute_order(best)

    if result is None:
        print("  No se pudo ejecutar la orden.")
        log_error_to_file(f"Señal {sig_id[:8]} ({sig_type}, score {score}, {strategy}): {error_detail}")
        update_signal_status(sig_id, "FAILED")
        send_telegram(
            f"[BOT] ORDEN FALLIDA - {sig_type} XAUUSD\n"
            f"Score: {score}/100 | Estrategia: {strategy}\n"
            f"Motivo: {error_detail}\n"
            f"Hora: {now_str}"
        )
        return

    count = increment_daily_count()
    ask, bid = get_current_price()
    price = ask if sig_type == "BUY" else bid
    sl = calc_anti_hunt_sl(sig_type, float(best["stop_loss"]))
    tp1 = float(best["take_profit_1"])

    update_signal_status(sig_id, "EXECUTING")

    print("\n  ORDEN EJECUTADA:")
    print(f"    Ticket: {result.order}")
    print(f"    Tipo:   {sig_type}")
    print(f"    Precio: {price}")
    print(f"    SL:     {sl} (anti-hunt +{SL_EXTRA_PTS} pts)")
    print(f"    TP1:    {tp1}")
    print(f"    Lote:   {result.volume}")
    print(f"    Hoy:    {count}/{MAX_DAILY}")

    send_telegram(
        f"[BOT] ORDEN ABIERTA - {sig_type} XAUUSD\n"
        f"Ticket: {result.order}\n"
        f"Precio entrada: {price}\n"
        f"Stop Loss: {sl} (anti-hunt)\n"
        f"Take Profit: {tp1}\n"
        f"Lote: {result.volume} (riesgo: {RISK_PERCENT}% del balance)\n"
        f"Score: {score}/100\n"
        f"Estrategia: {strategy}\n"
        f"Operaciones hoy: {count}/{MAX_DAILY}\n"
        f"Hora: {now_str}"
    )


def ensure_mt5_connected():
    account = mt5.account_info()
    if account is not None:
        return account

    print("  [MT5] Conexion perdida. Intentando reconectar...")
    return connect_mt5()


def main():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n{'=' * 55}")
    print(f"  BOT ENGINE - TradingProEA - {now_str}")
    print(f"  URL: {SUPABASE_URL}")
    print(f"  Simbolo MT5: {MT5_SYMBOL}")
    if USE_FIXED_LOT:
        print(f"  Lote: FIJO {FIXED_LOT_SIZE} (USE_FIXED_LOT=true) | Score min: {MIN_SCORE} | SL extra: {SL_EXTRA_PTS} pts")
    else:
        print(f"  Riesgo por operacion: {RISK_PERCENT}% del balance | Score min: {MIN_SCORE} | SL extra: {SL_EXTRA_PTS} pts")
    print(f"  Max diario: {MAX_DAILY} operaciones | Max perdidas/dia: {MAX_LOSSES_PER_DAY}")
    print(f"  Max perdidas/killzone: {MAX_LOSSES_PER_KILLZONE} (Londres 03:00-06:00 RD / NYC 09:00-12:00 RD)")
    print(f"  Breakeven real al {BREAKEVEN_TRIGGER_PCT*100:.0f}% del camino a TP1 (todas las estrategias)")
    print(f"  Salida por CHoCH activa para: {', '.join(EARLY_EXIT_STRATEGIES)}")
    print(f"  Vigencia: max {MAX_SIGNAL_AGE_MINUTES} min | deriva max {MAX_PRICE_DRIFT_RATIO}x el riesgo original")
    print(f"  Estrategias permitidas: {len(ALLOWED_STRATEGIES)}")
    print(f"  Loop: cada {LOOP_INTERVAL}s — Ctrl+C para detener")
    print(f"{'=' * 55}")

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: Faltan SUPABASE_URL o SUPABASE_KEY en .env")
        return

    try:
        account = connect_mt5()
        print(f"MT5 conectado: {account.login} | Balance: ${account.balance:.2f}")
    except Exception as e:
        print(f"Error MT5: {e}")
        return

    if TELEGRAM_TOKEN:
        send_telegram(
            f"[BOT] bot_engine.py INICIADO\n"
            f"Riesgo: {RISK_PERCENT}% | Score min: {MIN_SCORE} | Max diario: {MAX_DAILY}\n"
            f"Max perdidas/dia: {MAX_LOSSES_PER_DAY} | Max perdidas/killzone: {MAX_LOSSES_PER_KILLZONE}\n"
            f"Loop: {LOOP_INTERVAL}s\n"
            f"Hora: {now_str}"
        )

    try:
        while True:
            try:
                ensure_mt5_connected()
                run_cycle()
            except Exception as e:
                print(f"  [ERROR] run_cycle: {e}")
            time.sleep(LOOP_INTERVAL)
    except KeyboardInterrupt:
        print("\nDetenido manualmente (Ctrl+C).")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            mt5.shutdown()
        except Exception:
            pass

        print(f"ERROR: {e}")
        sys.exit(1)
