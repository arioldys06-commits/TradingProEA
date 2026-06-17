"""
result_tracker.py — V2
======================
Revisa señales PENDING y actualiza WIN/LOSS comparando
con las velas reales de Supabase.

Proyecto: qilvrvnwdtpbkcfwktqs (activo en dashboard)

Correcciones aplicadas:
  - URL Supabase apunta al proyecto correcto (via .env)
  - Usa timeframe M5 en lugar de M1 (M1 no siempre existe)
  - Removido filtro "instrument" en get_daily_stats (columna no existe en signals)
  - Trailing stop: cuando toca TP1 mueve SL a breakeven
  - Alertas Telegram al cerrar (WIN/LOSS/BREAKEVEN)
  - Reporte diario a las 20:00 UTC
"""

import os
import sys
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL     = os.getenv("SUPABASE_URL")
SUPABASE_KEY     = os.getenv("SUPABASE_KEY")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@XAUUSD_Signals_DR")

BREAKEVEN_FILE = "breakeven_signals.txt"

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
    return (
        f"REPORTE DIARIO — XAUUSD\n"
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n"
        f"---\n"
        f"Total senales: {stats['total']}\n"
        f"Ganadoras: {stats['wins']}\n"
        f"Perdedoras: {stats['losses']}\n"
        f"Winrate: {winrate}% [{emoji_wr}]\n"
        f"---\n"
        f"PnL del dia: {pnl_str} pts\n"
        f"Score promedio: {stats['avg_score']:.0f}/100\n"
        f"---\n"
        f"Trading Pro V2"
    )

def get_pending_signals():
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/signals",
        headers=headers(),
        params={
            "status": "eq.PENDING",
            "result": "is.null",
            "select": "id,signal_type,entry_price,stop_loss,take_profit_1,take_profit_2,confidence,strategy,created_at",
            "order":  "created_at.desc",
            "limit":  "20",
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
            "timeframe":   "eq.M5",          # CORREGIDO: era M1, no existe siempre
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
            "timeframe":  "eq.M5",           # CORREGIDO: era M1
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
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/signals?id=eq.{sig_id}",
            headers=headers(),
            json={
                "result":       result,
                "result_price": result_price,
                "result_at":    datetime.now(timezone.utc).isoformat(),
                "status":       "CLOSED",
            },
            timeout=15,
        )
    except Exception as e:
        print(f"  Error update_signal: {e}")

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

def get_daily_stats():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/signals",
        headers=headers(),
        params={
            "select":     "result,entry_price,take_profit_1,stop_loss,confidence",
            "status":     "eq.CLOSED",
            "created_at": f"gte.{today}T00:00:00Z",
            # CORREGIDO: removido filtro "instrument" — columna no existe en signals
        },
        timeout=20,
    )
    if r.status_code >= 400:
        return None
    signals = r.json()
    if not signals:
        return None

    wins      = sum(1 for s in signals if s.get("result") == "WIN")
    losses    = sum(1 for s in signals if s.get("result") == "LOSS")
    total     = wins + losses
    pnl       = sum(
        (float(s.get("take_profit_1", 0)) - float(s.get("entry_price", 0)))
        if s.get("result") == "WIN"
        else (float(s.get("stop_loss", 0)) - float(s.get("entry_price", 0)))
        for s in signals if s.get("result") in ("WIN", "LOSS")
    )
    avg_score = sum(s.get("confidence", 0) for s in signals) / len(signals) if signals else 0

    return {"total": total, "wins": wins, "losses": losses, "pnl": pnl, "avg_score": avg_score}

def main():
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*50}")
    print(f"  RESULT TRACKER V2 — {now_str}")
    print(f"  URL: {SUPABASE_URL}")
    print(f"{'='*50}")

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: Faltan SUPABASE_URL o SUPABASE_KEY en .env")
        sys.exit(1)

    breakeven_set = load_breakeven_signals()
    signals       = get_pending_signals()

    if not signals:
        print("  Sin señales PENDING.")
    else:
        print(f"  Revisando {len(signals)} señal(es) PENDING...")

    for signal in signals:
        sig_id   = signal["id"]
        sig_type = signal["signal_type"]
        entry    = float(signal["entry_price"])
        sl       = float(signal["stop_loss"])
        tp1      = float(signal["take_profit_1"])
        tp2      = float(signal["take_profit_2"])
        created  = signal["created_at"]

        # Ajustar SL si ya está en breakeven
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

    # Reporte diario a las 20:00 UTC
    now_utc = datetime.now(timezone.utc)
    if now_utc.hour == 20 and now_utc.minute < 2:
        stats = get_daily_stats()
        if stats and stats["total"] > 0:
            send_telegram(telegram_daily_report(stats))
            print(f"\n  Reporte diario enviado: {stats['wins']}W/{stats['losses']}L")

    print(f"\nResult tracker V2 completado.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)
