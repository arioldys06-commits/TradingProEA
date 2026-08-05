"""
result_tracker.py — V3
======================
Revisa señales EXECUTING (ya abiertas de verdad en MT5) y actualiza WIN/LOSS comparando
con las velas reales de Supabase.

Proyecto: qilvrvnwdtpbkcfwktqs (activo en dashboard)
"""

import os
import sys
import time
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL     = os.getenv("SUPABASE_URL")
SUPABASE_KEY     = os.getenv("SUPABASE_KEY")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@XAUUSD_Signals_DR")

BREAKEVEN_FILE = "breakeven_signals.txt"
LOOP_INTERVAL  = int(os.getenv("TRACKER_LOOP_INTERVAL", "60"))  # segundos entre cada revision

ANTI_HUNT_SL_EXTRA = float(os.getenv("ANTI_HUNT_SL_EXTRA", "2.0"))

# ── Desfase Republica Dominicana (UTC-4, sin horario de verano) ──
DR_OFFSET = timedelta(hours=-4)

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
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
        return r.status_code == 200
    except:
        return False

ERROR_LOG_FILE = "tracker_errors.log"

def log_error_to_file(message):
    try:
        with open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception:
        pass

def load_breakeven_signals():
    if os.path.exists(BREAKEVEN_FILE):
        try:
            with open(BREAKEVEN_FILE, "r") as f:
                return set(line.strip() for line in f if line.strip())
        except Exception as e:
            print(f"  [BREAKEVEN] No se pudo leer {BREAKEVEN_FILE}: {e}")
            return set()
    return set()

def save_breakeven_signals(sig_set, intentos=3):
    ultimo_error = None
    tmp_file = BREAKEVEN_FILE + ".tmp"

    for intento in range(1, intentos + 1):
        try:
            with open(tmp_file, "w") as f:
                for s in list(sig_set)[-500:]:
                    f.write(s + "\n")
            os.replace(tmp_file, BREAKEVEN_FILE)
            return True
        except Exception as e:
            ultimo_error = str(e)
            print(f"  [BREAKEVEN] Intento {intento}/{intentos} fallo al escribir {BREAKEVEN_FILE}: {ultimo_error}")
            if intento < intentos:
                time.sleep(1)

    log_error_to_file(
        f"NO SE PUDO ESCRIBIR {BREAKEVEN_FILE} tras {intentos} intentos (incluso con escritura atomica). "
        f"Error: {ultimo_error} | El breakeven en Supabase SI se aplico, "
        f"solo no quedo registrado localmente para el filtro de duplicados."
    )
    return False

def telegram_result(signal, result, exit_price, pnl_pts):
    emoji   = "✅" if result == "WIN" else "❌"
    pnl_str = f"+{pnl_pts:.1f}" if pnl_pts > 0 else f"{pnl_pts:.1f}"
    return (
        f"{emoji} <b>{result} — {signal['signal_type']} XAUUSD</b>\n"
        f"---\n"
        f"Estrategia: {signal.get('strategy', '-')}\n"
        f"Entrada: <code>{signal['entry_price']}</code>\n"
        f"Cierre: <code>{exit_price:.2f}</code>\n"
        f"PnL: <b>{pnl_str} puntos</b>\n"
        f"Score: {signal.get('confidence', '-')}/100\n"
        f"---\n"
        f"Trading Pro — Result Tracker"
    )

def telegram_breakeven(signal, current_price):
    return (
        f"BREAKEVEN ACTIVADO\n"
        f"---\n"
        f"{signal['signal_type']} XAUUSD\n"
        f"Entrada: {signal['entry_price']}\n"
        f"Precio actual: {current_price:.2f}\n"
        f"SL movido a breakeven: {signal['entry_price']}\n"
        f"---\n"
        f"Operacion protegida. Riesgo = 0."
    )

