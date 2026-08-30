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
- Maximo 2 perdidas POR KILLZONE (Londres 3-6 AM RD / NYC 9-12 PM RD),
  independiente del limite diario general — protege cada sesion por separado
- NUEVO: Maximo 2 perdidas FUERA de ambas killzones — protege las horas
  donde el bot opera fuera de sesion desde que killzone dejo de ser
  bloqueo obligatorio en signal_engine.py
- SL anti-hunt: 20 puntos extra
- Maximo 1 operacion abierta a la vez
- Maximo 6 operaciones por dia (configurable via MAX_DAILY en .env)
- Solo ejecuta estrategias permitidas
- NUEVO: cierre por tiempo (time-stop) para estrategias en
  TIME_STOP_STRATEGIES si no llegan a breakeven en TIME_STOP_MINUTES
- Notifica a Telegram al abrir

ADVERTENCIA:
Este script ejecuta ordenes REALES en MT5.
Usalo solo en la PC donde MetaTrader 5 este abierto y conectado.

CAMBIOS EN ESTA VERSION (fix: señal huerfana en Supabase tras cierre anticipado):
- BUG: los cierres anticipados reales en MT5 (CHoCH exit, time-stop)
  cerraban la posicion en MT5 pero NUNCA actualizaban la señal
  correspondiente en Supabase — se quedaba con status=EXECUTING para
  siempre. result_tracker.py solo revisa señales EXECUTING y las
  simula recorriendo velas M5 desde cero, sin saber que la posicion
  real ya se habia cerrado por otro motivo y a otro precio. Resultado:
  Telegram reportaba un WIN/LOSS ficticio sobre un trade que ya no
  existia (caso detectado: señal entrada 4402.65, EMA Pullback M5,
  reportada como LOSS por result_tracker.py aunque ya se habia cerrado
  antes via CHoCH/time-stop).
- FIX: get_strategy_by_position_comment() se reemplaza por
  get_signal_info_by_position_comment(), que ademas de la estrategia
  devuelve el signal_id completo (antes solo se pedia "strategy" a
  Supabase). Se agrega update_signal_closed(sig_id, result, exit_price)
  que marca la señal como CLOSED con su resultado real (WIN si el
  profit de MT5 al cerrar fue positivo, LOSS si no) en el mismo
  momento en que se cierra la posicion real. check_choch_exit() y
  check_time_stop() ahora reciben sig_id y llaman a esta funcion
  justo despues de close_position_market(). Con esto, cualquier señal
  cerrada por estos caminos deja de ser EXECUTING de inmediato y
  result_tracker.py no vuelve a simularla.

CAMBIOS EN VERSION ANTERIOR (limite de perdidas fuera de killzone + time-stop):
- NUEVO 2026-08-17 (limite de perdidas FUERA de killzone):
  Desde que killzone_requerida() en signal_engine.py dejo de bloquear
  (13-ago), todas las estrategias pueden operar fuera de Londres/NYC.
  Se agrega MAX_LOSSES_OUTSIDE_KILLZONE (2 por defecto), evaluado solo
  cuando get_current_killzone() devuelve None (fuera de ambas
  ventanas). Si se alcanza, el bot pausa fuera de killzone hasta que
  entre la proxima ventana — mismo patron que MAX_LOSSES_PER_KILLZONE,
  reutilizando get_daily_losses()/get_killzone_losses() por diferencia
  en vez de reconvertir horarios de nuevo.
- NUEVO 2026-08-17 (time-stop para EMA Pullback M5):
  Analisis del historico real de trades del bot (25 operaciones,
  origen=BOT, strategy='EMA Pullback M5'): los trades ganadores
  resuelven en ~15-20 min; los perdedores se alargan 35-100+ min sin
  llegar a breakeven. Se agrega check_time_stop(): si una posicion de
  una estrategia en TIME_STOP_STRATEGIES lleva mas de
  TIME_STOP_MINUTES abierta y todavia no alcanzo el 70% del camino a
  TP1 (no esta en _breakeven_applied), se cierra a mercado en vez de
  dejarla seguir sangrando tiempo. Se evalua ANTES que check_choch_exit
  en run_cycle — si el time-stop ya cerro la posicion, CHoCH no se
  evalua ese ciclo (ya no hay posicion que cerrar).

CAMBIOS EN VERSION ANTERIOR (fix critico: zona horaria en conteo de perdidas):
- BUG ENCONTRADO 2026-08-11: en un solo dia hubo 5 perdidas reales
  ejecutadas (4 EMA Pullback M5 + 1 FVG Fill M5) cuando MAX_LOSSES_PER_DAY=4
  y MAX_LOSSES_PER_KILLZONE=2 (default) deberian haber bloqueado la 5ta
  operacion — habia 4 perdidas ya cerradas (2 en Londres, 2 en NYC) antes
  de que abriera la 5ta a las 11:01 AM RD, dentro de la misma killzone NYC
  donde ya se habian alcanzado 2 perdidas.
