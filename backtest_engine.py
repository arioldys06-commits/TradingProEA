"""
backtest_engine.py
===================
RECONSTRUIDO 2026-08-31 — el archivo que existia bajo este nombre resulto
ser una copia de signal_engine.py (sin ninguna logica de backtest real:
sin lectura de rango historico, sin simulacion de trades, sin calculo de
winrate/profit factor/drawdown, sin insert a la tabla `backtests`). Por
eso el dashboard llevaba 11 dias sin corridas nuevas — el script que
deberia generarlas nunca existio de verdad con ese contenido.

Que hace este script:
  1. Descarga velas historicas de Supabase (ohlc_candles) para un rango
     de dias hacia atras, paginando (PostgREST limita ~1000 filas por
     request).
  2. Camina bar-a-bar (walk-forward, SIN look-ahead) sobre cada
     estrategia, reutilizando las funciones REALES de signal_engine.py
     via import — no se reimplementa la logica de las estrategias aqui,
     para garantizar que el backtest prueba EXACTAMENTE el mismo codigo
     que corre en vivo. Cualquier cambio futuro en signal_engine.py se
     refleja automaticamente en el proximo backtest sin tocar este
     archivo.
  3. Cuando una estrategia genera una señal, simula el resultado hacia
     adelante usando las velas siguientes del MISMO timeframe de la
     señal (limitacion conocida: para EMA Pullback/FVG Fill/Scalping
     (M5) y Sweep Displacement (M1) la simulacion es igual de precisa
     que el bot real; para Mean Reversion BB (M15) el resultado es una
     aproximacion mas gruesa, porque el bot real gestiona breakeven/
     trailing en M5 aunque la señal nazca en M15 — este backtest no
     replica esa gestion intra-vela, solo el SL/TP1/TP2 originales).
  4. Convencion CONSERVADORA de simulacion: si una misma vela toca SL y
     TP en el mismo rango (H-L), se asume que el SL se toco primero
     (peor caso) — evita inflar el winrate por optimismo del backtest.
  5. Agrega resultados por estrategia (trades, wins, losses, winrate,
     profit factor, "drawdown" = racha maxima de perdidas consecutivas,
     mejor hora RD por ganancia acumulada) e inserta una fila nueva por
     estrategia en la tabla `backtests` de Supabase, mismo formato que
     ya usa el dashboard (trading-pro-ea.vercel.app).

Uso:
    python backtest_engine.py                  # ultimos 30 dias, las 6 estrategias
    python backtest_engine.py --days 45         # ultimos 45 dias
    python backtest_engine.py --strategies ema,fvg   # solo esas dos

IMPORTANTE: este script debe correr en la MISMA carpeta que
signal_engine.py (lo importa como modulo). No requiere MT5 ni Telegram
— solo SUPABASE_URL y SUPABASE_KEY del .env.
"""

import os
import sys
import argparse
import statistics
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import requests
from dotenv import load_dotenv

load_dotenv()

# Reutiliza TODA la logica real de las 6 estrategias — no se reimplementa
# nada aqui. Si signal_engine.py cambia, el backtest cambia con el.
import signal_engine as se

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: Faltan SUPABASE_URL o SUPABASE_KEY en .env")
    sys.exit(1)

PAGE_SIZE = 1000  # limite practico por request de PostgREST/Supabase

# ── Fetch historico paginado ──────────────────────────────────────

def fetch_historical_candles(timeframe, start_iso, end_iso, instrument="XAUUSD"):
    """Descarga TODAS las velas de un timeframe entre start_iso y end_iso,
    paginando en bloques de PAGE_SIZE, ordenadas ascendente por tiempo."""
    all_rows = []
    offset = 0
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/ohlc_candles",
            headers=se.headers(),
            params={
                "select":     "candle_time,open,high,low,close,volume",
                "instrument": f"eq.{instrument}",
                "timeframe":  f"eq.{timeframe}",
                "candle_time": f"gte.{start_iso}",
                "and":        f"(candle_time.lte.{end_iso})",
                "order":      "candle_time.asc",
                "limit":      str(PAGE_SIZE),
                "offset":     str(offset),
            },
            timeout=30,
        )
        if r.status_code >= 400:
            print(f"  [ERROR] fetch {timeframe}: {r.status_code} {r.text[:200]}")
            break
        chunk = r.json()
        if not chunk:
            break
        all_rows.extend(chunk)
        if len(chunk) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return se.to_candles(all_rows)


