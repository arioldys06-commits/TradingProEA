"""
bot_engine.py
=============
Ejecuta ordenes reales en MT5 basado en señales PENDING de Supabase.

TradingProEA - Execution Engine

Reglas:
- Score minimo: 75
- Lotaje dinamico: se calcula segun balance real y % de riesgo (position_sizing.py)
- Vigencia: rechaza señales de mas de 10 min o con precio ya movido (anti señal-vieja)
- Maximo 2 perdidas por dia (real, leido del historial de MT5) — protege la cuenta si se deja corriendo sin supervision
- SL anti-hunt: 20 puntos extra
- Maximo 1 operacion abierta a la vez
- Maximo 3 operaciones por dia
- Solo ejecuta estrategias permitidas
- Notifica a Telegram al abrir

ADVERTENCIA:
Este script ejecuta ordenes REALES en MT5.
Usalo solo en la PC donde MetaTrader 5 este abierto y conectado.

CAMBIOS EN ESTA VERSION (parche de diagnostico):
- Cuando una orden falla (order_send devuelve None o retcode != DONE), ahora
  se captura el motivo exacto (retcode, comment, last_error de MT5) y:
    1. se guarda en bot_errors.log (no se pierde aunque cierres la consola)
    2. se envia por Telegram
  Antes ese detalle solo se imprimia en consola y se perdia.
"""

import os
import sys
import time
import requests
from datetime import datetime, date
from dotenv import load_dotenv

try:
    import MetaTrader5 as mt5
except ImportError:
    print("ERROR: Instala MetaTrader5: pip install MetaTrader5")
    sys.exit(1)

from position_sizing import calculate_lot_size

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@XAUUSD_Signals_DR")
MT5_SYMBOL = os.getenv("MT5_SYMBOL", "GOLD")

# ─── PARAMETROS DEL BOT ───────────────────────────────────────
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "0.5"))  # % del balance arriesgado por operacion
LOT_SIZE_FALLBACK = 0.02  # solo se usa si el calculo dinamico falla (ver execute_order)
MIN_SCORE = 75
MAX_DAILY = 5
MAX_LOSSES_PER_DAY = int(os.getenv("MAX_LOSSES_PER_DAY", "2"))  # corta el dia tras N perdidas
SL_EXTRA_PTS = 20
MAGIC_NUMBER = 20260601
DEVIATION = 20
DAILY_FILE = "bot_daily_count.txt"
ERROR_LOG_FILE = "bot_errors.log"  # NUEVO: guarda el motivo real de cada fallo de ejecucion
LOOP_INTERVAL = int(os.getenv("BOT_LOOP_INTERVAL", "60"))  # segundos entre cada chequeo

# ── Filtro de vigencia (anti señal-vieja) ──
# Si una señal PENDING lleva mas de esto sin ejecutarse (ej: porque el bot
# estuvo apagado, o result_tracker no la cerro a tiempo), se descarta en
# vez de ejecutarla con SL/TP calculados para un precio que ya no existe.
MAX_SIGNAL_AGE_MINUTES = 10
# Si el precio actual se alejo mas de este % de la distancia original al SL,
# la señal tambien se descarta aunque este "fresca" en minutos (movimiento
# de mercado grande = el setup ya no es el mismo).
MAX_PRICE_DRIFT_RATIO = 0.6
# ──────────────────────────────────────────────────────────────

# Estrategias autorizadas para ejecutar en MT5.
# Incluye las estrategias viejas y las nuevas que genera signal_engine.py.
ALLOWED_STRATEGIES = [
    "Scalping M5",
    "Scalping M1",
    "SMC H1",
    "Ruptura y confirmación",
    "Scalping M5 Engine",
    "H1 Liquidity Engulfing CHOCH",

    # Estrategias actuales del signal_engine.py
    "Scalping M5 SMC",
    "Killzone Breakout",
    "FVG Fill M5",
    "EMA Pullback M5",

    # Nuevo motor AI
    "TradingPro AI Elite",
]


def headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def send_telegram(message):
    if not TELEGRAM_TOKEN:
        return False

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"  [TELEGRAM] Error: {e}")
        return False