- CAUSA: get_daily_losses(), get_daily_wins() y get_killzone_losses()
  armaban la ventana horaria con date.today()/datetime.now() (hora LOCAL
  de RD, UTC-4) y se la pasaban directo a mt5.history_deals_get(). Pero
  los timestamps de deals en MT5 vienen en hora del SERVIDOR del broker
  (XMGlobal, horario europeo EET/EEST — mismo problema que ya existia en
  data_engine.py y que motivo la funcion eet_offset_hours() ahi). Al no
  convertir la ventana de RD a hora de servidor antes de consultar, el
  rango horario real consultado no coincidia con el dia/killzone que se
  creia estar contando, dejando pasar perdidas que ya debian contar.
- FIX: se replican aqui las mismas funciones eu_dst_active()/
  eet_offset_hours() que ya usa data_engine.py, mas una nueva
  rd_to_broker_time() que convierte un datetime naive en hora RD a hora
  de servidor del broker. get_daily_losses(), get_daily_wins() y
  get_killzone_losses() ahora convierten sus limites de ventana con esta
  funcion ANTES de llamar a mt5.history_deals_get(), para que el conteo
  de perdidas realmente refleje el dia/killzone en hora RD, no un rango
  desfasado por la diferencia horaria con el servidor.

CAMBIOS EN VERSION ANTERIOR (trailing stop por ATR despues del breakeven):
- NUEVO: una vez que el breakeven ya se activo en una posicion (SL en
  la entrada), el bot empieza a "arrastrar" el SL detrás del precio
  usando una distancia = ATR(14) de M5 x TRAILING_ATR_MULTIPLIER (1.2
  por defecto — mismo multiplicador que ya usa strategy_ema_pullback()
  en signal_engine.py para su SL original, por consistencia).
- La distancia se adapta sola a la volatilidad del momento: en oro,
  con velas de rango muy variable, un trailing de puntos fijos o
  atrapa al precio con cualquier mecha normal (si es corto) o deja
  ganancia enorme sin proteger (si es largo). Con ATR, un dia tranquilo
  el trailing va mas pegado; un dia volatil (como el 2026-08-07,
  reversion en V que dejo un trade en breakeven sin capturar nada de
  los +$29 flotantes que llego a tener) se amplia solo.
- Regla dura: el SL SOLO se mueve hacia adelante (a favor del trade),
  nunca hacia atras — si el ATR crece y "sugeriria" alejar el stop, se
  ignora esa actualizacion en vez de darle mas espacio del ya ganado.
- Se aplica exclusivamente a posiciones que YA tienen el breakeven
  activo (no antes) — antes de eso el SL sigue siendo el anti-hunt
  original. Aplica a todas las estrategias, igual que el breakeven.
- Aviso por Telegram solo la PRIMERA vez que el trailing mueve el SL
  por ticket (para no saturar el canal); los ajustes siguientes solo
  quedan en el log de consola.

CAMBIOS EN VERSION ANTERIOR (breakeven real al 70% del camino a TP1):
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

CAMBIOS EN VERSION ANTERIOR (gestion de salida por cambio de estructura):
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

CAMBIOS EN VERSION ANTERIOR (limite de perdidas por killzone):
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
  general (MAX_LOSSES_PER_DAY) — hasta este cambio, ver arriba el
  nuevo MAX_LOSSES_OUTSIDE_KILLZONE.

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
from datetime import datetime, date, time as dtime, timezone, timedelta
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
# AJUSTE 2026-08-20: valores anteriores (10 min / 0.6x) eran demasiado
# permisivos. Con MAX_PRICE_DRIFT_RATIO=0.6, una señal con SL de 16.86 pts
# de riesgo (caso real del 20-ago) toleraba hasta 10.1 pts de deriva antes
# de marcarse vencida — suficiente para comprar ya casi en el techo del
# movimiento, justo antes del retroceso que activa el SL. Se ajusta a
# valores mas estrictos para cortar entradas tardias:
# FIX 2026-08-25: antes estos dos valores estaban fijos en el codigo y
# el .env los tenia declarados sin ningun efecto real (el bot siempre
# usaba 5 / 0.3 sin importar lo que dijera el .env). Ahora se leen de
# ahi, igual que ya hacia BOT_LOOP_INTERVAL, para poder ajustarlos sin
# tocar el codigo.
MAX_SIGNAL_AGE_MINUTES = int(os.getenv("MAX_SIGNAL_AGE_MINUTES", "5"))
MAX_PRICE_DRIFT_RATIO = float(os.getenv("MAX_PRICE_DRIFT_RATIO", "0.3"))

# ── Killzones (hora local RD, la misma que usa datetime.now() en esta PC) ──
KILLZONE_LONDON = ("LONDON", dtime(3, 0), dtime(6, 0))
KILLZONE_NYC    = ("NYC", dtime(9, 0), dtime(12, 0))
KILLZONES = [KILLZONE_LONDON, KILLZONE_NYC]

# Limite de perdidas POR KILLZONE, independiente de MAX_LOSSES_PER_DAY.
# Si se alcanza dentro de una ventana, el bot pausa SOLO esa ventana —
# la otra killzone se evalua por separado, desde cero.
MAX_LOSSES_PER_KILLZONE = int(os.getenv("MAX_LOSSES_PER_KILLZONE", "2"))

