"""
backtest_engine.py
===================
Corre las 4 estrategias de signal_engine.py (Scalping M5 SMC, Killzone
Breakout, FVG Fill M5, EMA Pullback M5) contra las velas HISTORICAS ya
guardadas en Supabase (ohlc_candles), simula el resultado de cada señal
generada (gana en TP1 / pierde en SL), y guarda las estadísticas
agregadas en la tabla `backtests` — la misma que ya lee el dashboard
("Backtests — Historial de rendimiento").

DISEÑO: en vez de reescribir la lógica de cada estrategia por separado
(y arriesgar que el backtest se desincronice de lo que corre en vivo),
este script IMPORTA signal_engine.py directamente y solo reemplaza
("monkeypatch") las 3 funciones que dependen del reloj real
(is_killzone, get_session, get_session_start_utc) por versiones que usan
la hora de la VELA que se esta simulando en cada paso, en vez de
datetime.now(). También reemplaza get_candles() para que, cuando
alguna estrategia pida velas de los pares del dólar (para el filtro
DXY), reciba el historico correspondiente A ESE MOMENTO simulado, no
las velas mas recientes reales.

Todo lo demas (detect_swing_hl, calc_atr, detect_fvgs, OTE, Order
Blocks, filtro anti-trampa FVG, morfologia de vela, etc.) se usa TAL
CUAL esta en signal_engine.py — cero duplicacion de esa logica.

LIMITACIONES CONOCIDAS (quedan documentadas, no ocultas):
  - Solo se evalua contra TP1 (no TP2) para definir gana/pierde, igual
    que hace bot_engine.py en produccion (el SL/TP real que se manda a
    MT5 usa take_profit_1, TP2 es informativo).
  - Si una misma vela toca SL y TP1 al mismo tiempo (rango amplio), se
    asume PERDIDA por conservador — no hay forma de saber con velas M5
    cual se toco primero sin datos tick a tick.
  - No modela spread ni comision — el profit factor y winrate son "en
    limpio", un poco optimistas frente a la ejecucion real.
  - Si una señal no toca ni SL ni TP1 dentro de TRADE_TIMEOUT_VELAS
    (default 200 velas M5, ~16h), se descarta del conteo (no cuenta ni
    como ganada ni perdida) — evita que "operaciones eternas" distorsionen
    el winrate.
  - Solo corre para XAUUSD M5 (las 4 estrategias operan en M5) — no
    incluye la estrategia "TradingPro AI Elite" porque vive en un modulo
    aparte (strategy/tradingpro_ai.py) no incluido en este proyecto.
"""

import os
import sys
import bisect
from datetime import datetime, timezone, timedelta
import uuid
import requests
from dotenv import load_dotenv

load_dotenv()

import signal_engine as se

SUPABASE_URL = se.SUPABASE_URL
SUPABASE_KEY = se.SUPABASE_KEY

LOOKBACK_MIN_VELAS = 60       # minimo de velas M5 antes de empezar a evaluar (igual que exigen las estrategias)
TRADE_TIMEOUT_VELAS = 200     # ~16h en M5 — si no toca SL/TP1 en ese tiempo, se descarta del conteo


def _parse_time(t):
    if isinstance(t, datetime):
        return t
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


# ============================================================
# MONKEYPATCH: reemplazar las funciones que dependen del reloj real
# ============================================================

_BACKTEST_NOW = {"time": None}  # se actualiza en cada vela simulada


def _backtest_is_killzone():
    now = _BACKTEST_NOW["time"]
    rdh = ((now.hour - 4) + 24) % 24
    t = rdh * 100 + now.minute
    return (t >= 300 and t < 600) or (t >= 900 and t < 1200)


def _backtest_get_session():
    now = _BACKTEST_NOW["time"]
    rdh = ((now.hour - 4) + 24) % 24
    t = rdh * 100 + now.minute
    if t >= 300 and t < 600:
        return "London KZ"
    if t >= 900 and t < 1200:
        return "NY KZ"
    if t >= 100 and t < 300:
        return "Pre-London"
    return "Zona muerta"


def _backtest_get_session_start_utc():
    now = _BACKTEST_NOW["time"]
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


