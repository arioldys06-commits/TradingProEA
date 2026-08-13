"""
sync_trades_supabase.py
------------------------
Sincroniza los deals (trades) CERRADOS en MT5 hacia la tabla `trades_ejecutados`
en Supabase, para poder construir el dashboard y el reporte diario de Telegram.

REQUISITOS:
- Variables de entorno ya existentes: SUPABASE_URL, SUPABASE_KEY (o SERVICE_ROLE_KEY),
  MT5_MAGIC (20260601), MT5_SYMBOL (GOLD)

FIX EN ESTA VERSION (incluir operaciones manuales para P&L mensual):
  - Hasta ahora, tanto `aperturas` como `deals_cierre` filtraban
    exclusivamente por `d.magic == MAGIC`. Las operaciones abiertas a
    mano en MT5 (fuera del bot) normalmente llevan magic = 0, asi que
    quedaban completamente fuera de la sincronizacion — el P&L mensual
    en Supabase nunca reflejaba esas operaciones, aunque afectaran el
    balance real de la cuenta igual que las del bot.
  - Ahora el filtro es por SYMBOL en vez de por MAGIC: se sincronizan
    TODOS los deals cerrados de GOLD en esta cuenta, sea cual sea su
    magic number. Se agrega un campo nuevo `origen` a cada registro:
      - "BOT" si el magic del deal coincide con MAGIC (20260601)
      - "MANUAL" si el magic es 0 (abierta a mano en el terminal)
      - "OTRO(magic=N)" para cualquier otro magic no reconocido, como
        aviso por si en el futuro corre otro EA en la misma cuenta —
        mejor visibilizarlo que mezclarlo silenciosamente con BOT o
        MANUAL.
  - El vinculo a `strategy`/`signal_id` sigue funcionando igual para
    las operaciones del bot (via el comentario "TradingPro_xxxxxxxx").
    Las manuales simplemente no encuentran coincidencia de comentario
    y quedan sin signal_id/strategy — es el comportamiento correcto,
    ya que no vienen de ninguna señal generada por el sistema.
  - `origen` se incluye SIEMPRE en el registro (a diferencia de
    signal_id/strategy/open_time/precio_apertura, que se omiten
    cuando no hay dato — ver el fix de mas abajo sobre por que). Esto
    es seguro porque `origen` siempre es calculable de forma directa
    desde el magic del deal de cierre, sin depender de ningun cruce
    que pueda fallar.

FIX EN ESTA VERSION (perdida de vinculo strategy/signal_id en re-sincronizaciones):
  - BUG ENCONTRADO el 2026-08-07: si cargar_signals_cache() fallaba por
    cualquier motivo (timeout, corte de red), el codigo anterior dejaba
    _signals_cache = {} y SEGUIA con la sincronizacion igual. Con el
    cache vacio, ningun trade de ese ciclo encontraba su señal — y como
    el script hace upsert escribiendo signal_id/strategy explicitamente
    (incluso como None), sobreescribia con null datos que YA estaban
    correctamente vinculados de un ciclo anterior. Confirmado: 4 trades
    de EMA Pullback M5 del 2026-08-05 que ya tenian su estrategia bien
    vinculada aparecieron en null tras una resincronizacion posterior.
    Se repararon manualmente cruzando precio_cierre contra la señal
    original, pero el codigo tenia que corregirse para que no vuelva a
    pasar.
  - Dos cambios:
    1. Si cargar_signals_cache() falla, sincronizar_trades_cerrados()
       AHORA ABORTA el ciclo completo (no sube nada) en vez de seguir
       con el cache vacio. Es preferible perder un ciclo de
       sincronizacion (90s) que corromper datos ya buenos.
    2. Aunque el cache cargue bien, si un trade puntual no encuentra
       coincidencia (señal muy vieja, orden abierta manualmente sin
       pasar por el bot, etc.), las claves "signal_id" y "strategy" ya
       NO se incluyen en el registro que se manda a Supabase — en vez
       de mandarlas como None. Un upsert que omite una clave no toca
       esa columna en la fila existente, mientras que mandarla como
       None SI la sobreescribe. Asi un trade que ya tenia vinculo
       correcto nunca se puede corromper por un fallo de match en un
       ciclo posterior.

FIX EN ESTA VERSION (offset de zona horaria del servidor del broker):
  - Los timestamps de los deals en MT5 (d.time) vienen en hora del
    SERVIDOR del broker (XMGlobal, EEST = UTC+3 en verano / EET = UTC+2
    en invierno), NO en UTC real — aunque se entreguen como epoch Unix.
    Confirmado el 2026-08-07 comparando open_time contra el created_at
    de la señal que origino cada trade: sin corregir este offset, el
    open_time quedaba 3 horas adelantado respecto a la señal.
  - Se intento detectar el offset dinamicamente comparando la hora del
    ultimo TICK en vivo de MT5 contra datetime.now(timezone.utc) real
    del sistema. NO funciono: el feed de precios en vivo (ticks) SI
    viene sincronizado a UTC real en este broker, pero el HISTORIAL de
    deals cerrados guarda la hora del servidor al momento de la
    ejecucion — son dos relojes distintos dentro del mismo MT5.
    Comparar contra el tick daba offset 0 y no corregia nada.
  - Por eso el offset queda como variable fija en .env
    (BROKER_UTC_OFFSET_HOURS, por defecto 3 = EEST). Hay que
    actualizarla a mano 2 veces al año cuando cambia el horario de
    verano europeo (a 2 en otoño/invierno, de vuelta a 3 en primavera).
    Menos elegante que la deteccion automatica, pero es lo confiable
    dado que este MT5 no expone un reloj de referencia consistente
    entre ticks en vivo e historial de deals.

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
from collections import defaultdict
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
# Offset del servidor del broker respecto a UTC real, en horas. XMGlobal
# corre en EEST (UTC+3) en horario de verano europeo, EET (UTC+2) en
# invierno. Ver docstring arriba (FIX offset de zona horaria) sobre por
# que esto NO se puede detectar automaticamente en este broker.
BROKER_UTC_OFFSET_HOURS = float(os.getenv("BROKER_UTC_OFFSET_HOURS", "3"))

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

    Devuelve True si la carga fue exitosa, False si fallo. Es
    responsabilidad de quien llama a esta funcion NO continuar con la
    sincronizacion si devuelve False — ver el FIX en el docstring del
    modulo (perdida de vinculo strategy/signal_id) sobre por que
    seguir con el cache vacio es peligroso: causaria que CADA trade
    de ese ciclo se re-suba con signal_id/strategy en null, incluso
    los que ya estaban correctamente vinculados de un ciclo anterior.
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
        return True
    except Exception as e:
        print(f"[sync_trades] Error cargando cache de señales: {e}")
        _signals_cache = {}
        return False


def epoch_broker_to_utc_iso(epoch_broker_time):
    """Convierte un epoch en hora del broker a UTC real (ISO), restando
    BROKER_UTC_OFFSET_HOURS (configurable en .env — ver nota arriba)."""
    return datetime.fromtimestamp(
        epoch_broker_time - BROKER_UTC_OFFSET_HOURS * 3600, tz=timezone.utc
    ).isoformat()


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


def determinar_origen(magic: int) -> str:
    """
    Clasifica el deal segun su magic number:
      - MAGIC del bot -> "BOT"
      - 0 (default de MT5 para ordenes manuales) -> "MANUAL"
      - cualquier otro -> "OTRO(magic=N)", para que quede visible si en
        el futuro corre otro EA distinto en la misma cuenta, en vez de
        mezclarlo silenciosamente con BOT o MANUAL.
    """
    if magic == MAGIC:
        return "BOT"
    if magic == 0:
        return "MANUAL"
    return f"OTRO(magic={magic})"


def sincronizar_trades_cerrados(dias_atras: int = 3):
    """
    Lee los deals de MT5 de los últimos `dias_atras` días, separa los de
    apertura (ENTRY_IN) de los de cierre (ENTRY_OUT), vincula cada cierre
    con su señal original (strategy + signal_id) usando el comentario del
    deal de APERTURA — no el de cierre — y hace upsert en Supabase
    (trades_ejecutados) usando el ticket como llave única.

    Se sincronizan TODOS los deals de SYMBOL (no solo los del bot) para
    poder medir el P&L real de la cuenta a fin de mes, incluyendo
    operaciones manuales — ver FIX arriba (incluir operaciones manuales).

    IMPORTANTE: MT5 sobreescribe automaticamente el comentario del deal
    de cierre con la razon del cierre (ej. "[sl 4039.51]", "[tp 4031.86]"),
    perdiendo el comentario original "TradingPro_xxxxxxxx" que puso
    bot_engine.py al abrir la orden. Ese comentario original SI se
    conserva en el deal de apertura (ENTRY_IN) de la misma posicion, asi
    que hay que buscarlo ahi usando el position_id en comun — y de paso
    se aprovecha ese mismo deal para tomar open_time y precio_apertura.

    Todos los timestamps de MT5 (d.time) vienen en hora del SERVIDOR
    del broker (XMGlobal, EEST/EET), no en UTC real, aunque se
    entreguen como epoch Unix. Se corrige restando
    BROKER_UTC_OFFSET_HOURS (configurable en .env, ver nota arriba
    sobre por que no se puede detectar automaticamente en este broker).
    """
    desde = datetime.now() - timedelta(days=dias_atras)
    hasta = datetime.now()

    cache_ok = cargar_signals_cache()
    if not cache_ok:
        print(
            "[sync_trades] ABORTADO este ciclo — no se pudo cargar el cache de señales. "
            "Se prefiere saltar una sincronizacion (90s) antes que subir trades sin "
            "vincular y arriesgar sobreescribir datos ya correctos."
        )
        return
    print(f"[sync_trades] Offset del broker configurado: +{BROKER_UTC_OFFSET_HOURS:.0f}h respecto a UTC (BROKER_UTC_OFFSET_HOURS en .env)")

    deals = mt5.history_deals_get(desde, hasta)
    if deals is None or len(deals) == 0:
        print("[sync_trades] No hay deals en el rango consultado.")
        return

    # Info del deal de APERTURA por position_id (comment, time, price) —
    # es el unico que conserva el "TradingPro_xxxxxxxx" que puso
    # bot_engine.py, y de donde sale open_time/precio_apertura. Ya NO se
    # filtra por magic == MAGIC aqui, para tambien capturar la apertura
    # de operaciones manuales (magic 0) y poder tomarles open_time/precio.
    aperturas = {
        d.position_id: {"comment": d.comment, "time": d.time, "price": d.price}
        for d in deals
        if d.symbol == SYMBOL and d.entry == mt5.DEAL_ENTRY_IN
    }

    # Deals de SALIDA (cierre de posición) de este simbolo — de CUALQUIER
    # magic, no solo el del bot, para incluir operaciones manuales en el
    # P&L. entry == 1 significa DEAL_ENTRY_OUT (cierre); entry == 0 apertura.
    deals_cierre = [
        d for d in deals
        if d.symbol == SYMBOL and d.entry == mt5.DEAL_ENTRY_OUT
    ]

    if not deals_cierre:
        print("[sync_trades] No hay trades cerrados nuevos en este símbolo.")
        return

    registros = []
    vinculados = 0
    manuales = 0
    for d in deals_cierre:
        profit_neto = d.profit + d.swap + d.commission

        apertura = aperturas.get(d.position_id)
        comentario_apertura = apertura.get("comment") if apertura else None
        signal_id, strategy = buscar_signal_por_comentario(comentario_apertura)
        if signal_id:
            vinculados += 1

        origen = determinar_origen(d.magic)
        if origen == "MANUAL":
            manuales += 1

        registro = {
            "ticket": d.ticket,
            "magic": d.magic,
            "origen": origen,  # "BOT" / "MANUAL" / "OTRO(magic=N)" — siempre presente
            "symbol": d.symbol or SYMBOL,
            "tipo": "BUY" if d.type == mt5.DEAL_TYPE_BUY else "SELL",
            "volumen": d.volume,
            "precio_cierre": d.price,
            "profit": d.profit,
            "swap": d.swap,
            "comision": d.commission,
            "profit_neto": profit_neto,
            "close_time": epoch_broker_to_utc_iso(d.time),
            "comentario": d.comment,  # razon de cierre (sl/tp) o vacio si fue manual
        }

        # IMPORTANTE: estas 4 claves solo se incluyen si hay dato real.
        # Un upsert que OMITE una clave no toca esa columna en la fila
        # existente; mandarla explicitamente como None SI la
        # sobreescribe. Asi un trade ya vinculado/con datos correctos
        # de un ciclo anterior nunca se corrompe por un fallo de match
        # (deal de apertura fuera de ventana, señal no encontrada, etc.)
        # en un ciclo posterior — ver FIX en el docstring del modulo.
        if apertura:
            registro["open_time"] = epoch_broker_to_utc_iso(apertura["time"])
            registro["precio_apertura"] = apertura["price"]
        if signal_id:
            registro["signal_id"] = signal_id
            registro["strategy"] = strategy

        registros.append(registro)

    # Upsert: si el ticket ya existe, no lo duplica (requiere el UNIQUE en `ticket`)
    #
    # IMPORTANTE: PostgREST exige que TODAS las filas de un mismo upsert
    # en lote tengan exactamente las mismas columnas ("All object keys
    # must match" si no). Como algunas filas de este ciclo pueden traer
    # signal_id/strategy/open_time/precio_apertura y otras no (ver mas
    # arriba, se omiten a proposito cuando no hay dato), se agrupan por
    # su set exacto de columnas y se hace un upsert por grupo — asi cada
    # llamada es homogenea y ninguna fila termina escribiendo None en
    # una columna que no debia tocar.
    grupos = defaultdict(list)
    for registro in registros:
        clave = tuple(sorted(registro.keys()))
        grupos[clave].append(registro)

    for clave, filas in grupos.items():
        supabase.table("trades_ejecutados").upsert(filas, on_conflict="ticket").execute()

    print(
        f"[sync_trades] {len(registros)} trades sincronizados hacia Supabase "
        f"({vinculados} vinculados a su señal/estrategia original, {manuales} manuales)."
    )
    return len(registros)


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
    print(f"  Magic: {MAGIC} | Simbolo: {SYMBOL} (incluye BOT + MANUAL)")
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
