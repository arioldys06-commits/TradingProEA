"""
result_tracker.py — V3
======================
Revisa señales PENDING/EXECUTING y actualiza WIN/LOSS comparando
con las velas reales de Supabase.

Proyecto: qilvrvnwdtpbkcfwktqs (activo en dashboard)

CAMBIOS EN ESTA VERSION (V3 — desglose por estrategia):
  - get_daily_stats() ahora agrupa resultados por 'strategy', no solo
    el total del dia. Antes: {total, wins, losses, pnl, avg_score}.
    Ahora ademas incluye 'by_strategy': {nombre_estrategia: {...}}.
  - Nueva funcion get_strategy_stats_range(days) para ver el
    desempeño historico por estrategia (no solo el dia de hoy) —
    util para decidir si alguna estrategia hay que apagar.
  - telegram_daily_report() ahora imprime una tabla con el
    desglose por estrategia, ademas del resumen general.
  - Se mantiene 100% la logica original de cierre de señales
    (breakeven, WIN/LOSS, velas M5) — no se toco esa parte.

FIX (esta version): get_pending_signals() solo buscaba señales con
  status=PENDING. Pero bot_engine.py cambia el status a EXECUTING en
  cuanto abre la orden real en MT5 — asi que, para cuando este script
  corria, la señal ya habia dejado de ser PENDING y nunca se volvia a
  revisar. Resultado: ninguna señal ejecutada llegaba jamas a CLOSED
  con WIN/LOSS, y el desglose por estrategia se quedaba siempre vacio
  aunque el codigo para calcularlo ya estuviera bien escrito.
  Ahora el filtro incluye status en (PENDING, EXECUTING), asi que las
  señales que ya se ejecutaron en MT5 tambien se siguen revisando
  hasta que toquen SL o TP y puedan cerrarse correctamente.
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

def load_breakeven_signals():
    if os.path.exists(BREAKEVEN_FILE):
        with open(BREAKEVEN_FILE, "r") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_breakeven_signals(sig_set):
    with open(BREAKEVEN_FILE, "w") as f:
        for s in list(sig_set)[-500:]:
            f.write(s + "\n")

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
    pnl_str   = f"+{stats['pnl']:.1f}" if stats['pnl'] >= 0 else f"{stats['pnl']:.1f}"

    lines = [
        f"REPORTE DIARIO — XAUUSD",
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        f"---",
        f"Total senales: {stats['total']}",
        f"Ganadoras: {stats['wins']}",
        f"Perdedoras: {stats['losses']}",
        f"Winrate: {winrate}% [{emoji_wr}]",
        f"---",
        f"PnL del dia: {pnl_str} pts",
        f"Score promedio: {stats['avg_score']:.0f}/100",
        f"---",
        f"POR ESTRATEGIA:",
    ]

    # Desglose por estrategia, ordenado de mejor a peor winrate
    by_strategy = stats.get("by_strategy", {})
    ordered = sorted(
        by_strategy.items(),
        key=lambda kv: (kv[1]["wins"] / kv[1]["total"] if kv[1]["total"] else 0),
        reverse=True,
    )
    for strategy_name, s in ordered:
        wr = round(s["wins"] / s["total"] * 100, 1) if s["total"] else 0
        pnl_s = f"+{s['pnl']:.1f}" if s['pnl'] >= 0 else f"{s['pnl']:.1f}"
        lines.append(
            f"  {strategy_name}: {s['wins']}W/{s['losses']}L "
            f"({wr}%) | {pnl_s} pts"
        )

    lines += ["---", "Trading Pro V3"]
    return "\n".join(lines)

def telegram_strategy_range_report(stats_by_strategy, days):
    lines = [
        f"DESEMPEÑO POR ESTRATEGIA — ultimos {days} dias",
        f"---",
    ]
    ordered = sorted(
        stats_by_strategy.items(),
        key=lambda kv: (kv[1]["wins"] / kv[1]["total"] if kv[1]["total"] else 0),
        reverse=True,
    )
    for strategy_name, s in ordered:
        wr = round(s["wins"] / s["total"] * 100, 1) if s["total"] else 0
        pnl_s = f"+{s['pnl']:.1f}" if s['pnl'] >= 0 else f"{s['pnl']:.1f}"
        flag = "" if s["total"] < 5 else (" [REVISAR]" if wr < 45 else "")
        lines.append(
            f"{strategy_name}: {s['total']} señales | "
            f"{s['wins']}W/{s['losses']}L ({wr}%) | {pnl_s} pts{flag}"
        )
    lines += ["---", "Menos de 5 señales = muestra insuficiente todavia"]
    return "\n".join(lines)

def get_pending_signals():
    """
    Trae señales que aun necesitan seguimiento: las recien publicadas
    (PENDING) y las que bot_engine.py ya ejecuto en MT5 (EXECUTING).
    Antes solo buscaba PENDING, asi que las señales ejecutadas nunca
    volvian a revisarse ni a cerrarse — quedaban congeladas para siempre.
    """
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/signals",
        headers=headers(),
        params={
            "status":  "in.(PENDING,EXECUTING)",
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
    """Usa M5 — es el timeframe mínimo garantizado en Supabase."""
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
    """
    Marca la señal como CLOSED con su resultado (WIN/LOSS).
    NOTA: la tabla 'signals' no tiene columnas 'result_price' ni
    'result_at' (solo existen id, created_at, instrument, timeframe,
    signal_type, strategy, confidence, entry_price, stop_loss,
    take_profit_1, take_profit_2, status, result). Antes se intentaba
    escribir esas columnas inexistentes, Supabase rechazaba el PATCH
    completo (400), y como no se revisaba el status code, el error
    quedaba en silencio: la señal nunca pasaba a CLOSED de verdad,
    aunque en consola pareciera que sí. result_price se recibe igual
    (se usa en el mensaje de Telegram) pero ya no se intenta guardar
    en una columna que no existe.
    """
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
    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/signals?id=eq.{sig_id}",
            headers=headers(),
            json={"stop_loss": new_sl},
            timeout=15,
        )
    except Exception as e:
        print(f"  Error update_signal_sl: {e}")

def _empty_strategy_bucket():
    return {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0}

def _build_stats_from_signals(signals):
    """
    Toma una lista de señales CLOSED (con result, entry_price, take_profit_1,
    stop_loss, confidence, strategy) y arma el resumen global + por estrategia.
    Compartido por get_daily_stats() y get_strategy_stats_range().
    """
    wins   = sum(1 for s in signals if s.get("result") == "WIN")
    losses = sum(1 for s in signals if s.get("result") == "LOSS")
    total  = wins + losses

    def pnl_of(s):
        entry = float(s.get("entry_price", 0))
        if s.get("result") == "WIN":
            return float(s.get("take_profit_1", 0)) - entry
        return float(s.get("stop_loss", 0)) - entry

    pnl = sum(pnl_of(s) for s in signals if s.get("result") in ("WIN", "LOSS"))
    avg_score = sum(s.get("confidence", 0) for s in signals) / len(signals) if signals else 0

    # Desglose por estrategia
    by_strategy = {}
    for s in signals:
        if s.get("result") not in ("WIN", "LOSS"):
            continue
        name = s.get("strategy") or "sin_nombre"
        bucket = by_strategy.setdefault(name, _empty_strategy_bucket())
        bucket["total"] += 1
        if s["result"] == "WIN":
            bucket["wins"] += 1
        else:
            bucket["losses"] += 1
        bucket["pnl"] += pnl_of(s)

    return {
        "total": total, "wins": wins, "losses": losses,
        "pnl": pnl, "avg_score": avg_score,
        "by_strategy": by_strategy,
    }

def get_daily_stats():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/signals",
        headers=headers(),
        params={
            "select":     "result,entry_price,take_profit_1,stop_loss,confidence,strategy",
            "status":     "eq.CLOSED",
            "created_at": f"gte.{today}T00:00:00Z",
        },
        timeout=20,
    )
    if r.status_code >= 400:
        return None
    signals = r.json()
    if not signals:
        return None

    return _build_stats_from_signals(signals)

def get_strategy_stats_range(days=7):
    """
    Desempeño por estrategia en los ultimos N dias (no solo hoy).
    Util para decidir si una estrategia se debe desactivar de
    ALLOWED_STRATEGIES en bot_engine.py.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/signals",
        headers=headers(),
        params={
            "select":     "result,entry_price,take_profit_1,stop_loss,confidence,strategy",
            "status":     "eq.CLOSED",
            "created_at": f"gte.{since}",
        },
        timeout=20,
    )
    if r.status_code >= 400:
        print(f"  Error get_strategy_stats_range: {r.status_code} {r.text}")
        return None
    signals = r.json()
    if not signals:
        return None

    return _build_stats_from_signals(signals)["by_strategy"]

def run_cycle():
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n[{now_str}] Revisando señales...")

    breakeven_set = load_breakeven_signals()
    signals       = get_pending_signals()

    if not signals:
        print("  Sin señales PENDING/EXECUTING.")
    else:
        print(f"  Revisando {len(signals)} señal(es) PENDING/EXECUTING...")

    for signal in signals:
        sig_id   = signal["id"]
        sig_type = signal["signal_type"]
        entry    = float(signal["entry_price"])
        sl       = float(signal["stop_loss"])
        tp1      = float(signal["take_profit_1"])
        tp2      = float(signal["take_profit_2"])
        created  = signal["created_at"]

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

    # Reporte diario a las 20:00 UTC (con desglose por estrategia)
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