# ── Limite de perdidas FUERA de ambas killzones — NUEVO 2026-08-17 ──
# Proteccion general: desde que killzone_requerida() dejo de bloquear
# en signal_engine.py (13-ago), todas las estrategias pueden operar
# fuera de Londres/NYC. Este limite corta el bot fuera de esas ventanas
# si ya acumulo MAX_LOSSES_OUTSIDE_KILLZONE perdidas hoy en horario
# no-killzone, independiente del limite diario general y de los
# limites por killzone.
MAX_LOSSES_OUTSIDE_KILLZONE = int(os.getenv("MAX_LOSSES_OUTSIDE_KILLZONE", "2"))

# ── Gestion de salida por cambio de estructura (CHoCH) ──
# Estrategias que, mientras tienen una posicion real abierta, se
# monitorean en cada ciclo por si aparece un CHoCH en contra — de ser
# asi, se cierra la posicion completa antes de esperar SL/TP1.
EARLY_EXIT_STRATEGIES = ["EMA Pullback M5"]
# Misma ventana que usa signal_engine.py para detectar CHoCH (20 velas M5).
CHOCH_WINDOW = 20

# ── Cierre por tiempo (time-stop) — NUEVO 2026-08-17 ──
# Historico real: trades ganadores de EMA Pullback M5 resuelven en
# ~15-20 min; los perdedores se alargan 35-100+ min sin llegar a
# breakeven. Si a los TIME_STOP_MINUTES no llego al 70% del camino a
# TP1, se cierra en vez de dejarla sangrar mas tiempo.
TIME_STOP_STRATEGIES = ["EMA Pullback M5"]
TIME_STOP_MINUTES = int(os.getenv("TIME_STOP_MINUTES", "25"))

# ── Breakeven real al 70% del camino a TP1 ──
# Se aplica a TODAS las estrategias (no solo a las de EARLY_EXIT_STRATEGIES):
# es proteccion de riesgo general, independiente de la gestion por CHoCH.
BREAKEVEN_TRIGGER_PCT = float(os.getenv("BREAKEVEN_TRIGGER_PCT", "0.7"))

# ── Trailing stop por ATR, activo solo despues del breakeven ──
# Distancia = ATR(14) de M5 x este multiplicador. 1.2 es el mismo que
# usa strategy_ema_pullback() en signal_engine.py para su SL original.
TRAILING_ATR_MULTIPLIER = float(os.getenv("TRAILING_ATR_MULTIPLIER", "1.2"))
TRAILING_ATR_PERIOD = 14
# ──────────────────────────────────────────────────────────────

# ── Filtro de spread maximo — NUEVO 2026-08-18 / AMPLIADO 2026-08-26 ──
# En aperturas de killzone (justo cuando opera Sweep Displacement M1)
# el spread de XAUUSD se ensancha, y un SL de M1 calculado con precision
# milimetrica puede quedar barrido de inmediato por el spread mismo,
# no por el movimiento real del mercado. Se rechaza la ejecucion (no
# la señal en Supabase, que sigue PENDING para el proximo ciclo) si el
# spread actual supera el maximo permitido. Originalmente solo aplicaba
# a Sweep Displacement M1 (la mas sensible por operar en M1 con SL
# ajustado). AMPLIADO tras revisar trades del 19-23 ago: FVG Fill M5 y
# EMA Pullback M5 tambien tuvieron stop-outs en 1-2 minutos por spikes
# de spread/volatilidad que su ATR(14) promedio no alcanzo a capturar
# a tiempo — se agregan aqui por el mismo motivo.
SPREAD_FILTER_STRATEGIES = ["Sweep Displacement M1"]
MAX_SPREAD_POINTS = int(os.getenv("MAX_SPREAD_POINTS", "35"))  # ajustar segun spread tipico real de GOLD en XMGlobal

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
    "Sweep Displacement M1",
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


# ── FIX 2026-08-11: conversion de hora RD -> hora de servidor del broker ──
# Mismo problema y misma solucion que ya existe en data_engine.py: XMGlobal
# usa horario europeo (EET invierno UTC+2 / EEST verano UTC+3), y los
# timestamps que devuelve mt5.history_deals_get() vienen en esa hora de
# servidor, no en hora local de RD (UTC-4 fijo, sin horario de verano).
# Sin esta conversion, get_daily_losses()/get_killzone_losses() consultaban
# una ventana horaria desfasada y dejaban pasar perdidas que ya debian
# contar para los limites de proteccion — bug confirmado el 2026-08-11
# (5ta operacion del dia ejecutada cuando ya debian estar 4/4 y 2/2 killzone).

