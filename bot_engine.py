"""
bot_engine.py
=============
Ejecuta ordenes reales en MT5 basado en señales de Supabase.

Proyecto: qilvrvnwdtpbkcfwktqs (activo en dashboard)

Reglas:
- Score minimo: 75 (sincronizado con dashboard)
- Lotaje: 0.02
- SL anti-hunt: 20 puntos extra
- Maximo 1 operacion abierta a la vez
- Maximo 3 operaciones por dia
- Notifica a Telegram al abrir

ADVERTENCIA: Este script ejecuta ordenes REALES en MT5.
"""

import os
import sys
import requests
from datetime import datetime, date
from dotenv import load_dotenv

try:
    import MetaTrader5 as mt5
except ImportError:
    print("ERROR: Instala MetaTrader5: pip install MetaTrader5")
    sys.exit(1)

load_dotenv()

SUPABASE_URL     = os.getenv("SUPABASE_URL")
SUPABASE_KEY     = os.getenv("SUPABASE_KEY")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@XAUUSD_Signals_DR")
MT5_SYMBOL       = os.getenv("MT5_SYMBOL", "GOLD")

# ─── PARÁMETROS DEL BOT ───────────────────────────────────────
LOT_SIZE     = 0.02        # Lotaje fijo
MIN_SCORE    = 75          # Sincronizado con dashboard (index.html)
MAX_DAILY    = 3           # Máximo operaciones por día
SL_EXTRA_PTS = 20          # Puntos extra anti-hunt
MAGIC_NUMBER = 20260601    # ID único para órdenes del bot
DEVIATION    = 20          # Desviación máxima en puntos
# ──────────────────────────────────────────────────────────────

# Estrategias con buen historial de winrate
ALLOWED_STRATEGIES = [
    "Scalping M5",
    "Scalping M1",
    "SMC H1",
    "Ruptura y confirmación",
    "Scalping M5 Engine",
    "H1 Liquidity Engulfing CHOCH",
]

DAILY_FILE = "bot_daily_count.txt"

def headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=representation",
    }

def send_telegram(message):
    if not TELEGRAM_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10
        )
    except:
        pass

def get_daily_count():
    try:
        if os.path.exists(DAILY_FILE):
            with open(DAILY_FILE, "r") as f:
                data = f.read().strip().split(",")
                if len(data) == 2 and data[0] == str(date.today()):
                    return int(data[1])
    except:
        pass
    return 0

def increment_daily_count():
    count = get_daily_count() + 1
    with open(DAILY_FILE, "w") as f:
        f.write(f"{date.today()},{count}")
    return count

def connect_mt5():
    if not mt5.initialize():
        raise RuntimeError(f"No se pudo conectar a MT5: {mt5.last_error()}")
    account = mt5.account_info()
    if account is None:
        raise RuntimeError("MT5 sin cuenta conectada.")
    symbol = mt5.symbol_info(MT5_SYMBOL)
    if symbol is None:
        raise RuntimeError(f"Símbolo {MT5_SYMBOL} no encontrado.")
    if not symbol.visible:
        mt5.symbol_select(MT5_SYMBOL, True)
    return account

def get_open_positions():
    positions = mt5.positions_get(symbol=MT5_SYMBOL)
    if positions is None:
        return []
    return [p for p in positions if p.magic == MAGIC_NUMBER]