def log_error_to_file(message):
    """NUEVO: guarda el error en disco para que no se pierda si se cierra la consola."""
    try:
        with open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception as e:
        print(f"  [LOG] Error escribiendo {ERROR_LOG_FILE}: {e}")


def get_daily_count():
    try:
        if os.path.exists(DAILY_FILE):
            with open(DAILY_FILE, "r", encoding="utf-8") as f:
                raw = f.read().strip()

            data = raw.split(",")
            if len(data) == 2 and data[0] == str(date.today()):
                return int(data[1])
    except Exception:
        pass

    return 0


def increment_daily_count():
    count = get_daily_count() + 1

    with open(DAILY_FILE, "w", encoding="utf-8") as f:
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
        raise RuntimeError(f"Simbolo {MT5_SYMBOL} no encontrado en MT5.")

    if not symbol.visible:
        selected = mt5.symbol_select(MT5_SYMBOL, True)
        if not selected:
            raise RuntimeError(f"No se pudo activar el simbolo {MT5_SYMBOL}.")

    return account


def get_daily_losses():
    """
    Cuenta perdidas reales de HOY para nuestras operaciones (magic number),
    leyendo el historial de MT5 directamente — no depende de que
    result_tracker.py haya corrido ni de la tabla signals de Supabase.
    """
    today_start = datetime.combine(date.today(), datetime.min.time())
    deals = mt5.history_deals_get(today_start, datetime.now())

    if deals is None:
        return 0

    closing_deals = [
        d for d in deals
        if getattr(d, "magic", None) == MAGIC_NUMBER
        and getattr(d, "entry", None) == mt5.DEAL_ENTRY_OUT
    ]

    return sum(1 for d in closing_deals if d.profit < 0)


def get_daily_wins():
    today_start = datetime.combine(date.today(), datetime.min.time())
    deals = mt5.history_deals_get(today_start, datetime.now())

    if deals is None:
        return 0

    closing_deals = [
        d for d in deals
        if getattr(d, "magic", None) == MAGIC_NUMBER
        and getattr(d, "entry", None) == mt5.DEAL_ENTRY_OUT
    ]

    return sum(1 for d in closing_deals if d.profit >= 0)


def get_open_positions():
    positions = mt5.positions_get(symbol=MT5_SYMBOL)

    if positions is None:
        return []

    return [p for p in positions if getattr(p, "magic", None) == MAGIC_NUMBER]


def is_signal_stale(signal, current_price):
    """
    Rechaza señales viejas o cuyo precio ya se movio demasiado.
    Devuelve (True, razon) si la señal debe descartarse.
    """
    created_at_raw = signal.get("created_at")
    if not created_at_raw:
        return True, "sin created_at"

    try:
        created_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
        now = datetime.now(created_at.tzinfo)
        age_minutes = (now - created_at).total_seconds() / 60
    except Exception:
        return True, "created_at invalido"

    if age_minutes > MAX_SIGNAL_AGE_MINUTES:
        return True, f"tiene {age_minutes:.0f} min (max {MAX_SIGNAL_AGE_MINUTES})"

    if current_price is not None:
        try:
            entry = float(signal["entry_price"])
            sl = float(signal["stop_loss"])
            riesgo_original = abs(entry - sl)
            if riesgo_original > 0:
                deriva = abs(current_price - entry)
                ratio = deriva / riesgo_original
                if ratio > MAX_PRICE_DRIFT_RATIO:
                    return True, (
                        f"precio se alejo {deriva:.2f} pts "
                        f"({ratio:.1f}x el riesgo original de {riesgo_original:.2f} pts)"
                    )
        except (KeyError, ValueError, ZeroDivisionError):
            pass

    return False, "OK"


