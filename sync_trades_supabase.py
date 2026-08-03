"""
sync_trades_supabase.py
------------------------
Sincroniza los deals (trades) CERRADOS en MT5 hacia la tabla `trades_ejecutados`
en Supabase, para poder construir el dashboard y el reporte diario de Telegram.

REQUISITOS:
- Variables de entorno ya existentes: SUPABASE_URL, SUPABASE_KEY (o SERVICE_ROLE_KEY),
  MT5_MAGIC (20260601), MT5_SYMBOL (GOLD)

FIX EN ESTA VERSION (vinculo strategy/signal_id + loop por defecto):
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
from datetime import datetime, timedelta
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


def buscar_signal_por_comentario(comentario):
    """
    A partir del comentario de la orden en MT5 (ej. "TradingPro_a1b2c3d4"),
    extrae el prefijo del signal_id y busca en la tabla `signals` cual
    señal completa empieza con ese prefijo. Devuelve (signal_id, strategy)
    o (None, None) si no se encuentra o el comentario no tiene el formato
    esperado (por ejemplo, ordenes abiertas manualmente sin pasar por el bot).
    """
    if not comentario or not comentario.startswith(COMMENT_PREFIX):
        return None, None

    prefijo = comentario[len(COMMENT_PREFIX):].strip()
    if len(prefijo) < 4:
        return None, None

    try:
        r = (
            supabase.table("signals")
            .select("id,strategy")
            .like("id", f"{prefijo}%")
            .limit(1)
            .execute()
        )
        if r.data:
            return r.data[0]["id"], r.data[0].get("strategy")
    except Exception as e:
        print(f"[sync_trades] Error buscando señal por comentario '{comentario}': {e}")

    return None, None


def sincronizar_trades_cerrados(dias_atras: int = 3):
    """
    Lee los deals cerrados de MT5 de los últimos `dias_atras` días,
    filtra por magic number, vincula cada uno a su señal original
    (strategy + signal_id) via el comentario de la orden, y hace
    upsert en Supabase (trades_ejecutados) usando el ticket como
    llave única para evitar duplicados.
    """
    desde = datetime.now() - timedelta(days=dias_atras)
    hasta = datetime.now()

    deals = mt5.history_deals_get(desde, hasta)
    if deals is None or len(deals) == 0:
        print("[sync_trades] No hay deals en el rango consultado.")
        return

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

        signal_id, strategy = buscar_signal_por_comentario(d.comment)
        if signal_id:
            vinculados += 1

        registros.append({
            "ticket": d.ticket,
            "magic": d.magic,
            "symbol": d.symbol or SYMBOL,
            "tipo": "BUY" if d.type == mt5.DEAL_TYPE_BUY else "SELL",
            "volumen": d.volume,
            "precio_cierre": d.price,
            "profit": d.profit,
            "swap": d.swap,
            "comision": d.commission,
            "profit_neto": profit_neto,
            "close_time": datetime.fromtimestamp(d.time).isoformat(),
            "comentario": d.comment,
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