def slice_up_to(candles, idx, lookback=250):
    """Ventana de velas 'conocidas hasta ahora' en el indice idx — nunca
    incluye datos futuros (evita look-ahead bias)."""
    start = max(0, idx - lookback + 1)
    return candles[start:idx + 1]


def candles_up_to_time(candles, t_iso, lookback=250):
    """Para timeframes secundarios (M30/H1/M15): toma solo las velas cuyo
    candle_time es <= t_iso (la vela actual del timeframe principal)."""
    out = [c for c in candles if c["time"] <= t_iso]
    return out[-lookback:] if len(out) > lookback else out


# ── Simulacion de resultado de una señal ──────────────────────────

def simular_resultado(signal, forward_candles, max_bars=200):
    """Camina hacia adelante en forward_candles buscando el primer toque
    de SL, TP1 o TP2. Convencion conservadora: si la misma vela toca SL
    y TP, se asume SL primero. Devuelve (resultado, r_multiple) donde
    resultado in {"WIN_TP1","WIN_TP2","LOSS","TIMEOUT"}."""
    entry = signal["entry_price"]
    sl    = signal["stop_loss"]
    tp1   = signal["take_profit_1"]
    tp2   = signal["take_profit_2"]
    is_buy = signal["signal_type"] == "BUY"

    riesgo = abs(entry - sl)
    if riesgo <= 0:
        return "TIMEOUT", 0.0

    for c in forward_candles[:max_bars]:
        if is_buy:
            toco_sl  = c["L"] <= sl
            toco_tp1 = c["H"] >= tp1
            toco_tp2 = c["H"] >= tp2
        else:
            toco_sl  = c["H"] >= sl
            toco_tp1 = c["L"] <= tp1
            toco_tp2 = c["L"] <= tp2

        if toco_sl:
            return "LOSS", -1.0
        if toco_tp2:
            recompensa = abs(tp2 - entry) / riesgo
            return "WIN_TP2", recompensa
        if toco_tp1:
            recompensa = abs(tp1 - entry) / riesgo
            return "WIN_TP1", recompensa

    return "TIMEOUT", 0.0


# ── Walk-forward por estrategia ───────────────────────────────────

STRATEGY_TIMEFRAME = {
    "scalping":     "M5",
    "killzone":     "M5",
    "fvg":          "M5",
    "ema":          "M5",
    "sweep":        "M1",
    "mean_reversion": "M15",
}

STRATEGY_LABEL = {
    "scalping":       "Scalping M5 SMC",
    "killzone":       "Killzone Breakout",
    "fvg":            "FVG Fill M5",
    "ema":            "EMA Pullback M5",
    "sweep":          "Sweep Displacement M1",
    "mean_reversion": "Mean Reversion BB M15",
}

STEP_BARS = {
    # Evitar recalcular cada vela ahorra tiempo sin perder señal real,
    # porque las estrategias tienen cooldown/no-solapamiento de todas
    # formas (solo 1 operacion a la vez, igual que bot_engine.py).
    "M1": 1,
    "M5": 1,
    "M15": 1,
}


def run_backtest_scalping(m5, m30, h1, dxy_trend="NEUTRAL"):
    trades = []
    i = 100
    while i < len(m5) - 1:
        c5_win = slice_up_to(m5, i, lookback=100)
        t_iso  = c5_win[-1]["time"]
        c30_win = candles_up_to_time(m30, t_iso, lookback=60)
        ch1_win = candles_up_to_time(h1, t_iso, lookback=60)
        try:
            sig = se.strategy_scalping_m5(c5_win, c30_win, ch1_win, dxy_trend)
        except Exception:
            sig = None
        if sig:
            resultado, r = simular_resultado(sig, m5[i + 1:])
            trades.append((t_iso, resultado, r))
            i += 10  # evita re-señalizar sobre la misma vela de entrada
        else:
            i += 1
    return trades


def run_backtest_killzone(m5, h1, dxy_trend="NEUTRAL"):
    trades = []
    i = 30
    while i < len(m5) - 1:
        c5_win = slice_up_to(m5, i, lookback=40)
        t_iso  = c5_win[-1]["time"]
        ch1_win = candles_up_to_time(h1, t_iso, lookback=10)
        try:
            sig = se.strategy_killzone_breakout(c5_win, ch1_win, dxy_trend)
        except Exception:
            sig = None
        if sig:
            resultado, r = simular_resultado(sig, m5[i + 1:])
            trades.append((t_iso, resultado, r))
            i += 10
        else:
            i += 1
    return trades