# Cache de velas históricas de los pares del dólar (para el filtro DXY),
# cargado UNA sola vez al inicio del backtest — se filtra por tiempo en
# cada llamada, sin volver a golpear Supabase por cada vela simulada.
# IMPORTANTE: se guarda en formato CRUDO de Supabase (candle_time/open/
# high/low/close/volume), NO convertido con to_candles() — porque
# get_pair_trend() (quien consume esto via get_candles) espera ese
# formato crudo tal cual lo devolvía el get_candles original.
#
# FIX 2026-08-20 (rendimiento — backtest se volvia impracticamente lento
# despues de ~13 dias simulados): antes, cada llamada volvia a recorrer
# TODA la lista (list comprehension) y re-parseaba cada candle_time de
# texto a datetime, para las 4 monedas DXY, en CADA vela M5 simulada
# (~8000 veces). Ahora se pre-calculan los timestamps UNA sola vez por
# par (al cargar) y se usa busqueda binaria (bisect) para encontrar el
# corte, en vez de escanear y re-parsear el historial completo cada vez.
_dxy_full_cache = {}      # instrument -> lista cruda (candle_time/open/high/low/close/volume)
_dxy_times_cache = {}     # instrument -> lista de datetime ya parseados, mismo orden/indices que _dxy_full_cache


def _backtest_get_candles(timeframe, limit=100, instrument="XAUUSD"):
    """
    Reemplaza signal_engine.get_candles(). Si el instrumento es uno de
    los pares del dólar (DXY sintetico), devuelve el historico YA
    cargado (formato crudo de Supabase), filtrado hasta el momento que
    se esta simulando — nunca "el mas reciente real". XAUUSD no debería
    llamarse aqui (las estrategias reciben c5/c30/ch1 ya armados como
    parametros), pero se deja un fallback vacio por seguridad.
    """
    if instrument in _dxy_full_cache:
        now = _BACKTEST_NOW["time"]
        full = _dxy_full_cache[instrument]
        times = _dxy_times_cache[instrument]
        idx = bisect.bisect_right(times, now)
        return full[max(0, idx - limit):idx]
    return []


se.is_killzone = _backtest_is_killzone
se.get_session = _backtest_get_session
se.get_session_start_utc = _backtest_get_session_start_utc
se.get_candles = _backtest_get_candles


# ============================================================
# CARGA DE VELAS HISTORICAS
# ============================================================

