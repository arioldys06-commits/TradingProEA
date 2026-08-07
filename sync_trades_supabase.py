"""
sync_trades_supabase.py
------------------------
Sincroniza los deals (trades) CERRADOS en MT5 hacia la tabla `trades_ejecutados`
en Supabase, para poder construir el dashboard y el reporte diario de Telegram.

REQUISITOS:
- Variables de entorno ya existentes: SUPABASE_URL, SUPABASE_KEY (o SERVICE_ROLE_KEY),
  MT5_MAGIC (20260601), MT5_SYMBOL (GOLD)

FIX EN ESTA VERSION (timezone de close_time + open_time/precio_apertura):
  - close_time se guardaba con datetime.fromtimestamp(d.time), que convierte
    el timestamp Unix (UTC real) a la hora LOCAL de la PC (RD, UTC-4) y
    luego lo guarda sin indicar zona horaria. Supabase, al no ver zona,
    lo interpreta como si ya fuera UTC — restando 4 horas de mas a la
    hora real de cierre. Confirmado el 2026-08-07: los trades de esa
    mañana quedaron con close_time ANTERIOR a la hora en que se creo la
    señal que los origino, lo cual es imposible.
    Fix: datetime.fromtimestamp(d.time, tz=timezone.utc) — convierte el
    epoch directamente a UTC real, sin pasar por la zona local de la PC.
  - open_time y precio_apertura estaban SIEMPRE en null: el diccionario
    comentarios_apertura solo guardaba el comentario del deal de
    apertura, nunca su tiempo ni su precio, aunque ambos ya estaban
    disponibles en ese mismo deal. Ahora se guardan los tres juntos
    (comment, time, price) y se escriben en el registro final.

FIX (version anterior, vinculo strategy/signal_id + loop por defecto):
  - Antes, cada registro subido a `trades_ejecutados` nunca incluia `strategy`
    ni `signal_id`, aunque el dato SI estaba disponible: bot_engine.py pone
    en el comentario de cada orden "TradingPro_{signal_id[:8]}" (los primeros
    8 caracteres del UUID de la señal). El script solo guardaba ese texto tal
    cual en la columna `comentario`, sin usarlo para nada mas — por eso
    `strategy` y `signal_id` quedaban siempre en null, y era imposible
    comparar el rendimiento real de MT5 entre estrategias.
  - Ahora, por cada deal cerrado, se extrae ese prefijo de 8 caracteres del
    comentario y se busca en la tabla `signals` (por id que EMPIECE con ese
    prefijo, via "like"). Si se encuentra, se guarda el `signal_id` completo
    y su `strategy` real en el registro de `trades_ejecutados`.
  - Antes este script corria una sola vez y terminaba (a menos que se
    invocara manualmente con --loop). Como los demas scripts del sistema
    (data_engine, signal_engine, bot_engine, result_tracker) ya corren en
    loop continuo dejandolos abiertos en su propia consola, este ahora hace
    lo mismo por defecto — usa --once si en algun momento se quiere correr
    una sola vez y salir.
"""

import os
import sys
import time
import MetaTrader5 as mt5
from datetime import datetime, timedelta, timezone
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()  # carga las variables del archivo .env en la misma carpeta

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")  # usa service_role si vas a hacer upsert desde script local
MAGIC = int(os.getenv("MT5_MAGIC", "20260601"))
SYMBOL = os.getenv("MT5_SYMBOL", "GOLD")
LOOP_INTERVAL = int(os.getenv("SYNC_LOOP_INTERVAL", "90"))  # segundos entre cada sincronizacion

if not SUPABASE_URL or not SUPABASE_KEY:
    raise SystemExit(
        "[sync_trades] ERROR: No se encontraron SUPABASE_URL / SUPABASE_KEY.\n"
        "Verifica que exista un archivo .env en esta misma carpeta con esas variables,\n"
        "o que los NOMBRES coincidan con los que ya usas en tu bot (ej. SUPABASE_KEY vs SUPABASE_SERVICE_KEY)."
    )

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Prefijo que bot_engine.py pone al inicio de cada comentario de orden,
# seguido de los primeros 8 caracteres del UUID de la señal original.
COMMENT_PREFIX = "TradingPro_"