def telegram_daily_report(stats):
    winrate   = round(stats['wins'] / stats['total'] * 100, 1) if stats['total'] > 0 else 0
    emoji_wr  = "BIEN" if winrate >= 65 else "OK" if winrate >= 50 else "MAL"
    pnl_str   = f"+{stats['pnl']:.2f}" if stats['pnl'] >= 0 else f"{stats['pnl']:.2f}"

    lines = [
        f"REPORTE DIARIO — XAUUSD (trades reales)",
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        f"---",
        f"Total trades ejecutados: {stats['total']}",
        f"Ganadoras: {stats['wins']}",
        f"Perdedoras: {stats['losses']}",
        f"Winrate: {winrate}% [{emoji_wr}]",
        f"---",
        f"PnL del dia: {pnl_str}",
        f"---",
        f"POR ESTRATEGIA:",
    ]

    by_strategy = stats.get("by_strategy", {})
    ordered = sorted(
        by_strategy.items(),
        key=lambda kv: (kv[1]["wins"] / kv[1]["total"] if kv[1]["total"] else 0),
        reverse=True,
    )
    for strategy_name, s in ordered:
        wr = round(s["wins"] / s["total"] * 100, 1) if s["total"] else 0
        pnl_s = f"+{s['pnl']:.2f}" if s['pnl'] >= 0 else f"{s['pnl']:.2f}"
        lines.append(
            f"  {strategy_name}: {s['wins']}W/{s['losses']}L "
            f"({wr}%) | {pnl_s}"
        )

    lines += ["---", "Trading Pro V3"]
    return "\n".join(lines)

def telegram_strategy_range_report(stats_by_strategy, days):
    lines = [
        f"DESEMPEÑO POR ESTRATEGIA (trades reales) — ultimos {days} dias",
        f"---",
    ]
    ordered = sorted(
        stats_by_strategy.items(),
        key=lambda kv: (kv[1]["wins"] / kv[1]["total"] if kv[1]["total"] else 0),
        reverse=True,
    )
    for strategy_name, s in ordered:
        wr = round(s["wins"] / s["total"] * 100, 1) if s["total"] else 0
        pnl_s = f"+{s['pnl']:.2f}" if s['pnl'] >= 0 else f"{s['pnl']:.2f}"
        flag = "" if s["total"] < 5 else (" [REVISAR]" if wr < 45 else "")
        lines.append(
            f"{strategy_name}: {s['total']} trades | "
            f"{s['wins']}W/{s['losses']}L ({wr}%) | {pnl_s}{flag}"
        )
    lines += ["---", "Menos de 5 trades = muestra insuficiente todavia"]
    return "\n".join(lines)

def get_pending_signals():
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/signals",
        headers=headers(),
        params={
            "status":  "eq.EXECUTING",
            "result":  "is.null",
            "select":  "id,signal_type,entry_price,stop_loss,take_profit_1,take_profit_2,confidence,strategy,created_at",
            "order":   "created_at.desc",
            "limit":   "20",
        },
        timeout=20,
    )
    if r.status_code >= 400:
        print(f"  Error get_pending_signals: {r.status_code} {r.text}")
        return []
    return r.json()

def get_candles_after(created_at, limit=200):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/ohlc_candles",
        headers=headers(),
        params={
            "select":      "candle_time,high,low,close",
            "instrument":  "eq.XAUUSD",
            "timeframe":   "eq.M5",
            "candle_time": f"gt.{created_at}",
            "order":       "candle_time.asc",
            "limit":       str(limit),
        },
        timeout=20,
    )
    if r.status_code >= 400:
        return []
    return r.json()

def get_current_price():
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/ohlc_candles",
        headers=headers(),
        params={
            "select":     "close",
            "instrument": "eq.XAUUSD",
            "timeframe":  "eq.M5",
            "order":      "candle_time.desc",
            "limit":      "1",
        },
        timeout=10,
    )
    if r.status_code >= 400 or not r.json():
        return None
    return float(r.json()[0]["close"])

def update_signal(sig_id, result, result_price):
    try:
        r = requests.patch(
            f"{SUPABASE_URL}/rest/v1/signals?id=eq.{sig_id}",
            headers=headers(),
            json={
                "result": result,
                "status": "CLOSED",
            },
            timeout=15,
        )
        if r.status_code >= 400:
            print(f"  [ERROR] update_signal {sig_id[:8]} -> {r.status_code}: {r.text}")
            return False
        return True
    except Exception as e:
        print(f"  Error update_signal: {e}")
        return False

def update_signal_sl(sig_id, new_sl):
    pass

def _empty_strategy_bucket():
    return {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0}