def fetch_all_candles_raw(instrument, timeframe, limit=8000):
    """Igual que fetch_all_candles, pero SIN convertir a formato interno —
    devuelve las filas tal cual las entrega Supabase (candle_time/open/
    high/low/close/volume). Se usa para el cache de pares del dólar."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/ohlc_candles",
        headers=se.headers(),
        params={
            "select":     "candle_time,open,high,low,close,volume",
            "instrument": f"eq.{instrument}",
            "timeframe":  f"eq.{timeframe}",
            "order":      "candle_time.asc",
            "limit":      str(limit),
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def fetch_all_candles(instrument, timeframe, limit=8000):
    return se.to_candles(fetch_all_candles_raw(instrument, timeframe, limit))


def preload_dxy_pairs():
    print("  Cargando histórico de pares del dólar (filtro DXY)...")
    for par in se.DOLLAR_PAIRS:
        candles = fetch_all_candles_raw(par, "M30")  # formato crudo, ver nota en _backtest_get_candles
        _dxy_full_cache[par] = candles
        _dxy_times_cache[par] = [_parse_time(c["candle_time"]) for c in candles]
        print(f"    {par}: {len(candles)} velas M30")


# ============================================================
# SIMULACION DE RESULTADO DE UNA SEÑAL (gana/pierde/timeout)
# ============================================================

def simulate_outcome(c5_all, entry_idx, sig):
    """
    Recorre las velas M5 posteriores a la señal buscando cual se toca
    primero: el SL o el TP1. Devuelve (resultado, idx_cierre, r_multiple):
      resultado: "WIN", "LOSS", o "TIMEOUT"
      idx_cierre: índice en c5_all donde se resolvió (o None si TIMEOUT)
      r_multiple: ganancia/pérdida en múltiplos de riesgo (+1.5 tipico en
                  TP1 para la mayoría de estrategias, -1.0 en SL)
    """
    entry = sig["entry_price"]
    sl = sig["stop_loss"]
    tp1 = sig["take_profit_1"]
    is_buy = sig["signal_type"] == "BUY"

    riesgo = abs(entry - sl)
    recompensa = abs(tp1 - entry)
    if riesgo <= 0:
        return "TIMEOUT", None, 0

    limite = min(len(c5_all), entry_idx + 1 + TRADE_TIMEOUT_VELAS)
    for j in range(entry_idx + 1, limite):
        vela = c5_all[j]
        if is_buy:
            toco_sl = vela["L"] <= sl
            toco_tp = vela["H"] >= tp1
        else:
            toco_sl = vela["H"] >= sl
            toco_tp = vela["L"] <= tp1

        if toco_sl and toco_tp:
            # ambas en la misma vela — se asume perdida (conservador)
            return "LOSS", j, -1.0
        if toco_sl:
            return "LOSS", j, -1.0
        if toco_tp:
            r = recompensa / riesgo
            return "WIN", j, r

    return "TIMEOUT", None, 0


# ============================================================
# CORRER UNA ESTRATEGIA CONTRA TODO EL HISTORICO
# ============================================================

def run_backtest_for_strategy(nombre, fn_estrategia, c5_all, c30_all, ch1_all, necesita_ch1=True):
    """
    Recorre c5_all vela por vela, llamando fn_estrategia(c5, c30, [ch1,]
    dxy_trend) con solo las velas visibles HASTA ese punto (como si
    'ahora' fuera esa vela). Si detecta señal y no hay una operación ya
    abierta de esta estrategia, la simula hasta su resolución (WIN/LOSS/
    TIMEOUT) y no vuelve a evaluar hasta que esa operación se resuelva
    — replica el "máximo 1 operación abierta a la vez" del bot real.
    """
    trades = []
    i = LOOKBACK_MIN_VELAS
    n = len(c5_all)

    # FIX 2026-08-20 (rendimiento): timestamps de c30/ch1 pre-calculados
    # UNA sola vez aqui, en vez de re-parsear cada "time" de texto a
    # datetime en cada vela M5 simulada. bisect reemplaza el escaneo
    # lineal completo de la lista por busqueda binaria.
    c30_times = [_parse_time(c["time"]) for c in c30_all]
    ch1_times = [_parse_time(c["time"]) for c in ch1_all] if necesita_ch1 else []

    while i < n:
        candle = c5_all[i]
        now = _parse_time(candle["time"])
        _BACKTEST_NOW["time"] = now

        c5 = c5_all[max(0, i - 99): i + 1]
        idx30 = bisect.bisect_right(c30_times, now)
        c30 = c30_all[max(0, idx30 - 60):idx30]

        try:
            if necesita_ch1:
                idx1 = bisect.bisect_right(ch1_times, now)
                ch1 = ch1_all[max(0, idx1 - 60):idx1]
                dxy_trend = se.get_dxy_trend()
                sig = fn_estrategia(c5, c30, ch1, dxy_trend)
            else:
                dxy_trend = se.get_dxy_trend()
                sig = fn_estrategia(c5, c30, dxy_trend)
        except Exception as e:
            print(f"    [{nombre}] Error en vela {i} ({now}): {e}")
            i += 1
            continue

        if sig is None:
            i += 1
            continue

        resultado, idx_cierre, r_multiple = simulate_outcome(c5_all, i, sig)

        if resultado != "TIMEOUT":
            trades.append({
                "entry_time": now,
                "resultado": resultado,
                "r_multiple": r_multiple,
                "hora_rd": ((now.hour - 4) + 24) % 24,
            })

        # Avanza al cierre de la operación (o 1 vela si fue TIMEOUT/riesgo 0)
        i = (idx_cierre + 1) if idx_cierre else (i + 1)

    return trades


# ============================================================
# ESTADISTICAS Y GUARDADO EN SUPABASE
# ============================================================

def calcular_estadisticas(trades):
    total = len(trades)
    if total == 0:
        return None

    ganadoras = [t for t in trades if t["resultado"] == "WIN"]
    perdedoras = [t for t in trades if t["resultado"] == "LOSS"]

    winrate = round(len(ganadoras) / total * 100, 1)

    suma_ganancias = sum(t["r_multiple"] for t in ganadoras)
    suma_perdidas = abs(sum(t["r_multiple"] for t in perdedoras))
    profit_factor = round(suma_ganancias / suma_perdidas, 2) if suma_perdidas > 0 else (suma_ganancias if suma_ganancias > 0 else 0)

    # Drawdown maximo en multiplos de R, sobre la curva acumulada en orden cronologico
    trades_ordenados = sorted(trades, key=lambda t: t["entry_time"])
    curva = []
    acumulado = 0
    for t in trades_ordenados:
        acumulado += t["r_multiple"]
        curva.append(acumulado)

    max_dd = 0
    pico = curva[0] if curva else 0
    for valor in curva:
        pico = max(pico, valor)
        dd = pico - valor
        max_dd = max(max_dd, dd)

    # Mejor hora (RD): la hora con mejor winrate, exigiendo minimo 3 trades en esa hora
    por_hora = {}
    for t in trades:
        por_hora.setdefault(t["hora_rd"], []).append(t["resultado"] == "WIN")

    mejor_hora = None
    mejor_wr = -1
    for hora, resultados in por_hora.items():
        if len(resultados) < 3:
            continue
        wr = sum(resultados) / len(resultados)
        if wr > mejor_wr:
            mejor_wr = wr
            mejor_hora = hora

    mejor_hora_str = f"{mejor_hora:02d}:00 RD" if mejor_hora is not None else "N/D (poca muestra)"

    return {
        "total_trades": total,
        "winning_trades": len(ganadoras),
        "losing_trades": len(perdedoras),
        "winrate": winrate,
        "profit_factor": profit_factor,
        "drawdown": round(max_dd, 2),
        "best_hour": mejor_hora_str,
    }


def guardar_backtest(strategy_name, timeframe, stats):
    payload = {
        "id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "strategy": strategy_name,
        "timeframe": timeframe,
        **stats,
    }
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/backtests",
        headers=se.headers(),
        json=payload,
        timeout=15,
    )
    if r.status_code >= 400:
        print(f"  [ERROR] Guardando backtest de {strategy_name}: {r.status_code} {r.text}")
        return False
    return True


# ============================================================
# MAIN
# ============================================================

def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: Faltan SUPABASE_URL o SUPABASE_KEY en .env")
        sys.exit(1)

    print(f"\n{'='*55}")
    print("  BACKTEST ENGINE — Trading Pro XAUUSD")
    print(f"{'='*55}\n")

    preload_dxy_pairs()

    print("\n  Cargando histórico XAUUSD (M5, M30, H1)...")
    c5_all = fetch_all_candles("XAUUSD", "M5")
    c30_all = fetch_all_candles("XAUUSD", "M30")
    ch1_all = fetch_all_candles("XAUUSD", "H1")
    print(f"    M5: {len(c5_all)} velas | M30: {len(c30_all)} velas | H1: {len(ch1_all)} velas\n")

    if len(c5_all) < LOOKBACK_MIN_VELAS + 50:
        print("  No hay suficientes velas M5 para un backtest confiable todavía.")
        sys.exit(1)

    estrategias = [
        ("Scalping M5 SMC", se.strategy_scalping_m5, True),
        ("FVG Fill M5", se.strategy_fvg_fill, False),
        ("EMA Pullback M5", se.strategy_ema_pullback, True),
    ]

    resultados_resumen = []

    for nombre, fn, necesita_ch1 in estrategias:
        print(f"  Corriendo backtest: {nombre}...")
        trades = run_backtest_for_strategy(nombre, fn, c5_all, c30_all, ch1_all, necesita_ch1)
        stats = calcular_estadisticas(trades)

        if stats is None:
            print(f"    {nombre}: 0 señales generadas en todo el histórico — nada que guardar.\n")
            continue

        print(
            f"    {nombre}: {stats['total_trades']} trades | "
            f"Winrate {stats['winrate']}% | PF {stats['profit_factor']} | "
            f"DD {stats['drawdown']}R | Mejor hora: {stats['best_hour']}\n"
        )
        guardar_backtest(nombre, "M5", stats)
        resultados_resumen.append((nombre, stats))

    # ── Estrategia 2 (Killzone Breakout) — firma distinta (c5, ch1, dxy) ──
    print("  Corriendo backtest: Killzone Breakout...")
    trades_kz = []
    i = LOOKBACK_MIN_VELAS
    n = len(c5_all)
    ch1_times_kz = [_parse_time(c["time"]) for c in ch1_all]
    while i < n:
        candle = c5_all[i]
        now = _parse_time(candle["time"])
        _BACKTEST_NOW["time"] = now
        c5 = c5_all[max(0, i - 99): i + 1]
        idx1_kz = bisect.bisect_right(ch1_times_kz, now)
        ch1 = ch1_all[max(0, idx1_kz - 60):idx1_kz]
        try:
            dxy_trend = se.get_dxy_trend()
            sig = se.strategy_killzone_breakout(c5, ch1, dxy_trend)
        except Exception as e:
            print(f"    [Killzone Breakout] Error en vela {i} ({now}): {e}")
            i += 1
            continue

        if sig is None:
            i += 1
            continue

        resultado, idx_cierre, r_multiple = simulate_outcome(c5_all, i, sig)
        if resultado != "TIMEOUT":
            trades_kz.append({
                "entry_time": now,
                "resultado": resultado,
                "r_multiple": r_multiple,
                "hora_rd": ((now.hour - 4) + 24) % 24,
            })
        i = (idx_cierre + 1) if idx_cierre else (i + 1)

    stats_kz = calcular_estadisticas(trades_kz)
    if stats_kz is None:
        print("    Killzone Breakout: 0 señales generadas en todo el histórico — nada que guardar.\n")
    else:
        print(
            f"    Killzone Breakout: {stats_kz['total_trades']} trades | "
            f"Winrate {stats_kz['winrate']}% | PF {stats_kz['profit_factor']} | "
            f"DD {stats_kz['drawdown']}R | Mejor hora: {stats_kz['best_hour']}\n"
        )
        guardar_backtest("Killzone Breakout", "M5", stats_kz)
        resultados_resumen.append(("Killzone Breakout", stats_kz))

    print(f"{'='*55}")
    print(f"  Backtest completo. {len(resultados_resumen)} estrategia(s) guardada(s) en Supabase.")
    print("  Revisa el dashboard — pestaña Backtests.")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