_signals_cache = {}


def cargar_signals_cache():
    """
    Trae (id, strategy) de las señales recientes UNA SOLA VEZ por ciclo
    de sincronizacion, y arma un diccionario {primeros_8_chars_del_id:
    {"id": id_completo, "strategy": strategy}} para hacer el vinculo en
    memoria.

    Se hace asi en vez de un LIKE por cada trade porque la columna `id`
    en Supabase es de tipo uuid, y Postgres no permite comparar uuid con
    LIKE sin conversion explicita de tipo (error 42883: "operator does
    not exist: uuid ~~ unknown"). Cargar el lote completo una vez y
    comparar en Python evita ese problema de tipos y ademas reduce las
    llamadas a Supabase de "una por trade" a "una por ciclo".

    Se guarda el id COMPLETO (no solo el prefijo) porque la columna
    signal_id en trades_ejecutados tambien es tipo uuid — un string
    parcial de 8 caracteres seria rechazado al hacer el upsert.
    """
    global _signals_cache
    try:
        r = (
            supabase.table("signals")
            .select("id,strategy")
            .order("created_at", desc=True)
            .limit(1000)
            .execute()
        )
        _signals_cache = {
            str(row["id"])[:8]: {"id": row["id"], "strategy": row.get("strategy")}
            for row in (r.data or [])
        }
    except Exception as e:
        print(f"[sync_trades] Error cargando cache de señales: {e}")
        _signals_cache = {}


def buscar_signal_por_comentario(comentario):
    """
    A partir del comentario del deal de APERTURA en MT5 (ej.
    "TradingPro_0c256165"), extrae el prefijo del signal_id y lo busca
    en _signals_cache (cargado una vez por ciclo con cargar_signals_cache()).
    Devuelve (signal_id_completo, strategy) o (None, None) si no coincide
    con ninguna señal conocida — por ejemplo, ordenes abiertas manualmente
    sin pasar por el bot.
    """
    if not comentario or not comentario.startswith(COMMENT_PREFIX):
        return None, None

    prefijo = comentario[len(COMMENT_PREFIX):].strip()
    if len(prefijo) < 4:
        return None, None

    encontrado = _signals_cache.get(prefijo)
    if encontrado:
        return encontrado["id"], encontrado["strategy"]

    return None, None