def get_pending_signals():
    """Obtiene señales PENDING con score >= MIN_SCORE."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: Faltan SUPABASE_URL o SUPABASE_KEY en .env")
        return []

    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/signals"
        f"?status=eq.PENDING&result=is.null"
        f"&confidence=gte.{MIN_SCORE}"
        f"&select=id,signal_type,entry_price,stop_loss,take_profit_1,take_profit_2,confidence,strategy,created_at"
        f"&order=confidence.desc"
        f"&limit=10",
        headers=headers(),
        timeout=20,
    )
    if r.status_code >= 400:
        print(f"  Error Supabase: {r.status_code} {r.text}")
        return []

    all_signals = r.json()
    filtered    = [s for s in all_signals if s.get("strategy") in ALLOWED_STRATEGIES]
    excluded    = len(all_signals) - len(filtered)
    if excluded > 0:
        print(f"  Filtradas {excluded} señal(es) de estrategias no permitidas.")
    return filtered

def get_current_price():
    tick = mt5.symbol_info_tick(MT5_SYMBOL)
    if tick is None:
        return None, None
    return tick.ask, tick.bid

def calc_anti_hunt_sl(signal_type, original_sl):
    point = mt5.symbol_info(MT5_SYMBOL).point
    extra = SL_EXTRA_PTS * point * 10  # XAUUSD: punto = 0.01
    if signal_type == "BUY":
        return round(original_sl - extra, 2)
    else:
        return round(original_sl + extra, 2)

def execute_order(signal):
    ask, bid = get_current_price()
    if ask is None:
        print("  Sin precio disponible.")
        return None

    signal_type  = signal["signal_type"]
    tp1          = float(signal["take_profit_1"])
    original_sl  = float(signal["stop_loss"])
    sl           = calc_anti_hunt_sl(signal_type, original_sl)
    order_type   = mt5.ORDER_TYPE_BUY if signal_type == "BUY" else mt5.ORDER_TYPE_SELL
    price        = ask if signal_type == "BUY" else bid

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       MT5_SYMBOL,
        "volume":       LOT_SIZE,
        "type":         order_type,
        "price":        price,
        "sl":           sl,
        "tp":           tp1,
        "deviation":    DEVIATION,
        "magic":        MAGIC_NUMBER,
        "comment":      f"TradingPro_{signal['id'][:8]}",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)

    if result is None:
        print(f"  Error enviando orden: {mt5.last_error()}")
        return None

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        print(f"  Orden rechazada: {result.retcode} — {result.comment}")
        return None

    return result

def update_signal_status(sig_id, status):
    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/signals?id=eq.{sig_id}",
            headers=headers(),
            json={"status": status},
            timeout=15,
        )
    except:
        pass

def check_and_close_positions():
    """Notifica si alguna posición del bot fue cerrada por MT5 (SL/TP)."""
    positions    = get_open_positions()
    all_active   = mt5.positions_get(symbol=MT5_SYMBOL)
    active_tickets = {p.ticket for p in all_active} if all_active else set()
    now_str      = datetime.now().strftime("%Y-%m-%d %H:%M")

    for pos in positions:
        if pos.ticket not in active_tickets:
            result_txt = "WIN" if pos.profit > 0 else "LOSS"
            pnl        = round(pos.profit, 2)
            send_telegram(
                f"[{result_txt}] Orden cerrada\n"
                f"Ticket: {pos.ticket}\n"
                f"Tipo: {'BUY' if pos.type == 0 else 'SELL'}\n"
                f"Ganancia: ${pnl}\n"
                f"Hora: {now_str}"
            )
            print(f"  Posicion {pos.ticket} cerrada — {result_txt} ${pnl}")

def main():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*50}")
    print(f"  BOT ENGINE — {now_str}")
    print(f"  URL: {SUPABASE_URL}")
    print(f"  Lote: {LOT_SIZE} | Score min: {MIN_SCORE} | SL extra: {SL_EXTRA_PTS} pts")
    print(f"{'='*50}")

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: Faltan SUPABASE_URL o SUPABASE_KEY en .env")
        return

    # Conectar MT5
    try:
        account = connect_mt5()
        print(f"MT5: {account.login} | Balance: ${account.balance:.2f}")
    except Exception as e:
        print(f"Error MT5: {e}")
        return

    # Revisar si hubo cierres automáticos
    check_and_close_positions()

    # Verificar límite diario
    daily_count = get_daily_count()
    print(f"Operaciones hoy: {daily_count}/{MAX_DAILY}")
    if daily_count >= MAX_DAILY:
        print("Limite diario alcanzado. No se abriran mas operaciones hoy.")
        mt5.shutdown()
        return

    # Verificar si ya hay posición abierta
    open_positions = get_open_positions()
    if open_positions:
        pos = open_positions[0]
        print(f"Posicion abierta: ticket {pos.ticket} | {'BUY' if pos.type == 0 else 'SELL'} | Profit: ${round(pos.profit, 2)}")
        mt5.shutdown()
        return

    # Obtener mejor señal pendiente
    signals = get_pending_signals()
    if not signals:
        print("Sin señales pendientes con score suficiente.")
        mt5.shutdown()
        return

    best      = signals[0]
    sig_type  = best["signal_type"]
    score     = best["confidence"]
    strategy  = best["strategy"]
    sig_id    = best["id"]

    print(f"\nSenal encontrada: {sig_type} | Score: {score}/100 | {strategy}")

    # Ejecutar orden
    print(f"Ejecutando orden {sig_type} en MT5...")
    result = execute_order(best)

    if result is None:
        print("No se pudo ejecutar la orden.")
        mt5.shutdown()
        return

    # Éxito
    count        = increment_daily_count()
    ask, bid     = get_current_price()
    price        = ask if sig_type == "BUY" else bid
    sl           = calc_anti_hunt_sl(sig_type, float(best["stop_loss"]))
    tp1          = float(best["take_profit_1"])

    print(f"\nORDEN EJECUTADA:")
    print(f"  Ticket:  {result.order}")
    print(f"  Tipo:    {sig_type}")
    print(f"  Precio:  {price}")
    print(f"  SL:      {sl} (anti-hunt +{SL_EXTRA_PTS} pts)")
    print(f"  TP1:     {tp1}")
    print(f"  Lote:    {LOT_SIZE}")
    print(f"  Hoy:     {count}/{MAX_DAILY}")

    update_signal_status(sig_id, "EXECUTING")

    send_telegram(
        f"[BOT] ORDEN ABIERTA — {sig_type} XAUUSD\n"
        f"Ticket: {result.order}\n"
        f"Precio entrada: {price}\n"
        f"Stop Loss: {sl} (anti-hunt)\n"
        f"Take Profit: {tp1}\n"
        f"Lote: {LOT_SIZE}\n"
        f"Score: {score}/100\n"
        f"Estrategia: {strategy}\n"
        f"Operaciones hoy: {count}/{MAX_DAILY}\n"
        f"Hora: {now_str}"
    )

    mt5.shutdown()
    print("\nBot engine completado.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            mt5.shutdown()
        except:
            pass
        print(f"ERROR: {e}")
        sys.exit(1)
