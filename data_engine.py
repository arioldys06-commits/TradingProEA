"""
data_engine.py
==============
Descarga velas XAUUSD desde MT5 y las sube a Supabase.
Proyecto: qilvrvnwdtpbkcfwktqs (proyecto activo del dashboard)
"""

import MetaTrader5 as mt5
import time
from datetime import datetime, timezone
from supabase import create_client

# ── CONFIGURACIÓN ─────────────────────────────────────────────
SUPA_URL = "https://qilvrvnwdtpbkcfwktqs.supabase.co"
SUPA_KEY = "sb_secret_U_iPqRAky22ndAlDLUg0rg_X-_DHuEQ"

TIMEFRAMES = {
    "M1":  (mt5.TIMEFRAME_M1,  150),
    "M5":  (mt5.TIMEFRAME_M5,  150),
    "M15": (mt5.TIMEFRAME_M15, 150),
    "M30": (mt5.TIMEFRAME_M30, 150),
    "H1":  (mt5.TIMEFRAME_H1,  200),
    "H2":  (mt5.TIMEFRAME_H2,  150),
    "H4":  (mt5.TIMEFRAME_H4,  150),
}

SYMBOL   = "GOLD"    # Nombre del símbolo en MT5 (XMGlobal)
INTERVAL = 60        # Segundos entre cada ciclo
# ──────────────────────────────────────────────────────────────

def get_supabase():
    return create_client(SUPA_URL, SUPA_KEY)

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
            dt = datetime.fromtimestamp(r["time"], tz=timezone.utc)
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