def run_backtest_fvg(m5, m30, m15, dxy_trend="NEUTRAL"):
    trades = []
    i = 40
    while i < len(m5) - 1:
        c5_win = slice_up_to(m5, i, lookback=50)
        t_iso  = c5_win[-1]["time"]
        c30_win = candles_up_to_time(m30, t_iso, lookback=60)
        c15_win = candles_up_to_time(m15, t_iso, lookback=60)
        try:
            sig = se.strategy_fvg_fill(c5_win, c30_win, c15_win, dxy_trend)
        except Exception:
            sig = None
        if sig:
            resultado, r = simular_resultado(sig, m5[i + 1:])
            trades.append((t_iso, resultado, r))
            i += 10
        else:
            i += 1
    return trades


def run_backtest_ema(m5, m30, h1, dxy_trend="NEUTRAL"):
    trades = []
    i = 55
    while i < len(m5) - 1:
        c5_win = slice_up_to(m5, i, lookback=60)
        t_iso  = c5_win[-1]["time"]
        c30_win = candles_up_to_time(m30, t_iso, lookback=60)
        ch1_win = candles_up_to_time(h1, t_iso, lookback=60)
        try:
            sig = se.strategy_ema_pullback(c5_win, c30_win, ch1_win, dxy_trend)
        except Exception:
            sig = None
        if sig:
            resultado, r = simular_resultado(sig, m5[i + 1:])
            trades.append((t_iso, resultado, r))
            i += 8
        else:
            i += 1
    return trades


def run_backtest_sweep(m1, m15, m5, dxy_trend="NEUTRAL"):
    trades = []
    i = 60
    while i < len(m1) - 1:
        c1_win = slice_up_to(m1, i, lookback=60)
        t_iso  = c1_win[-1]["time"]
        c15_win = candles_up_to_time(m15, t_iso, lookback=250)
        c5_win  = candles_up_to_time(m5, t_iso, lookback=40)
        # strategy_sweep_displacement hace su PROPIO fetch de M1 via
        # se.get_candles("M1", 60) — en backtest eso rompe el walk-forward
        # (traeria M1 EN VIVO, no historico). Se parchea temporalmente
        # get_candles para devolver la ventana historica correcta.
        original_get_candles = se.get_candles
        se.get_candles = lambda tf, limit=60, instrument="XAUUSD": (
            [{"candle_time": c["time"], "open": c["O"], "high": c["H"],
              "low": c["L"], "close": c["C"], "volume": c["V"]} for c in c1_win[-limit:]]
            if tf == "M1" else original_get_candles(tf, limit, instrument)
        )
        try:
            sig = se.strategy_sweep_displacement(c15_win, c5_win, dxy_trend)
        except Exception:
            sig = None
        finally:
            se.get_candles = original_get_candles
        if sig:
            resultado, r = simular_resultado(sig, m1[i + 1:])
            trades.append((t_iso, resultado, r))
            i += 15
        else:
            i += 1
    return trades


def run_backtest_mean_reversion(m15, dxy_trend="NEUTRAL"):
    trades = []
    i = 45
    while i < len(m15) - 1:
        c15_win = slice_up_to(m15, i, lookback=60)
        try:
            sig = se.strategy_mean_reversion_bb(c15_win, dxy_trend)
        except Exception:
            sig = None
        if sig:
            resultado, r = simular_resultado(sig, m15[i + 1:])
            trades.append((c15_win[-1]["time"], resultado, r))
            i += 6
        else:
            i += 1
    return trades


# ── Agregacion de resultados ──────────────────────────────────────