def sincronizar_trades_cerrados(dias_atras: int = 3):
    """
    Lee los deals de MT5 de los últimos `dias_atras` días, separa los de
    apertura (ENTRY_IN) de los de cierre (ENTRY_OUT), vincula cada cierre
    con su señal original (strategy + signal_id) usando el comentario del
    deal de APERTURA — no el de cierre — y hace upsert en Supabase
    (trades_ejecutados) usando el ticket como llave única.

    IMPORTANTE: MT5 sobreescribe automaticamente el comentario del deal
    de cierre con la razon del cierre (ej. "[sl 4039.51]", "[tp 4031.86]"),
    perdiendo el comentario original "TradingPro_xxxxxxxx" que puso
    bot_engine.py al abrir la orden. Ese comentario original SI se
    conserva en el deal de apertura (ENTRY_IN) de la misma posicion, asi
    que hay que buscarlo ahi usando el position_id en comun — y de paso
    se aprovecha ese mismo deal para tomar open_time y precio_apertura.

    Todos los timestamps de MT5 (d.time) son epoch Unix en UTC real, asi
    que se convierten con datetime.fromtimestamp(d.time, tz=timezone.utc)
    — NUNCA sin tz, porque eso los convierte a la hora local de la PC
    (RD, UTC-4) sin decirlo, y Supabase termina interpretandolos como si
    ya fueran UTC (le resta 4 horas de mas a la hora real de cierre).
    """
    desde = datetime.now() - timedelta(days=dias_atras)
    hasta = datetime.now()

    cargar_signals_cache()

    deals = mt5.history_deals_get(desde, hasta)
    if deals is None or len(deals) == 0:
        print("[sync_trades] No hay deals en el rango consultado.")
        return

    # Info del deal de APERTURA por position_id (comment, time, price) —
    # es el unico que conserva el "TradingPro_xxxxxxxx" que puso
    # bot_engine.py, y de donde sale open_time/precio_apertura.
    aperturas = {
        d.position_id: {"comment": d.comment, "time": d.time, "price": d.price}
        for d in deals
        if d.magic == MAGIC and d.entry == mt5.DEAL_ENTRY_IN
    }

    # Solo nos interesan los deals de SALIDA (cierre de posición) de nuestro bot
    # entry == 1 significa DEAL_ENTRY_OUT (cierre). entry == 0 es apertura.
    deals_cierre = [
        d for d in deals
        if d.magic == MAGIC and d.entry == mt5.DEAL_ENTRY_OUT
    ]

    if not deals_cierre:
        print("[sync_trades] No hay trades cerrados nuevos con ese magic number.")
        return

    registros = []
    vinculados = 0
    for d in deals_cierre:
        profit_neto = d.profit + d.swap + d.commission

        apertura = aperturas.get(d.position_id)
        comentario_apertura = apertura.get("comment") if apertura else None
        signal_id, strategy = buscar_signal_por_comentario(comentario_apertura)
        if signal_id:
            vinculados += 1

        open_time = (
            datetime.fromtimestamp(apertura["time"], tz=timezone.utc).isoformat()
            if apertura else None
        )
        precio_apertura = apertura["price"] if apertura else None

        registros.append({
            "ticket": d.ticket,
            "magic": d.magic,
            "symbol": d.symbol or SYMBOL,
            "tipo": "BUY" if d.type == mt5.DEAL_TYPE_BUY else "SELL",
            "volumen": d.volume,
            "precio_apertura": precio_apertura,
            "precio_cierre": d.price,
            "profit": d.profit,
            "swap": d.swap,
            "comision": d.commission,
            "profit_neto": profit_neto,
            "open_time": open_time,
            "close_time": datetime.fromtimestamp(d.time, tz=timezone.utc).isoformat(),
            "comentario": d.comment,  # razon de cierre (sl/tp), se conserva tal cual
            "signal_id": signal_id,
            "strategy": strategy,
        })

    # Upsert: si el ticket ya existe, no lo duplica (requiere el UNIQUE en `ticket`)
    result = supabase.table("trades_ejecutados").upsert(
        registros, on_conflict="ticket"
    ).execute()

    print(
        f"[sync_trades] {len(registros)} trades sincronizados hacia Supabase "
        f"({vinculados} vinculados a su señal/estrategia original)."
    )
    return result


def loop_continuo(intervalo_segundos: int = None):
    """
    Corre la sincronización en un loop infinito, pensado para dejarlo abierto
    junto al resto del sistema (una consola mas, igual que data_engine.py,
    signal_engine.py, bot_engine.py y result_tracker.py).
    """
    intervalo_segundos = intervalo_segundos or LOOP_INTERVAL

    if not mt5.initialize():
        print("[sync_trades] Error al inicializar MT5:", mt5.last_error())
        return

    print(f"\n{'='*55}")
    print(f"  SYNC TRADES SUPABASE — Loop continuo")
    print(f"  Magic: {MAGIC} | Simbolo: {SYMBOL}")
    print(f"  Sincroniza cada {intervalo_segundos}s — Ctrl+C para detener")
    print(f"{'='*55}\n")

    try:
        while True:
            try:
                sincronizar_trades_cerrados()
            except Exception as e:
                print(f"[sync_trades] Error durante sincronización: {e}")
                # Intenta reconectar MT5 por si se cayó la conexión
                mt5.shutdown()
                mt5.initialize()
            time.sleep(intervalo_segundos)
    except KeyboardInterrupt:
        print("\nDetenido manualmente (Ctrl+C).")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    # Por defecto corre en loop continuo, igual que el resto del sistema.
    # Usa --once si en algun momento quieres una sola corrida y salir.
    if "--once" in sys.argv:
        if not mt5.initialize():
            print("Error al inicializar MT5:", mt5.last_error())
        else:
            sincronizar_trades_cerrados()
            mt5.shutdown()
    else:
        loop_continuo()
