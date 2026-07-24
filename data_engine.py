"""
data_engine.py
==============
Descarga velas XAUUSD desde MT5 y las sube a Supabase.
Proyecto: qilvrvnwdtpbkcfwktqs (proyecto activo del dashboard)
"""

import os
import time
import MetaTrader5 as mt5
from datetime import datetime, timezone, timedelta
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

# ── CONFIGURACIÓN ─────────────────────────────────────────────
# Las credenciales viven SOLO en el archivo .env local, nunca aqui.
SUPA_URL = os.getenv("SUPABASE_URL")
SUPA_KEY = os.getenv("SUPABASE_KEY")

TIMEFRAMES = {
    "M1":  (mt5.TIMEFRAME_M1,  150),
    "M5":  (mt5.TIMEFRAME_M5,  150),
    "M15": (mt5.TIMEFRAME_M15, 150),
    "M30": (mt5.TIMEFRAME_M30, 150),
    "H1":  (mt5.TIMEFRAME_H1,  200),
    "H2":  (mt5.TIMEFRAME_H2,  150),
    "H4":  (mt5.TIMEFRAME_H4,  150),
}

SYMBOL   = os.getenv("MT5_SYMBOL", "GOLD")  # Nombre del símbolo en MT5 (XMGlobal)
INTERVAL = 60        # Segundos entre cada ciclo
# ──────────────────────────────────────────────────────────────

def get_supabase():
    if not SUPA_URL or not SUPA_KEY:
        raise RuntimeError(
            "Faltan SUPABASE_URL o SUPABASE_KEY. "
            "Revisa que el archivo .env exista en esta carpeta y tenga esas variables."
        )
    return create_client(SUPA_URL, SUPA_KEY)

def eu_dst_active(dt_utc):
    """
    Determina si, en la fecha dada, el horario de verano europeo (EEST,
    UTC+3) esta activo en vez del horario de invierno (EET, UTC+2).
    Regla de la UE: DST va desde el ultimo domingo de marzo (01:00 UTC)
    hasta el ultimo domingo de octubre (01:00 UTC).
    Necesario porque XMGlobal sigue el horario europeo, y usar un offset
    fijo de 2h todo el año desfasa las velas ~1h durante el verano.
    """
    year = dt_utc.year

    def last_sunday(month):
        # Empieza en el ultimo dia del mes y retrocede hasta domingo (weekday()==6)
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


def init_mt5():
    if not mt5.initialize():
        print(f"[ERROR] MT5 init falló: {mt5.last_error()}")
        return False
    print(f"[OK] MT5 conectado — build {mt5.version()}")
    return True

def fetch_and_upload(sb):
    for tf_name, (tf_const, count) in TIMEFRAMES.items():
        rates = mt5.copy_rates_from_pos(SYMBOL, tf_const, 0, count)
        if rates is None or len(rates) == 0:
            print(f"  [{tf_name}] Sin datos")
            continue

        rows = []
        for r in rates:
            # MT5 XMGlobal usa horario europeo (EET invierno / EEST verano)
            # pero r["time"] llega como si fuera UTC sin ajustar. El offset
            # correcto cambia segun la epoca del año por el horario de verano
            # europeo — usar 2h fijo todo el año desfasaba las velas ~1h
            # durante el verano (julio = EEST = UTC+3, no EET = UTC+2).
            dt_raw = datetime.fromtimestamp(r["time"], tz=timezone.utc)
            offset = eet_offset_hours(dt_raw)
            dt     = dt_raw - timedelta(hours=offset)  # EET/EEST → UTC real
            rows.append({
                "instrument":  "XAUUSD",
                "timeframe":   tf_name,
                "candle_time": dt.isoformat(),
                "open":        float(r["open"]),
                "high":        float(r["high"]),
                "low":         float(r["low"]),
                "close":       float(r["close"]),
                "volume":      int(r["tick_volume"]),
            })

        try:
            sb.table("ohlc_candles").upsert(
                rows,
                on_conflict="instrument,timeframe,candle_time"
            ).execute()
            print(f"  [{tf_name}] {len(rows)} velas subidas")
        except Exception as e:
            print(f"  [{tf_name}] Error Supabase: {e}")

def main():
    if not SUPA_URL or not SUPA_KEY:
        print("ERROR: Faltan SUPABASE_URL o SUPABASE_KEY en .env")
        return

    if not init_mt5():
        return

    sb = get_supabase()
    print(f"[START] data_engine corriendo — interval {INTERVAL}s")
    print(f"[URL]   {SUPA_URL}")
    print(f"[TFs]   {list(TIMEFRAMES.keys())}")

    while True:
        now = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{now}] Descargando velas...")
        try:
            fetch_and_upload(sb)
        except Exception as e:
            print(f"[ERROR] {e}")
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