def agregar_resultado(trades):
    total = len(trades)
    if total == 0:
        return None

    wins = [t for t in trades if t[1] in ("WIN_TP1", "WIN_TP2")]
    losses = [t for t in trades if t[1] == "LOSS"]
    timeouts = [t for t in trades if t[1] == "TIMEOUT"]

    n_wins = len(wins)
    n_losses = len(losses)
    n_contados = n_wins + n_losses  # timeouts no cuentan para winrate

    if n_contados == 0:
        return None

    winrate = round(100 * n_wins / n_contados, 1)

    suma_r_ganada = sum(r for _, _, r in wins)
    suma_r_perdida = abs(sum(r for _, _, r in losses))  # cada loss = -1R
    profit_factor = round(suma_r_ganada / suma_r_perdida, 2) if suma_r_perdida > 0 else None

    # "Drawdown" = racha maxima de perdidas consecutivas (mismo estilo
    # de numero pequeño que ya usa la tabla backtests: 6, 4, 4.5...).
    racha_actual = 0
    racha_maxima = 0
    for _, resultado, _ in trades:
        if resultado == "LOSS":
            racha_actual += 1
            racha_maxima = max(racha_maxima, racha_actual)
        elif resultado in ("WIN_TP1", "WIN_TP2"):
            racha_actual = 0

    # Mejor hora RD por ganancia acumulada — solo si hay >=3 trades en
    # esa hora (evita reportar "mejor hora" con 1 solo trade de muestra).
    horas = defaultdict(float)
    conteo_horas = defaultdict(int)
    for t_iso, resultado, r in trades:
        if resultado not in ("WIN_TP1", "WIN_TP2", "LOSS"):
            continue
        try:
            t_utc = datetime.fromisoformat(t_iso.replace("Z", "+00:00"))
            hora_rd = (t_utc.hour - 4) % 24
        except Exception:
            continue
        horas[hora_rd] += r
        conteo_horas[hora_rd] += 1

    horas_validas = {h: v for h, v in horas.items() if conteo_horas[h] >= 3}
    if horas_validas:
        mejor_hora_num = max(horas_validas, key=horas_validas.get)
        best_hour = f"{mejor_hora_num:02d}:00 RD"
    else:
        best_hour = "N/D (poca muestra)"

    return {
        "total_trades":   n_contados,
        "winning_trades": n_wins,
        "losing_trades":  n_losses,
        "winrate":        winrate,
        "profit_factor":  profit_factor,
        "drawdown":       racha_maxima,
        "best_hour":      best_hour,
    }


def ya_existe_resultado_identico(strategy_label, timeframe, resultado):
    """Evita duplicados exactos: si ya hay una fila de HOY con la misma
    estrategia/timeframe y el mismo resultado numerico (total_trades,
    winrate, profit_factor), es un re-run sin cambios en el codigo/rango
    y no vale la pena guardarla de nuevo. Si algo cambio (nuevo parametro,
    mas dias de historial), SI se guarda como fila nueva para conservar
    la evolucion en el tiempo (la columna FECHA del dashboard esta pensada
    para eso)."""
    hoy_inicio = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/backtests",
        headers=se.headers(),
        params={
            "select":       "id",
            "strategy":     f"eq.{strategy_label}",
            "timeframe":    f"eq.{timeframe}",
            "total_trades": f"eq.{resultado['total_trades']}",
            "winrate":      f"eq.{resultado['winrate']}",
            "profit_factor": f"eq.{resultado['profit_factor']}" if resultado["profit_factor"] is not None else "is.null",
            "created_at":   f"gte.{hoy_inicio}",
            "limit":        "1",
        },
        timeout=15,
    )
    if r.status_code >= 400:
        # Si el chequeo falla, no bloqueamos el backtest — mejor un
        # duplicado ocasional que perder el resultado por un error de red.
        print(f"  [WARN] no se pudo chequear duplicados: {r.status_code} {r.text[:150]}")
        return False
    return len(r.json()) > 0


def insertar_backtest(strategy_label, timeframe, resultado):
    if ya_existe_resultado_identico(strategy_label, timeframe, resultado):
        print(f"    {strategy_label}: SKIP (resultado identico ya guardado hoy)")
        return True

    payload = {
        "strategy":       strategy_label,
        "timeframe":      timeframe,
        "total_trades":   resultado["total_trades"],
        "winning_trades": resultado["winning_trades"],
        "losing_trades":  resultado["losing_trades"],
        "winrate":        resultado["winrate"],
        "profit_factor":  resultado["profit_factor"],
        "drawdown":       resultado["drawdown"],
        "best_hour":      resultado["best_hour"],
        "created_at":     datetime.now(timezone.utc).isoformat(),
    }
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/backtests",
        headers=se.headers(),
        json=payload,
        timeout=15,
    )
    if r.status_code >= 400:
        print(f"  [ERROR] insert backtests: {r.status_code} {r.text[:200]}")
        return False
    return True