def get_pending_signals(current_price=None):
    """Obtiene señales PENDING con confidence >= MIN_SCORE, estrategia permitida y vigentes."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: Faltan SUPABASE_URL o SUPABASE_KEY en .env")
        return []

    url = (
        f"{SUPABASE_URL}/rest/v1/signals"
        f"?status=eq.PENDING"
        f"&result=is.null"
        f"&confidence=gte.{MIN_SCORE}"
        f"&select=id,signal_type,entry_price,stop_loss,take_profit_1,take_profit_2,confidence,strategy,created_at"
        f"&order=confidence.desc,created_at.desc"
        f"&limit=10"
    )

    try:
        r = requests.get(url, headers=headers(), timeout=20)
    except Exception as e:
        print(f"  Error conectando a Supabase: {e}")
        return []

    if r.status_code >= 400:
        print(f"  Error Supabase: {r.status_code} {r.text}")
        return []

    all_signals = r.json()

    filtered = []
    excluded = []
    stale = []

    for signal in all_signals:
        strategy = signal.get("strategy")
        if strategy not in ALLOWED_STRATEGIES:
            excluded.append(strategy)
            continue

        is_stale, reason = is_signal_stale(signal, current_price)
        if is_stale:
            stale.append((signal.get("id", "")[:8], reason))
            continue

        filtered.append(signal)

    if excluded:
        unique_excluded = sorted(set(str(x) for x in excluded))
        print(
            f"  Filtradas {len(excluded)} señal(es) de estrategias no permitidas: "
            f"{', '.join(unique_excluded)}"
        )

    if stale:
        for sig_id, reason in stale:
            print(f"  [VIGENCIA] Señal {sig_id} descartada — {reason}")

    return filtered


def get_current_price():
    tick = mt5.symbol_info_tick(MT5_SYMBOL)

    if tick is None:
        return None, None

    return tick.ask, tick.bid


def calc_anti_hunt_sl(signal_type, original_sl):
    symbol = mt5.symbol_info(MT5_SYMBOL)

    if symbol is None:
        return round(original_sl, 2)

    # En XAUUSD muchos brokers usan point = 0.01.
    # Multiplicamos por 10 para mantener el comportamiento original.
    extra = SL_EXTRA_PTS * symbol.point * 10

    if signal_type == "BUY":
        return round(original_sl - extra, 2)

    return round(original_sl + extra, 2)


def validate_signal(signal):
    required = [
        "id",
        "signal_type",
        "entry_price",
        "stop_loss",
        "take_profit_1",
        "take_profit_2",
        "confidence",
        "strategy",
    ]

    missing = [field for field in required if field not in signal or signal[field] is None]
    if missing:
        return False, f"Señal incompleta. Faltan campos: {', '.join(missing)}"

    if signal["signal_type"] not in ["BUY", "SELL"]:
        return False, f"Tipo de señal invalido: {signal['signal_type']}"

    try:
        float(signal["entry_price"])
        float(signal["stop_loss"])
        float(signal["take_profit_1"])
        float(signal["take_profit_2"])
        int(signal["confidence"])
    except Exception:
        return False, "La señal tiene valores numericos invalidos."

    return True, "OK"


def execute_order(signal):
    """
    Devuelve una tupla (result, error_detail):
      - Si la orden se ejecuta bien: (result_de_mt5, None)
      - Si falla en cualquier punto: (None, "descripcion exacta del motivo")
    Antes esta funcion devolvia solo `result` (o None) y el motivo real del
    fallo se perdia si no estabas viendo la consola en el momento exacto.
    """
    is_valid, reason = validate_signal(signal)
    if not is_valid:
        print(f"  Señal invalida: {reason}")
        return None, f"Señal invalida: {reason}"

    ask, bid = get_current_price()
    if ask is None or bid is None:
        print("  Sin precio disponible en MT5.")
        return None, "Sin precio disponible en MT5 (symbol_info_tick devolvio None)"

    signal_type = signal["signal_type"]
    original_sl = float(signal["stop_loss"])
    tp1 = float(signal["take_profit_1"])
    sl = calc_anti_hunt_sl(signal_type, original_sl)

    order_type = mt5.ORDER_TYPE_BUY if signal_type == "BUY" else mt5.ORDER_TYPE_SELL
    price = ask if signal_type == "BUY" else bid

    # ── Lotaje dinamico segun balance real y % de riesgo ──
    # Usa el SL YA ajustado (anti-hunt) para que el riesgo calculado
    # sea el riesgo real de la operacion, no el de la señal original.
    account = mt5.account_info()
    lote, detalle = calculate_lot_size(
        mt5=mt5,
        symbol=MT5_SYMBOL,
        entry_price=price,
        stop_loss=sl,
        balance=account.balance if account else 0,
        risk_percent=RISK_PERCENT,
    )

    if lote is None:
        print(f"  [RIESGO] No se pudo calcular lote dinamico: {detalle}")
        print(f"  [RIESGO] Usando lote de respaldo: {LOT_SIZE_FALLBACK}")
        lote = LOT_SIZE_FALLBACK
    else:
        print(
            f"  [RIESGO] Balance: ${detalle['balance']} | "
            f"Riesgo: {detalle['risk_percent']}% (${detalle['riesgo_dinero']}) | "
            f"SL: {detalle['distancia_stop_pts']} pts | Lote: {lote}"
        )
        if "aviso" in detalle:
            print(f"  [RIESGO] AVISO: {detalle['aviso']}")

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": MT5_SYMBOL,
        "volume": lote,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp1,
        "deviation": DEVIATION,
        "magic": MAGIC_NUMBER,
        "comment": f"TradingPro_{signal['id'][:8]}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)

    if result is None:
        error_code, error_desc = mt5.last_error()
        detalle_error = (
            f"order_send devolvio None. MT5 last_error: {error_code} - {error_desc} | "
            f"volumen={lote}, precio={price}, sl={sl}, tp={tp1}, "
            f"balance=${account.balance if account else '?'}"
        )
        print(f"  Error enviando orden: {detalle_error}")
        return None, detalle_error

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        detalle_error = (
            f"Orden rechazada. Retcode: {result.retcode} - {result.comment} | "
            f"volumen={lote}, precio={price}, sl={sl}, tp={tp1}, "
            f"balance=${account.balance if account else '?'}"
        )
        print(f"  {detalle_error}")
        return None, detalle_error

    return result, None


def update_signal_status(sig_id, status):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False

    try:
        r = requests.patch(
            f"{SUPABASE_URL}/rest/v1/signals?id=eq.{sig_id}",
            headers=headers(),
            json={"status": status},
            timeout=15,
        )

        if r.status_code >= 400:
            print(f"  Error actualizando señal {sig_id}: {r.status_code} {r.text}")
            return False

        return True
    except Exception as e:
        print(f"  Error update_signal_status: {e}")
        return False


# Evita mandar el aviso de "limite de perdidas" mas de una vez por dia.
_loss_alert_sent_date = None


def run_cycle():
    """Un solo chequeo: busca señal vigente y ejecuta si corresponde. No conecta ni desconecta MT5."""
    global _loss_alert_sent_date
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    daily_count = get_daily_count()
    if daily_count >= MAX_DAILY:
        print(f"  [{now_str}] Limite diario alcanzado ({daily_count}/{MAX_DAILY}). Esperando al dia siguiente.")
        return

    daily_losses = get_daily_losses()
    if daily_losses >= MAX_LOSSES_PER_DAY:
        print(
            f"  [{now_str}] Limite de perdidas diarias alcanzado "
            f"({daily_losses}/{MAX_LOSSES_PER_DAY}). Se detiene por hoy para proteger la cuenta."
        )
        if _loss_alert_sent_date != date.today():
            send_telegram(
                f"[BOT] Limite de perdidas alcanzado — {daily_losses}/{MAX_LOSSES_PER_DAY}\n"
                f"El bot deja de operar por hoy para proteger la cuenta.\n"
                f"Hora: {now_str}"
            )
            _loss_alert_sent_date = date.today()
        return

    open_positions = get_open_positions()
    if open_positions:
        pos = open_positions[0]
        side = "BUY" if pos.type == 0 else "SELL"
        print(
            f"  [{now_str}] Posicion abierta: ticket {pos.ticket} | {side} | "
            f"Profit: ${round(pos.profit, 2)}"
        )
        return

    signals = get_pending_signals(current_price=get_current_price()[0])
    if not signals:
        print(f"  [{now_str}] Sin señales pendientes con score suficiente, estrategia permitida y vigentes.")
        return

    best = signals[0]
    sig_id = best["id"]
    sig_type = best["signal_type"]
    score = best["confidence"]
    strategy = best["strategy"]

    print(f"\n  [{now_str}] Señal encontrada: {sig_type} | Score: {score}/100 | {strategy}")
    print("  Ejecutando orden en MT5...")

    result, error_detail = execute_order(best)

    if result is None:
        print("  No se pudo ejecutar la orden.")
        log_error_to_file(f"Señal {sig_id[:8]} ({sig_type}, score {score}, {strategy}): {error_detail}")
        update_signal_status(sig_id, "FAILED")
        send_telegram(
            f"[BOT] ORDEN FALLIDA - {sig_type} XAUUSD\n"
            f"Score: {score}/100 | Estrategia: {strategy}\n"
            f"Motivo: {error_detail}\n"
            f"Hora: {now_str}"
        )
        return

    count = increment_daily_count()
    ask, bid = get_current_price()
    price = ask if sig_type == "BUY" else bid
    sl = calc_anti_hunt_sl(sig_type, float(best["stop_loss"]))
    tp1 = float(best["take_profit_1"])

    update_signal_status(sig_id, "EXECUTING")

    print("\n  ORDEN EJECUTADA:")
    print(f"    Ticket: {result.order}")
    print(f"    Tipo:   {sig_type}")
    print(f"    Precio: {price}")
    print(f"    SL:     {sl} (anti-hunt +{SL_EXTRA_PTS} pts)")
    print(f"    TP1:    {tp1}")
    print(f"    Lote:   {result.volume}")
    print(f"    Hoy:    {count}/{MAX_DAILY}")

    send_telegram(
        f"[BOT] ORDEN ABIERTA - {sig_type} XAUUSD\n"
        f"Ticket: {result.order}\n"
        f"Precio entrada: {price}\n"
        f"Stop Loss: {sl} (anti-hunt)\n"
        f"Take Profit: {tp1}\n"
        f"Lote: {result.volume} (riesgo: {RISK_PERCENT}% del balance)\n"
        f"Score: {score}/100\n"
        f"Estrategia: {strategy}\n"
        f"Operaciones hoy: {count}/{MAX_DAILY}\n"
        f"Hora: {now_str}"
    )


def ensure_mt5_connected():
    """Verifica que MT5 siga conectado; si se cayo, intenta reconectar."""
    account = mt5.account_info()
    if account is not None:
        return account

    print("  [MT5] Conexion perdida. Intentando reconectar...")
    return connect_mt5()


def main():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n{'=' * 55}")
    print(f"  BOT ENGINE - TradingProEA - {now_str}")
    print(f"  URL: {SUPABASE_URL}")
    print(f"  Simbolo MT5: {MT5_SYMBOL}")
    print(f"  Riesgo por operacion: {RISK_PERCENT}% del balance | Score min: {MIN_SCORE} | SL extra: {SL_EXTRA_PTS} pts")
    print(f"  Max diario: {MAX_DAILY} operaciones | Max perdidas/dia: {MAX_LOSSES_PER_DAY}")
    print(f"  Vigencia: max {MAX_SIGNAL_AGE_MINUTES} min | deriva max {MAX_PRICE_DRIFT_RATIO}x el riesgo original")
    print(f"  Estrategias permitidas: {len(ALLOWED_STRATEGIES)}")
    print(f"  Loop: cada {LOOP_INTERVAL}s — Ctrl+C para detener")
    print(f"{'=' * 55}")

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: Faltan SUPABASE_URL o SUPABASE_KEY en .env")
        return

    try:
        account = connect_mt5()
        print(f"MT5 conectado: {account.login} | Balance: ${account.balance:.2f}")
    except Exception as e:
        print(f"Error MT5: {e}")
        return

    if TELEGRAM_TOKEN:
        send_telegram(
            f"[BOT] bot_engine.py INICIADO\n"
            f"Riesgo: {RISK_PERCENT}% | Score min: {MIN_SCORE} | Max diario: {MAX_DAILY}\n"
            f"Loop: {LOOP_INTERVAL}s\n"
            f"Hora: {now_str}"
        )

    try:
        while True:
            try:
                ensure_mt5_connected()
                run_cycle()
            except Exception as e:
                print(f"  [ERROR] run_cycle: {e}")
            time.sleep(LOOP_INTERVAL)
    except KeyboardInterrupt:
        print("\nDetenido manualmente (Ctrl+C).")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            mt5.shutdown()
        except Exception:
            pass

        print(f"ERROR: {e}")
        sys.exit(1)