def _dr_day_bounds_utc(days_ago=0):
    """
    Devuelve (inicio_utc_iso, fin_utc_iso) del dia calendario en zona
    Republica Dominicana (UTC-4, fijo, sin DST) que corresponde a
    'hoy - days_ago'. Se usa para filtrar trades_ejecutados por
    close_time y que el reporte cuadre con el dia de trading real.
    """
    now_dr    = datetime.now(timezone.utc) + DR_OFFSET
    target_dr = (now_dr - timedelta(days=days_ago)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start_utc = target_dr - DR_OFFSET
    end_utc   = start_utc + timedelta(days=1)
    return start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

def _build_stats_from_trades(trades):
    """
    Toma una lista de filas de trades_ejecutados (con profit_neto y
    strategy) y arma el resumen global + por estrategia. Reemplaza a
    _build_stats_from_signals(): el reporte ahora se basa en
    operaciones REALES ejecutadas en MT5, no en señales.
    """
    wins   = sum(1 for t in trades if float(t.get("profit_neto") or 0) > 0)
    losses = sum(1 for t in trades if float(t.get("profit_neto") or 0) <= 0)
    total  = wins + losses
    pnl    = sum(float(t.get("profit_neto") or 0) for t in trades)

    by_strategy = {}
    for t in trades:
        name = t.get("strategy") or "sin_nombre"
        bucket = by_strategy.setdefault(name, _empty_strategy_bucket())
        p = float(t.get("profit_neto") or 0)
        bucket["total"] += 1
        if p > 0:
            bucket["wins"] += 1
        else:
            bucket["losses"] += 1
        bucket["pnl"] += p

    return {
        "total": total, "wins": wins, "losses": losses,
        "pnl": pnl, "by_strategy": by_strategy,
    }

def get_daily_stats():
    """
    Reporte diario basado en trades_ejecutados (operaciones reales en
    MT5), no en la tabla 'signals'. 'signals' incluye señales que
    llegan a CLOSED sin haberse ejecutado nunca como orden real —
    contarlas infla las perdedoras del dia. Ver conversacion del
    2026-08-05: reporte via 'signals' mostro 9 senales / 8 perdedoras,
    cuando en trades_ejecutados solo hubo 4 trades reales / 3 perdedoras.
    """
    start_utc, end_utc = _dr_day_bounds_utc(days_ago=0)
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/trades_ejecutados",
        headers=headers(),
        params={
            "select":     "profit_neto,strategy,close_time",
            "close_time": [f"gte.{start_utc}", f"lt.{end_utc}"],
        },
        timeout=20,
    )
    if r.status_code >= 400:
        print(f"  Error get_daily_stats: {r.status_code} {r.text}")
        return None
    trades = r.json()
    if not trades:
        return None

    return _build_stats_from_trades(trades)

def get_strategy_stats_range(days=7):
    """
    Desempeño por estrategia en los ultimos N dias, basado en
    trades_ejecutados (operaciones reales), no en 'signals'.
    Util para decidir si una estrategia se debe desactivar de
    ALLOWED_STRATEGIES en bot_engine.py.
    """
    start_utc, _ = _dr_day_bounds_utc(days_ago=days - 1)
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/trades_ejecutados",
        headers=headers(),
        params={
            "select":     "profit_neto,strategy,close_time",
            "close_time": f"gte.{start_utc}",
        },
        timeout=20,
    )
    if r.status_code >= 400:
        print(f"  Error get_strategy_stats_range: {r.status_code} {r.text}")
        return None
    trades = r.json()
    if not trades:
        return None

    return _build_stats_from_trades(trades)["by_strategy"]