# ── Main ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Backtest de las estrategias de signal_engine.py")
    parser.add_argument("--days", type=int, default=30, help="Dias hacia atras a testear (default 30)")
    parser.add_argument(
        "--strategies", type=str, default="scalping,killzone,fvg,ema,sweep,mean_reversion",
        help="Lista separada por coma: scalping,killzone,fvg,ema,sweep,mean_reversion"
    )
    args = parser.parse_args()

    seleccion = [s.strip() for s in args.strategies.split(",") if s.strip()]

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    start_iso = start.isoformat()
    end_iso = end.isoformat()

    print(f"\n{'='*55}")
    print(f"  BACKTEST ENGINE — Trading Pro XAUUSD")
    print(f"  Rango: {start.strftime('%Y-%m-%d')} a {end.strftime('%Y-%m-%d')} ({args.days} dias)")
    print(f"  Estrategias: {', '.join(seleccion)}")
    print(f"{'='*55}\n")

    print("  Descargando velas historicas...")
    m5  = fetch_historical_candles("M5",  start_iso, end_iso)
    m15 = fetch_historical_candles("M15", start_iso, end_iso)
    m30 = fetch_historical_candles("M30", start_iso, end_iso)
    h1  = fetch_historical_candles("H1",  start_iso, end_iso)
    m1  = fetch_historical_candles("M1",  start_iso, end_iso) if "sweep" in seleccion else []
    print(f"  M5: {len(m5)} velas | M15: {len(m15)} | M30: {len(m30)} | H1: {len(h1)} | M1: {len(m1)}")

    if len(m5) < 200:
        print("  [ERROR] Muy pocas velas M5 para un backtest confiable. Revisa el rango de fechas o data_engine.py.")
        sys.exit(1)

    dxy_trend = "NEUTRAL"  # simplificacion: no se recalculan pares USD historicos por rendimiento

    resultados_finales = []

    if "scalping" in seleccion:
        print("\n  [1] Corriendo Scalping M5 SMC...")
        trades = run_backtest_scalping(m5, m30, h1, dxy_trend)
        res = agregar_resultado(trades)
        if res:
            resultados_finales.append(("scalping", res))
            print(f"      {res['total_trades']} trades | winrate {res['winrate']}% | PF {res['profit_factor']}")
        else:
            print("      Sin trades suficientes en el rango.")

    if "killzone" in seleccion:
        print("\n  [2] Corriendo Killzone Breakout...")
        trades = run_backtest_killzone(m5, h1, dxy_trend)
        res = agregar_resultado(trades)
        if res:
            resultados_finales.append(("killzone", res))
            print(f"      {res['total_trades']} trades | winrate {res['winrate']}% | PF {res['profit_factor']}")
        else:
            print("      Sin trades suficientes en el rango.")

    if "fvg" in seleccion:
        print("\n  [3] Corriendo FVG Fill M5...")
        trades = run_backtest_fvg(m5, m30, m15, dxy_trend)
        res = agregar_resultado(trades)
        if res:
            resultados_finales.append(("fvg", res))
            print(f"      {res['total_trades']} trades | winrate {res['winrate']}% | PF {res['profit_factor']}")
        else:
            print("      Sin trades suficientes en el rango.")

    if "ema" in seleccion:
        print("\n  [4] Corriendo EMA Pullback M5...")
        trades = run_backtest_ema(m5, m30, h1, dxy_trend)
        res = agregar_resultado(trades)
        if res:
            resultados_finales.append(("ema", res))
            print(f"      {res['total_trades']} trades | winrate {res['winrate']}% | PF {res['profit_factor']}")
        else:
            print("      Sin trades suficientes en el rango.")

    if "sweep" in seleccion:
        print("\n  [5] Corriendo Sweep Displacement M1...")
        trades = run_backtest_sweep(m1, m15, m5, dxy_trend)
        res = agregar_resultado(trades)
        if res:
            resultados_finales.append(("sweep", res))
            print(f"      {res['total_trades']} trades | winrate {res['winrate']}% | PF {res['profit_factor']}")
        else:
            print("      Sin trades suficientes en el rango.")

    if "mean_reversion" in seleccion:
        print("\n  [6] Corriendo Mean Reversion BB M15...")
        trades = run_backtest_mean_reversion(m15, dxy_trend)
        res = agregar_resultado(trades)
        if res:
            resultados_finales.append(("mean_reversion", res))
            print(f"      {res['total_trades']} trades | winrate {res['winrate']}% | PF {res['profit_factor']}")
        else:
            print("      Sin trades suficientes en el rango.")

    print(f"\n  Guardando {len(resultados_finales)} resultado(s) en Supabase...")
    for key, res in resultados_finales:
        ok = insertar_backtest(STRATEGY_LABEL[key], STRATEGY_TIMEFRAME[key], res)
        print(f"    {STRATEGY_LABEL[key]}: {'OK' if ok else 'ERROR'}")

    print(f"\n{'='*55}")
    print("  Backtest finalizado.")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