def eu_dst_active(dt_utc):
    """
    Determina si, en la fecha dada, el horario de verano europeo (EEST,
    UTC+3) esta activo en vez del horario de invierno (EET, UTC+2).
    Regla de la UE: DST va desde el ultimo domingo de marzo (01:00 UTC)
    hasta el ultimo domingo de octubre (01:00 UTC).
    """
    year = dt_utc.year

    def last_sunday(month):
        if month == 12:
            d = datetime(year, 12, 31, tzinfo=timezone.utc)
        else:
            d = datetime(year, month + 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
        while d.weekday() != 6:
            d -= timedelta(days=1)
        return d

    dst_start = last_sunday(3).replace(hour=1)
    dst_end = last_sunday(10).replace(hour=1)
    return dst_start <= dt_utc < dst_end


def eet_offset_hours(dt_utc):
    """Offset correcto (2h invierno EET, 3h verano EEST) segun la fecha."""
    return 3 if eu_dst_active(dt_utc) else 2


def rd_to_broker_time(dt_rd_naive):
    """
    Convierte un datetime naive en hora local RD (UTC-4 fijo) a la hora
    de servidor del broker (EET/EEST), que es la que interpreta
    mt5.history_deals_get(). Devuelve un datetime naive listo para pasar
    directo a la API de MT5.
    """
    dt_utc = (dt_rd_naive + timedelta(hours=4)).replace(tzinfo=timezone.utc)
    offset = eet_offset_hours(dt_utc)
    dt_broker = dt_utc + timedelta(hours=offset)
    return dt_broker.replace(tzinfo=None)


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
    # FIX 2026-08-11: convertir el rango de hora RD a hora de servidor
    # del broker antes de consultar — antes se pasaba hora local RD
    # directo y la ventana quedaba desfasada frente a los deals reales.
    today_start_rd = datetime.combine(date.today(), datetime.min.time())
    now_rd = datetime.now()
    start_broker = rd_to_broker_time(today_start_rd)
    end_broker = rd_to_broker_time(now_rd)
    closing_deals = _closing_deals_between(start_broker, end_broker)
    return sum(1 for d in closing_deals if d.profit < 0)


def get_daily_wins():
    # FIX 2026-08-11: mismo ajuste de zona horaria que get_daily_losses().
    today_start_rd = datetime.combine(date.today(), datetime.min.time())
    now_rd = datetime.now()
    start_broker = rd_to_broker_time(today_start_rd)
    end_broker = rd_to_broker_time(now_rd)
    closing_deals = _closing_deals_between(start_broker, end_broker)
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
    cierre cayo dentro de la ventana horaria [start_time, end_time) de HOY,
    en hora RD. Se usa para el limite MAX_LOSSES_PER_KILLZONE, independiente
    del limite diario general.
    FIX 2026-08-11: la ventana se define en hora RD (igual que siempre)
    pero se convierte a hora de servidor del broker antes de consultar a
    MT5 — mismo bug y misma correccion que get_daily_losses().
    """
    today = date.today()
    window_start_rd = datetime.combine(today, start_time)
    window_end_rd = datetime.combine(today, end_time)
    start_broker = rd_to_broker_time(window_start_rd)
    end_broker = rd_to_broker_time(window_end_rd)
    closing_deals = _closing_deals_between(start_broker, end_broker)
    return sum(1 for d in closing_deals if d.profit < 0)


def get_outside_killzone_losses():
    """
    Perdidas de HOY que cerraron fuera de ambas killzones (Londres
    3-6 RD / NYC 9-12 RD). Se calcula por diferencia sobre funciones
    ya probadas (get_daily_losses / get_killzone_losses) en vez de
    reconvertir horarios de nuevo, para no repetir la logica de
    rd_to_broker_time() y arriesgar otro bug de zona horaria.
    """
    total = get_daily_losses()
    london_start, london_end = KILLZONE_LONDON[1], KILLZONE_LONDON[2]
    nyc_start, nyc_end = KILLZONE_NYC[1], KILLZONE_NYC[2]
    london_losses = get_killzone_losses(london_start, london_end)
    nyc_losses = get_killzone_losses(nyc_start, nyc_end)
    return total - london_losses - nyc_losses


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


def get_signal_info_by_position_comment(comment):
    """
    El comment de la orden real en MT5 es 'TradingPro_{signal_id[:8]}'
    (ver execute_order). Busca en Supabase la señal cuyo id empieza
    con ese prefijo y devuelve (signal_id, strategy) — antes esta
    funcion (get_strategy_by_position_comment) solo devolvia strategy,
    lo cual bastaba para decidir si aplicar CHoCH/time-stop pero no
    alcanzaba para poder cerrar la señal en Supabase despues de un
    cierre anticipado real en MT5 (ver update_signal_closed y el FIX
    2026-08-17 en el bloque de comentarios de arriba).
    Devuelve (None, None) si no se pudo resolver.
    """
    if not comment or not comment.startswith("TradingPro_"):
        return None, None
    prefix = comment.replace("TradingPro_", "").strip()
    if not prefix:
        return None, None
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/signals",
            headers=headers(),
            params={
                "id":     f"like.{prefix}*",
                "select": "id,strategy",
                "limit":  "1",
            },
            timeout=15,
        )
        if r.status_code >= 400:
            return None, None
        data = r.json()
        if not data:
            return None, None
        return data[0]["id"], data[0]["strategy"]
    except Exception:
        return None, None


def update_signal_closed(sig_id, result, exit_price=None, intentos=3):
    """
    NUEVO 2026-08-17: cierra en Supabase (status=CLOSED, result=WIN/LOSS)
    una señal cuya posicion real en MT5 se cerro por un camino que NO es
    el flujo normal de result_tracker.py (CHoCH exit o time-stop). Sin
    esto, la señal se quedaba con status=EXECUTING para siempre y
    result_tracker.py la seguia "viendo" como posicion abierta,
    simulando su recorrido con velas M5 desde cero — generando un
    WIN/LOSS ficticio en Telegram que no correspondia al cierre real.
    Reintenta igual que update_signal_status, por consistencia.
    """
    if not sig_id or not SUPABASE_URL or not SUPABASE_KEY:
        return False

    ultimo_error = None
    for intento in range(1, intentos + 1):
        try:
            r = requests.patch(
                f"{SUPABASE_URL}/rest/v1/signals?id=eq.{sig_id}",
                headers=headers(),
                json={"status": "CLOSED", "result": result},
                timeout=15,
            )
            if r.status_code < 400:
                return True
            ultimo_error = f"HTTP {r.status_code}: {r.text}"
        except Exception as e:
            ultimo_error = str(e)

        print(f"  [SUPABASE] Intento {intento}/{intentos} fallo al cerrar señal {sig_id}: {ultimo_error}")
        if intento < intentos:
            time.sleep(2)

    log_error_to_file(
        f"NO SE PUDO CERRAR SEÑAL tras {intentos} intentos. "
        f"Señal: {sig_id} -> CLOSED/{result} | Error: {ultimo_error}"
    )
    return False


def close_position_market(position, motivo=""):
    """
    Cierra a mercado el 100% de una posicion abierta (orden inversa
    sobre el mismo ticket). Se usa para el cierre anticipado por CHoCH
    en contra, y para el cierre por tiempo (time-stop) — no espera a
    que el precio toque SL o TP1.
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
        "comment":      motivo or "CHoCH_exit",
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


# ── Breakeven real al 70% del camino a TP1 ──────────────────────

_breakeven_applied = set()  # tickets ya movidos a breakeven en esta sesion
_trailing_activated = set()  # tickets donde ya se aviso el primer trailing


def calc_atr_m5(candles, period=14):
    """
    ATR(period) sobre velas M5 (mismo calculo que usa signal_engine.py
    en calc_atr). candles debe traer high/low/close en orden
    cronologico — get_recent_m5_candles() ya las devuelve asi.
    """
    if len(candles) < period + 1:
        return 0
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i]["high"])
        l = float(candles[i]["low"])
        pc = float(candles[i - 1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period


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
    Si el precio ya recorrio BREAKEVEN_TRIGGER_PCT (70% por defecto)
    de la distancia entre la entrada y el TP1 (position.tp, el mismo
    TP1 que se fijo al abrir la orden), mueve el SL real a la entrada.
    Se aplica una sola vez por ticket. Independiente de la estrategia
    y de la gestion por CHoCH.
    """
    if position.ticket in _breakeven_applied:
        return False

    entry = position.price_open
    tp1 = position.tp

    # FIX 2026-08-11: antes solo detectaba "ya en breakeven" si el SL
    # estaba CASI EXACTO en la entrada. Si el trailing ya lo habia
    # movido bien por delante (mas protegido) y el bot se reinicia
    # (se pierde _breakeven_applied en memoria), esa condicion no
    # reconocia el avance y volvia a mandar el SL A LA ENTRADA — un
    # retroceso real de proteccion. Ahora se reconoce "ya protegido"
    # si el SL esta EN O MAS ALLA de la entrada (a favor del trade),
    # sin importar cuanto haya avanzado el trailing.
    if position.sl:
        ya_protegido = (
            (side == "BUY" and position.sl >= entry - 0.01) or
            (side == "SELL" and position.sl <= entry + 0.01)
        )
        if ya_protegido:
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


def check_trailing_stop(position, side, now_str):
    """
    Una vez que el breakeven ya esta activo en esta posicion (ticket en
    _breakeven_applied), arrastra el SL detras del precio a una
    distancia de ATR(14) de M5 x TRAILING_ATR_MULTIPLIER. Nunca antes
    del breakeven — hasta ese punto el SL sigue siendo el anti-hunt
    original.
    Regla dura: el SL SOLO se mueve a favor del trade, nunca hacia
    atras (no le da mas espacio del que ya se gano).
    Devuelve True si movio el SL, False si no hizo nada.
    """
    if position.ticket not in _breakeven_applied:
        return False

    candles = get_recent_m5_candles(30)
    atr = calc_atr_m5(candles, TRAILING_ATR_PERIOD)
    if atr <= 0:
        return False

    tick = mt5.symbol_info_tick(MT5_SYMBOL)
    if tick is None:
        return False

    entry = position.price_open
    precio_actual = tick.bid if side == "BUY" else tick.ask
    distancia = atr * TRAILING_ATR_MULTIPLIER

    # FIX 2026-08-11: el objeto `position` que llega aqui se obtuvo UNA
    # sola vez al inicio del ciclo, ANTES de que check_breakeven() (que
    # corre justo antes en run_cycle) pudiera haber modificado el SL
    # real en MT5 este mismo ciclo. Comparar directo contra
    # `position.sl` en ese caso usa un valor desactualizado (el
    # anti-hunt viejo, muy por debajo de la entrada) y puede dejar
    # pasar un candidato de trailing PEOR que la entrada — rompiendo la
    # garantia de "peor caso = $0" que el breakeven acaba de fijar.
    # Se corrige con dos medidas independientes:
    #   1. nuevo_sl nunca puede ser peor que la entrada, sin importar
    #      lo que diga el ATR (piso/techo duro).
    #   2. la base de comparacion tambien se corrige a "al menos la
    #      entrada", para no comparar contra el SL viejo desactualizado.
    if side == "BUY":
        nuevo_sl = round(precio_actual - distancia, 2)
        nuevo_sl = max(nuevo_sl, entry)  # nunca peor que breakeven
        sl_base = max(position.sl, entry) if position.sl else entry
        if nuevo_sl <= sl_base:
            return False
    else:
        nuevo_sl = round(precio_actual + distancia, 2)
        nuevo_sl = min(nuevo_sl, entry)  # nunca peor que breakeven
        sl_base = min(position.sl, entry) if position.sl else entry
        if nuevo_sl >= sl_base:
            return False

    ok, error_detail = modify_position_sltp(position, new_sl=nuevo_sl)

    if not ok:
        print(f"  [TRAILING] Error moviendo SL en ticket {position.ticket}: {error_detail}")
        log_error_to_file(f"Trailing ATR fallo — ticket {position.ticket}: {error_detail}")
        return False

    print(
        f"  [TRAILING] Ticket {position.ticket} — SL movido a {nuevo_sl} "
        f"(ATR={atr:.2f} x {TRAILING_ATR_MULTIPLIER})"
    )

    if position.ticket not in _trailing_activated:
        _trailing_activated.add(position.ticket)
        send_telegram(
            f"[BOT] TRAILING STOP ACTIVADO\n"
            f"Ticket: {position.ticket}\n"
            f"SL ahora en: {nuevo_sl}\n"
            f"Distancia: ATR {atr:.2f} x {TRAILING_ATR_MULTIPLIER}\n"
            f"El SL seguira moviendose a favor del trade mientras siga ganando.\n"
            f"Hora: {now_str}"
        )

    return True


def check_time_stop(position, sig_id, strategy, now_str):
    """
    Cierra la posicion si lleva mas de TIME_STOP_MINUTES abierta y
    todavia no llego a breakeven (no alcanzo el 70% del camino a TP1).
    Solo aplica a estrategias en TIME_STOP_STRATEGIES.

    NUEVO 2026-08-17: historico real de EMA Pullback M5 (25 trades) —
    los ganadores resuelven en ~15-20 min, los perdedores se alargan
    35-100+ min sin llegar a breakeven. En vez de dejar que la posicion
    siga sangrando tiempo sin confirmar la tesis del pullback, se cierra
    a mercado despues de TIME_STOP_MINUTES si no hay progreso real.
    Devuelve True si cerro la posicion, False si no hizo nada.

    FIX 2026-08-17 (señal huerfana en Supabase): ademas de cerrar la
    posicion real en MT5, ahora tambien cierra la señal correspondiente
    en Supabase (status=CLOSED, result=WIN/LOSS segun profit real) para
    que result_tracker.py deje de "verla" como EXECUTING y no simule un
    resultado ficticio sobre un trade que ya no existe.
    """
    if strategy not in TIME_STOP_STRATEGIES:
        return False

    if position.ticket in _breakeven_applied:
        return False  # ya iba ganando lo suficiente, no forzar salida

    open_time_utc = datetime.fromtimestamp(position.time, tz=timezone.utc)
    elapsed_min = (datetime.now(timezone.utc) - open_time_utc).total_seconds() / 60

    if elapsed_min < TIME_STOP_MINUTES:
        return False

    profit_antes = round(position.profit, 2)
    ok, close_price, error_detail = close_position_market(position, motivo="time_stop")

    if not ok:
        print(f"  [TIME STOP] Error cerrando ticket {position.ticket}: {error_detail}")
        log_error_to_file(f"Time stop fallo — ticket {position.ticket} ({strategy}): {error_detail}")
        return False

    print(
        f"  [TIME STOP] {strategy} — {elapsed_min:.0f} min sin llegar a breakeven. "
        f"Posicion {position.ticket} cerrada @ {close_price} | Profit aprox: ${profit_antes}"
    )

    # FIX 2026-08-17: cerrar la señal en Supabase para que result_tracker.py
    # no la siga tratando como EXECUTING y simule un resultado ficticio.
    resultado = "WIN" if profit_antes > 0 else "LOSS"
    if sig_id:
        cerrada_ok = update_signal_closed(sig_id, resultado, close_price)
        if not cerrada_ok:
            print(f"  [TIME STOP] AVISO: no se pudo cerrar la señal {sig_id[:8]} en Supabase")
    else:
        print("  [TIME STOP] AVISO: no se encontro signal_id — la señal puede quedar EXECUTING en Supabase")
        log_error_to_file(
            f"Time stop cerro ticket {position.ticket} pero no se pudo resolver signal_id "
            f"desde el comment de la posicion"
        )

    send_telegram(
        f"[BOT] CIERRE POR TIEMPO — {strategy}\n"
        f"Motivo: {elapsed_min:.0f} min abierta sin alcanzar breakeven\n"
        f"Ticket: {position.ticket}\n"
        f"Precio de cierre: {close_price}\n"
        f"Profit aprox.: ${profit_antes}\n"
        f"Hora: {now_str}"
    )
    return True


def check_choch_exit(position, side, sig_id, strategy, now_str):
    """
    Si la posicion abierta pertenece a una estrategia con gestion de
    salida temprana (EARLY_EXIT_STRATEGIES) y aparece un CHoCH en
    contra de su direccion, la cierra de inmediato. Devuelve True si
    cerro la posicion (para que run_cycle no siga evaluando nada mas
    ese ciclo), False si no hizo nada.

    FIX 2026-08-17 (señal huerfana en Supabase): ademas de cerrar la
    posicion real en MT5, ahora tambien cierra la señal correspondiente
    en Supabase (status=CLOSED, result=WIN/LOSS segun profit real).
    Antes, un cierre por CHoCH dejaba la señal con status=EXECUTING
    para siempre, y result_tracker.py la seguia simulando su recorrido
    con velas M5 desde cero — reportando por Telegram un WIN/LOSS
    ficticio que no correspondia al cierre real ya ocurrido en MT5.
    Caso detectado: señal con entrada 4402.65 (EMA Pullback M5)
    reportada como LOSS por result_tracker.py aunque ya se habia
    cerrado antes por esta via.
    """
    if strategy not in EARLY_EXIT_STRATEGIES:
        return False

    candles = get_recent_m5_candles(30)
    choch = detect_choch(candles)

    if not choch or choch == side:
        return False

    profit_antes = round(position.profit, 2)
    ok, close_price, error_detail = close_position_market(position, motivo="CHoCH_exit")

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

    # FIX 2026-08-17: cerrar la señal en Supabase — ver docstring arriba.
    resultado = "WIN" if profit_antes > 0 else "LOSS"
    if sig_id:
        cerrada_ok = update_signal_closed(sig_id, resultado, close_price)
        if not cerrada_ok:
            print(f"  [CHoCH EXIT] AVISO: no se pudo cerrar la señal {sig_id[:8]} en Supabase")
    else:
        print("  [CHoCH EXIT] AVISO: no se encontro signal_id — la señal puede quedar EXECUTING en Supabase")
        log_error_to_file(
            f"CHoCH exit cerro ticket {position.ticket} pero no se pudo resolver signal_id "
            f"desde el comment de la posicion"
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


def spread_actual_ok():
    """Lee el spread actual del simbolo en puntos (ya calculado por MT5
    en symbol_info().spread, no hace falta recalcular ask-bid a mano).
    Devuelve (ok, spread_points). Si no se puede leer el simbolo, se
    deja pasar (fail-open) en vez de bloquear por un problema de datos
    — mismo criterio que ya usa calc_anti_hunt_sl() cuando symbol_info
    devuelve None."""
    symbol = mt5.symbol_info(MT5_SYMBOL)
    if symbol is None:
        return True, None
    return symbol.spread <= MAX_SPREAD_POINTS, symbol.spread


def log_spread_actual():
    """NUEVO 2026-08-28: registra el spread actual en Supabase en cada
    ciclo (no solo cuando rechaza una orden), para poder analizar despues
    la distribucion real del spread de GOLD en XMGlobal y calibrar
    MAX_SPREAD_POINTS con datos en vez de con dos muestras sueltas.
    No bloquea el ciclo si falla: es solo telemetria."""
    symbol = mt5.symbol_info(MT5_SYMBOL)
    if symbol is None or not SUPABASE_URL or not SUPABASE_KEY:
        return

    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/spread_log",
            headers=headers(),
            json={"spread_pts": symbol.spread},
            timeout=10,
        )
    except Exception as e:
        print(f"  [SPREAD_LOG] fallo al registrar spread: {e}")


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

    # NUEVO 2026-08-18: filtro de spread maximo, solo para estrategias
    # sensibles (Sweep Displacement M1 por ahora). La señal en Supabase
    # NO se marca FAILED por esto en el momento en que se descarta aqui
    # — el rechazo ocurre a nivel de ejecucion, mismo tratamiento que
    # cualquier otro fallo de execute_order() (ver run_cycle: marca
    # FAILED recien despues de que execute_order devuelve None).
    if signal["strategy"] in SPREAD_FILTER_STRATEGIES:
        spread_ok, spread_pts = spread_actual_ok()
        if not spread_ok:
            print(f"  Spread actual {spread_pts} pts > maximo {MAX_SPREAD_POINTS} pts para {signal['strategy']} — orden rechazada")
            return None, f"Spread demasiado ancho ({spread_pts} pts > {MAX_SPREAD_POINTS} max) para {signal['strategy']}"

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
                          # (incluye la clave especial "OUTSIDE_KZ")


def run_cycle():
    global _loss_alert_sent_date
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    log_spread_actual()

    # ── FIX 2026-08-25 (bug critico: gestion de posicion saltada por limites) ──
    # ANTES: los 4 chequeos de limite (MAX_DAILY, MAX_LOSSES_PER_DAY,
    # MAX_LOSSES_PER_KILLZONE, MAX_LOSSES_OUTSIDE_KILLZONE) se evaluaban
    # PRIMERO, y cualquiera de ellos hacia `return` antes de llegar al
    # bloque que gestiona una posicion YA ABIERTA (breakeven, trailing
    # stop, time-stop, CHoCH exit). Resultado: en cuanto se disparaba
    # cualquier limite mientras habia una posicion abierta, esa posicion
    # dejaba de recibir CUALQUIER gestion activa por el resto de su vida
    # — se quedaba solo con su SL/TP original (anti-hunt), sin breakeven,
    # sin trailing, sin time-stop, sin CHoCH exit. Caso mas facil de
    # disparar: la ultima operacion permitida del dia (MAX_DAILY), que en
    # el ciclo siguiente a abrirse ya cumple daily_count >= MAX_DAILY.
    # FIX: la gestion de la posicion abierta se mueve ANTES de los 4
    # limites. Los limites solo deben bloquear la apertura de OPERACIONES
    # NUEVAS, nunca la gestion de una posicion que ya esta en curso —
    # de hecho, es justo cuando el bot esta "pausado" por limites que mas
    # importa que la posicion abierta siga protegida activamente.
    open_positions = get_open_positions()
    if open_positions:
        pos = open_positions[0]
        side = "BUY" if pos.type == 0 else "SELL"
        print(
            f"  [{now_str}] Posicion abierta: ticket {pos.ticket} | {side} | "
            f"Profit: ${round(pos.profit, 2)}"
        )

        # ── NUEVO: breakeven real al 70% del camino a TP1 (todas las estrategias) ──
        check_breakeven(pos, side, now_str)

        # ── NUEVO: trailing stop por ATR, activo solo despues del breakeven ──
        check_trailing_stop(pos, side, now_str)

        sig_id, strategy = get_signal_info_by_position_comment(getattr(pos, "comment", None))

        # ── NUEVO 2026-08-17: cierre por tiempo si no llega a breakeven ──
        cerrada_por_tiempo = check_time_stop(pos, sig_id, strategy, now_str)

        # ── NUEVO: gestion de salida por cambio de estructura (CHoCH) ──
        if not cerrada_por_tiempo and strategy in EARLY_EXIT_STRATEGIES:
            check_choch_exit(pos, side, sig_id, strategy, now_str)

        return

    # ── A partir de aqui NO hay posicion abierta: los limites solo
    # deciden si se permite ABRIR una operacion nueva. ──

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

    # ── NUEVO 2026-08-17: limite de perdidas FUERA de ambas killzones ──
    if kz is None:
        outside_losses = get_outside_killzone_losses()
        if outside_losses >= MAX_LOSSES_OUTSIDE_KILLZONE:
            print(
                f"  [{now_str}] Limite de perdidas fuera de killzone alcanzado "
                f"({outside_losses}/{MAX_LOSSES_OUTSIDE_KILLZONE}). Se pausa fuera de Londres/NYC."
            )
            alert_key = (date.today(), "OUTSIDE_KZ")
            if _kz_loss_alert_sent.get(alert_key) is not True:
                send_telegram(
                    f"[BOT] Limite de perdidas fuera de killzone — "
                    f"{outside_losses}/{MAX_LOSSES_OUTSIDE_KILLZONE}\n"
                    f"El bot pausa fuera de Londres (03:00-06:00) y NYC (09:00-12:00) RD. "
                    f"Se reactiva al entrar la proxima killzone.\n"
                    f"Hora: {now_str}"
                )
                _kz_loss_alert_sent[alert_key] = True
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
    print(f"  Max perdidas fuera de killzone: {MAX_LOSSES_OUTSIDE_KILLZONE}")
    print(f"  [FIX 2026-08-11] Conteo de perdidas ahora corrige offset horario RD -> servidor broker (EET/EEST)")
    print(f"  Breakeven real al {BREAKEVEN_TRIGGER_PCT*100:.0f}% del camino a TP1 (todas las estrategias)")
    print(f"  Trailing stop por ATR x{TRAILING_ATR_MULTIPLIER} despues del breakeven (todas las estrategias)")
    print(f"  Salida por CHoCH activa para: {', '.join(EARLY_EXIT_STRATEGIES)}")
    print(f"  Time-stop ({TIME_STOP_MINUTES} min) activo para: {', '.join(TIME_STOP_STRATEGIES)}")
    print(f"  Filtro de spread maximo ({MAX_SPREAD_POINTS} pts) activo para: {', '.join(SPREAD_FILTER_STRATEGIES)}")
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
            f"Max perdidas/dia: {MAX_LOSSES_PER_DAY} | Max perdidas/killzone: {MAX_LOSSES_PER_KILLZONE} | "
            f"Max perdidas fuera de killzone: {MAX_LOSSES_OUTSIDE_KILLZONE}\n"
            f"Time-stop {TIME_STOP_MINUTES} min activo para: {', '.join(TIME_STOP_STRATEGIES)}\n"
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