def run_cycle():
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n[{now_str}] Revisando señales...")

    breakeven_set = load_breakeven_signals()
    signals       = get_pending_signals()

    if not signals:
        print("  Sin señales EXECUTING pendientes de cierre.")
    else:
        print(f"  Revisando {len(signals)} señal(es) EXECUTING...")

    for signal in signals:
        sig_id   = signal["id"]
        sig_type = signal["signal_type"]
        entry    = float(signal["entry_price"])
        sl       = float(signal["stop_loss"])
        tp1      = float(signal["take_profit_1"])
        tp2      = float(signal["take_profit_2"])
        created  = signal["created_at"]

        if sig_type == "BUY":
            sl = sl - ANTI_HUNT_SL_EXTRA
        else:
            sl = sl + ANTI_HUNT_SL_EXTRA

        if sig_id in breakeven_set:
            sl = entry

        candles = get_candles_after(created, 200)
        if not candles:
            print(f"  [{sig_type}] Sin velas M5 posteriores todavía.")
            continue

        result      = None
        exit_price  = None
        tp1_touched = False

        for candle in candles:
            h = float(candle["high"])
            l = float(candle["low"])

            if sig_type == "BUY":
                if h >= tp1 and not tp1_touched:
                    tp1_touched = True
                    if sig_id not in breakeven_set:
                        breakeven_set.add(sig_id)
                        save_breakeven_signals(breakeven_set)
                        update_signal_sl(sig_id, entry)
                        send_telegram(telegram_breakeven(signal, float(candle["close"])))
                        print(f"  BUY {sig_id[:8]} — Breakeven activado @ TP1 {tp1}")
                    sl = entry

                if l <= sl:
                    result     = "WIN" if tp1_touched else "LOSS"
                    exit_price = sl
                    break

                if h >= tp2:
                    result     = "WIN"
                    exit_price = tp2
                    break

            elif sig_type == "SELL":
                if l <= tp1 and not tp1_touched:
                    tp1_touched = True
                    if sig_id not in breakeven_set:
                        breakeven_set.add(sig_id)
                        save_breakeven_signals(breakeven_set)
                        update_signal_sl(sig_id, entry)
                        send_telegram(telegram_breakeven(signal, float(candle["close"])))
                        print(f"  SELL {sig_id[:8]} — Breakeven activado @ TP1 {tp1}")
                    sl = entry

                if h >= sl:
                    result     = "WIN" if tp1_touched else "LOSS"
                    exit_price = sl
                    break

                if l <= tp2:
                    result     = "WIN"
                    exit_price = tp2
                    break

        if result and exit_price:
            pnl_pts = (exit_price - entry) if sig_type == "BUY" else (entry - exit_price)
            update_signal(sig_id, result, exit_price)
            if sig_id in breakeven_set:
                breakeven_set.discard(sig_id)
                save_breakeven_signals(breakeven_set)
            send_telegram(telegram_result(signal, result, exit_price, pnl_pts))
            icon = "WIN" if result == "WIN" else "LOSS"
            print(f"  [{icon}] {sig_type} [{sig_id[:8]}] — {result} @ {exit_price:.2f} ({pnl_pts:+.1f} pts)")
        else:
            age_min = (
                datetime.now(timezone.utc)
                - datetime.fromisoformat(created.replace("Z", "+00:00"))
            ).total_seconds() / 60
            be_tag = "[BREAKEVEN activo]" if sig_id in breakeven_set else ""
            print(f"  [PEND] {sig_type} [{sig_id[:8]}] — En progreso ({age_min:.0f} min) {be_tag}")

    # Reporte diario a las 20:00 UTC (basado en trades_ejecutados)
    now_utc = datetime.now(timezone.utc)
    if now_utc.hour == 20 and now_utc.minute < 2:
        stats = get_daily_stats()
        if stats and stats["total"] > 0:
            send_telegram(telegram_daily_report(stats))
            print(f"\n  Reporte diario enviado: {stats['wins']}W/{stats['losses']}L")

    # Reporte semanal por estrategia, domingos a las 20:00 UTC
    if now_utc.weekday() == 6 and now_utc.hour == 20 and now_utc.minute < 2:
        weekly = get_strategy_stats_range(days=7)
        if weekly:
            send_telegram(telegram_strategy_range_report(weekly, days=7))
            print(f"\n  Reporte semanal por estrategia enviado.")

    print(f"  Ciclo completado.")


def main():
    print(f"\n{'='*50}")
    print(f"  RESULT TRACKER V3 — Loop continuo")
    print(f"  URL: {SUPABASE_URL}")
    print(f"  Colchon anti-hunt SL: {ANTI_HUNT_SL_EXTRA} pts (replica bot_engine.py)")
    print(f"  Revisa cada {LOOP_INTERVAL}s — Ctrl+C para detener")
    print(f"{'='*50}")

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: Faltan SUPABASE_URL o SUPABASE_KEY en .env")
        sys.exit(1)

    try:
        while True:
            try:
                run_cycle()
            except Exception as e:
                print(f"  [ERROR] run_cycle: {e}")
            time.sleep(LOOP_INTERVAL)
    except KeyboardInterrupt:
        print("\nDetenido manualmente (Ctrl+C).")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)
