# -*- coding: utf-8 -*-
"""
strategy_countertrend_smasher.py
================================
Estrategia: Countertrend Smasher (port a Python del EA de MT5
CountertrendSmasher.mq5, misma logica y mismos parametros por defecto).

Logica (igual que el EA):
  1. Estructura HH/HL/LH/LL en M1, M5, M15, H1 y H4 con swings fractales
     (SWING_STRENGTH velas a cada lado).
  2. REGLA DURA: no compra si el precio esta en (o sobre) un HH confirmado
     en >= MIN_TF_CONFIRM timeframes (zona HH_ZONE_ATR x ATR). No vende en
     un LL confirmado en >= MIN_TF_CONFIRM timeframes.
  3. Gatillo en la vela M5 CERRADA, en un swing clave de M5 o M15:
       - Barrido de liquidez: la mecha atraviesa el nivel y cierra de vuelta.
       - Doble suelo / doble techo: la mecha retestea el nivel (LEVEL_TOL_ATR).
     La mecha de rechazo debe ser >= MIN_WICK_RATIO del rango de la vela.
  4. Confluencia (proxy de "huella" / sentimiento, 0..3):
       - pico de volumen de ticks en la vela gatillo,
       - presion de vela ponderada por volumen girando a favor,
       - sesgo de H4 no en contra.
     Se exige MIN_SCORE (3 = modo sniper, igual que el EA).
  5. SL detras del extremo de la mecha + SL_BUFFER_ATR x ATR, se descarta si
     el SL es mayor que MAX_SL_ATR x ATR. TP = RR x riesgo. Breakeven a
     BREAKEVEN_R (lo gestiona bot_engine.py con check_breakeven_cts()).

INTEGRACION
-----------
- signal_engine.py llama evaluate() en cada analyze() y registra con
  register_published() las señales que si se publicaron (limite de 2/dia).
- bot_engine.py usa calc_lots() para el tamaño de posicion (2% de equity,
  salta el trade si el lote minimo arriesga mas de MAX_RISK_PCT_MIN_LOT %),
  NO aplica el SL anti-hunt, NO aplica trailing, y mueve a breakeven a 1R.
- backtest_engine.py usa evaluate(..., stateless=True).

Este modulo no hace requests ni importa MetaTrader5: recibe las velas ya
convertidas por signal_engine.to_candles() ({"time","O","H","L","C","V"},
orden cronologico, time en ISO UTC).
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

STRATEGY_NAME = "Countertrend Smasher M5"

# =========================================================================
# PARAMETROS — mismos defaults que los inputs del EA de MT5
# =========================================================================
ENTRY_TF = "M5"                                 # InpEntryTF
LEVEL_TF = "M15"                                # InpLevelTF
BIAS_TF = "H4"                                  # InpBiasTF
STRUCTURE_TFS = ["M1", "M5", "M15", "H1", "H4"]  # InpUseM1..InpUseH4

SWING_STRENGTH = 3        # InpSwingStrength
SWING_LOOKBACK = 300      # InpSwingLookback
MIN_TF_CONFIRM = 2        # InpMinTFConfirm
HH_ZONE_ATR = 1.0         # InpHHZoneATR

USE_SWEEP = True          # InpUseSweep
USE_DOUBLE_TB = True      # InpUseDoubleTB
LEVELS_PER_TF = 4         # InpLevelsPerTF
LEVEL_TOL_ATR = 0.25      # InpLevelTolATR
MAX_SWEEP_ATR = 1.5       # InpMaxSweepATR
MIN_BARS_FROM_LEVEL = 5   # InpMinBarsFromLevel
MIN_WICK_RATIO = 0.6      # InpMinWickRatio
MIN_RANGE_ATR = 0.5       # InpMinRangeATR
REQUIRE_COLOR = True      # InpRequireColor
ATR_PERIOD = 14           # InpATRPeriod

VOL_MULT = 1.3            # InpVolMult
VOL_AVG_BARS = 20         # InpVolAvgBars
SENT_FAST_BARS = 3        # InpSentFastBars
SENT_SLOW_BARS = 20       # InpSentSlowBars
MIN_SCORE = 3             # InpMinScore (0..3)

RISK_PCT = 2.0            # InpRiskPct
MAX_RISK_PCT_MIN_LOT = 5.0  # InpMaxRiskPctMinLot
RR = 3.0                  # InpRR
SL_BUFFER_ATR = 0.1       # InpSLBufferATR
MAX_SL_ATR = 2.0          # InpMaxSLATR
MAX_TRADES_PER_DAY = 2    # InpMaxTradesPerDay
MAX_MARGIN_USE_PCT = 50.0  # InpMaxMarginUsePct
BREAKEVEN_R = 1.0         # InpBreakevenR
BE_LOCK_POINTS = 10       # InpBELockPoints

MAX_SPREAD_POINTS = 60    # InpMaxSpreadPoints (lo aplica bot_engine.py)
START_HOUR = 1            # InpStartHour (hora del servidor MT5, EET/EEST)
END_HOUR = 22             # InpEndHour

TF_MINUTES = {"M1": 1, "M3": 3, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H2": 120, "H4": 240}

# Puntaje 0..3 del EA -> confidence 0..100 que usa el resto del sistema.
# 3/3 = 100, 2/3 = 92, 1/3 = 84, 0/3 = 76 (todas >= MIN_SCORE=75 del bot,
# el filtro real es MIN_SCORE de esta estrategia).
CONFIDENCE_BY_SCORE = {0: 76, 1: 84, 2: 92, 3: 100}

# Estado en memoria (solo modo en vivo)
_last_bar_time = None
_trades_today = 0
_trades_day = None


# =========================================================================
# Utilidades
# =========================================================================

def _parse_time(t):
    if isinstance(t, datetime):
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(t).replace("Z", "+00:00"))


def _eu_dst_active(dt_utc):
    """Horario de verano europeo: ultimo domingo de marzo 01:00 UTC al
    ultimo domingo de octubre 01:00 UTC (mismo criterio que bot_engine.py)."""
    def last_sunday(month):
        d = datetime(dt_utc.year, month, 31, 1, 0, tzinfo=timezone.utc)
        return d - timedelta(days=(d.weekday() + 1) % 7)
    return last_sunday(3) <= dt_utc < last_sunday(10)


def broker_hour(now_utc):
    """Hora del servidor de XM (EET = UTC+2, EEST = UTC+3)."""
    offset = 3 if _eu_dst_active(now_utc) else 2
    return (now_utc + timedelta(hours=offset)).hour


def in_session(now_utc):
    """InSession() del EA: ventana en hora del servidor."""
    if START_HOUR == END_HOUR:
        return True
    h = broker_hour(now_utc)
    if START_HOUR < END_HOUR:
        return START_HOUR <= h < END_HOUR
    return h >= START_HOUR or h < END_HOUR


def closed_bars(candles, tf, now_utc):
    """Quita la vela en formacion (Supabase guarda la vela actual de MT5)."""
    minutes = TF_MINUTES[tf]
    out = []
    for c in candles:
        if _parse_time(c["time"]) + timedelta(minutes=minutes) <= now_utc:
            out.append(c)
    return out


def calc_atr(closed, period=ATR_PERIOD):
    """ATR simple de MT5 (media de los ultimos `period` true ranges) sobre
    velas cerradas, igual que iATR leido en la vela 1."""
    if len(closed) < period + 1:
        return 0.0
    trs = []
    for i in range(len(closed) - period, len(closed)):
        h, l, pc = closed[i]["H"], closed[i]["L"], closed[i - 1]["C"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / period


# =========================================================================
# Estructura de mercado (GetSwings / BuildState del EA)
# =========================================================================

def get_swings(candles, strength=SWING_STRENGTH, lookback=SWING_LOOKBACK, max_swings=2):
    """Swings mas recientes (indice 0 = el mas nuevo). `candles` en orden
    cronologico; puede incluir la vela en formacion, igual que CopyHigh
    desde la posicion 0 en el EA. Devuelve (highs, high_times, lows, low_times)."""
    need = lookback + 2 * strength + 2
    bars = list(reversed(candles[-need:]))  # serie: 0 = mas nueva
    n = len(bars)
    highs, h_times, lows, l_times = [], [], [], []
    for i in range(strength + 1, n - strength):
        is_h = len(highs) < max_swings
        is_l = len(lows) < max_swings
        if not is_h and not is_l:
            break
        for k in range(1, strength + 1):
            if not (is_h or is_l):
                break
            # velas mas nuevas (i-k) estrictamente menores/mayores,
            # mas viejas (i+k) menores-o-iguales / mayores-o-iguales
            if bars[i]["H"] <= bars[i - k]["H"] or bars[i]["H"] < bars[i + k]["H"]:
                is_h = False
            if bars[i]["L"] >= bars[i - k]["L"] or bars[i]["L"] > bars[i + k]["L"]:
                is_l = False
        if is_h:
            highs.append(bars[i]["H"])
            h_times.append(bars[i]["time"])
        if is_l:
            lows.append(bars[i]["L"])
            l_times.append(bars[i]["time"])
    return highs, h_times, lows, l_times


def build_state(candles):
    """HH/HL/LH/LL de un timeframe. None si no hay 2 swings de cada lado."""
    hs, _, ls, _ = get_swings(candles, max_swings=2)
    if len(hs) < 2 or len(ls) < 2:
        return None
    hi_label = 1 if hs[0] > hs[1] else -1
    lo_label = 1 if ls[0] > ls[1] else -1
    bias = 0
    if hi_label == 1 and lo_label == 1:
        bias = 1
    elif hi_label == -1 and lo_label == -1:
        bias = -1
    return {"hi0": hs[0], "hi1": hs[1], "lo0": ls[0], "lo1": ls[1],
            "hi_label": hi_label, "lo_label": lo_label, "bias": bias}


def structure_labels(state):
    if not state:
        return "n/a"
    hl = "HH" if state["hi_label"] == 1 else "LH"
    ll = "HL" if state["lo_label"] == 1 else "LL"
    return f"{hl}/{ll}"


def count_confirmed_hh(states, price, zone):
    c, level = 0, 0.0
    for s in states.values():
        if not s or s["hi_label"] != 1:
            continue
        if price >= s["hi0"] - zone:
            c += 1
            level = max(level, s["hi0"])
    return c, level


def count_confirmed_ll(states, price, zone):
    c, level = 0, 0.0
    for s in states.values():
        if not s or s["lo_label"] != -1:
            continue
        if price <= s["lo0"] + zone:
            c += 1
            level = s["lo0"] if level == 0 else min(level, s["lo0"])
    return c, level


# =========================================================================
# Gatillo y confluencia (DetectTrigger / ConfluenceScore del EA)
# =========================================================================

def pressure(series, start, count):
    """Presion de vela ponderada por volumen de ticks en [-1, 1].
    `series` en orden serie (0 = ultima vela cerrada)."""
    num = den = 0.0
    for i in range(start, min(start + count, len(series))):
        c = series[i]
        rng = c["H"] - c["L"]
        if rng <= 0:
            continue
        v = float(c.get("V", 0) or 0)
        num += (c["C"] - c["O"]) / rng * v
        den += v
    return num / den if den > 0 else 0.0


def collect_levels(direction, tf_candles):
    """Swings bajos (compras) o altos (ventas) de ENTRY_TF y LEVEL_TF."""
    levels = []
    tfs = [ENTRY_TF] if LEVEL_TF == ENTRY_TF else [ENTRY_TF, LEVEL_TF]
    for tf in tfs:
        hs, ht, ls, lt = get_swings(tf_candles.get(tf, []), max_swings=LEVELS_PER_TF)
        if direction > 0:
            levels.extend(zip(ls, lt))
        else:
            levels.extend(zip(hs, ht))
    return levels


def detect_trigger(direction, bar, atr, tf_candles):
    rng = bar["H"] - bar["L"]
    if rng <= 0 or rng < atr * MIN_RANGE_ATR:
        return None

    body_hi = max(bar["O"], bar["C"])
    body_lo = min(bar["O"], bar["C"])
    tol = atr * LEVEL_TOL_ATR
    max_level_time = _parse_time(bar["time"]) - timedelta(minutes=MIN_BARS_FROM_LEVEL * TF_MINUTES[ENTRY_TF])

    if direction > 0:
        if (body_lo - bar["L"]) / rng < MIN_WICK_RATIO:
            return None
        if REQUIRE_COLOR and bar["C"] < bar["O"]:
            return None
    else:
        if (bar["H"] - body_hi) / rng < MIN_WICK_RATIO:
            return None
        if REQUIRE_COLOR and bar["C"] > bar["O"]:
            return None

    for level, t in collect_levels(direction, tf_candles):
        if _parse_time(t) > max_level_time:
            continue  # nivel demasiado reciente
        if direction > 0:
            if bar["C"] <= level:
                continue  # debe cerrar de vuelta por encima del nivel
            if USE_SWEEP and bar["L"] < level - tol and (level - bar["L"]) <= atr * MAX_SWEEP_ATR:
                return {"dir": 1, "kind": "SWEEP_LOW", "level": level, "extreme": bar["L"]}
            if USE_DOUBLE_TB and abs(bar["L"] - level) <= tol:
                return {"dir": 1, "kind": "DOUBLE_BOTTOM", "level": level, "extreme": min(bar["L"], level)}
        else:
            if bar["C"] >= level:
                continue  # debe cerrar de vuelta por debajo del nivel
            if USE_SWEEP and bar["H"] > level + tol and (bar["H"] - level) <= atr * MAX_SWEEP_ATR:
                return {"dir": -1, "kind": "SWEEP_HIGH", "level": level, "extreme": bar["H"]}
            if USE_DOUBLE_TB and abs(bar["H"] - level) <= tol:
                return {"dir": -1, "kind": "DOUBLE_TOP", "level": level, "extreme": max(bar["H"], level)}
    return None


def confluence_score(direction, series, bias_state):
    """series: velas cerradas del ENTRY_TF en orden serie (0 = gatillo)."""
    score = 0

    # 1) pico de volumen en la vela gatillo (volumen de frenado / absorcion)
    vols = [float(series[i].get("V", 0) or 0) for i in range(1, min(VOL_AVG_BARS + 1, len(series)))]
    avg = sum(vols) / len(vols) if vols else 0.0
    vol_spike = VOL_MULT <= 0 or (avg > 0 and float(series[0].get("V", 0) or 0) >= avg * VOL_MULT)
    if vol_spike:
        score += 1

    # 2) giro de presion: ventana rapida a favor vs ventana anterior
    fast = pressure(series, 0, SENT_FAST_BARS)
    slow = pressure(series, SENT_FAST_BARS, SENT_SLOW_BARS)
    shift = fast > slow if direction > 0 else fast < slow
    if shift:
        score += 1

    # 3) sesgo de H4 no en contra
    htf_bias = bias_state["bias"] if bias_state else 0
    htf_ok = htf_bias != -1 if direction > 0 else htf_bias != 1
    if htf_ok:
        score += 1

    detail = f"vol={'Y' if vol_spike else 'N'} press={fast:.2f}/{slow:.2f} htf={htf_bias}"
    return score, detail


# =========================================================================
# Punto de entrada
# =========================================================================

def _today_utc(now_utc):
    return now_utc.date()


def register_published(sig, now_utc=None):
    """Llamar despues de publicar la señal con exito (limite de 2/dia)."""
    global _trades_today, _trades_day
    now_utc = now_utc or datetime.now(timezone.utc)
    if _trades_day != _today_utc(now_utc):
        _trades_day = _today_utc(now_utc)
        _trades_today = 0
    _trades_today += 1


def evaluate(tf_candles, now_utc=None, stateless=False, log=print):
    """
    tf_candles: {"M1": [...], "M5": [...], "M15": [...], "H1": [...], "H4": [...]}
                velas de to_candles() en orden cronologico.
    now_utc:    hora actual (en backtest, el cierre de la vela M5 evaluada).
    stateless:  True en backtest (no usa el control de vela/dia en memoria).

    Devuelve un dict de señal compatible con publish_signal() o None.
    """
    global _last_bar_time, _trades_today, _trades_day
    now_utc = now_utc or datetime.now(timezone.utc)
    tag = f"  [8] {STRATEGY_NAME}"

    entry_closed = closed_bars(tf_candles.get(ENTRY_TF, []), ENTRY_TF, now_utc)
    need = max(VOL_AVG_BARS, SENT_FAST_BARS + SENT_SLOW_BARS) + 1
    if len(entry_closed) < max(need, ATR_PERIOD + 1):
        log(f"{tag}: sin velas suficientes de {ENTRY_TF}")
        return None

    trigger_bar = entry_closed[-1]

    # Solo una evaluacion por vela cerrada (el EA trabaja 1 vez por vela).
    if not stateless:
        # Si Supabase todavia no tiene la vela nueva, la ultima cerrada puede
        # estar guardada a medias (data_engine la subio mientras se formaba).
        # Se espera a que aparezca la siguiente vela para evaluarla completa.
        if tf_candles[ENTRY_TF][-1]["time"] == trigger_bar["time"]:
            log(f"{tag}: esperando que data_engine suba la vela {ENTRY_TF} nueva")
            return None
        if _trades_day != _today_utc(now_utc):
            _trades_day = _today_utc(now_utc)
            _trades_today = 0
        if trigger_bar["time"] == _last_bar_time:
            log(f"{tag}: vela {trigger_bar['time']} ya evaluada")
            return None
        _last_bar_time = trigger_bar["time"]
        if _trades_today >= MAX_TRADES_PER_DAY:
            log(f"{tag}: limite de {MAX_TRADES_PER_DAY} operaciones hoy")
            return None

    if not in_session(now_utc):
        log(f"{tag}: fuera de sesion ({START_HOUR}-{END_HOUR}h servidor)")
        return None

    atr = calc_atr(entry_closed)
    if atr <= 0:
        return None

    states = {tf: build_state(tf_candles.get(tf, [])) for tf in STRUCTURE_TFS}
    bias_state = states.get(BIAS_TF) if BIAS_TF in states else build_state(tf_candles.get(BIAS_TF, []))

    series = list(reversed(entry_closed[-need:]))
    # Precio actual: cierre de la vela en formacion si existe, si no el de la gatillo.
    raw_entry = tf_candles.get(ENTRY_TF, [])
    price = raw_entry[-1]["C"] if raw_entry else trigger_bar["C"]
    zone = atr * HH_ZONE_ATR

    estructura = " ".join(f"{tf}:{structure_labels(states[tf])}" for tf in STRUCTURE_TFS)

    for direction in (1, -1):
        sig = detect_trigger(direction, trigger_bar, atr, tf_candles)
        if not sig:
            continue
        side = "BUY" if direction > 0 else "SELL"

        # REGLA DURA: no comprar en HH confirmado en varios TF, no vender en LL
        if direction > 0:
            c, lvl = count_confirmed_hh(states, price, zone)
            if c >= MIN_TF_CONFIRM:
                log(f"{tag}: BLOCKED BUY ({sig['kind']}): precio en HH {lvl:.2f} confirmado en {c} TFs")
                continue
        else:
            c, lvl = count_confirmed_ll(states, price, zone)
            if c >= MIN_TF_CONFIRM:
                log(f"{tag}: BLOCKED SELL ({sig['kind']}): precio en LL {lvl:.2f} confirmado en {c} TFs")
                continue

        score, detail = confluence_score(direction, series, bias_state)
        if score < MIN_SCORE:
            log(f"{tag}: {side} {sig['kind']} descartado — score {score} < {MIN_SCORE} ({detail})")
            continue

        entry = price
        sl = sig["extreme"] - atr * SL_BUFFER_ATR if direction > 0 else sig["extreme"] + atr * SL_BUFFER_ATR
        dist = abs(entry - sl)
        if dist <= 0 or (direction > 0 and sl >= entry) or (direction < 0 and sl <= entry):
            log(f"{tag}: {side} {sig['kind']} descartado — precio ya paso el SL")
            continue
        if dist > atr * MAX_SL_ATR:
            log(f"{tag}: SKIP {sig['kind']}: SL {dist:.2f} mayor a {MAX_SL_ATR} ATR")
            continue

        tp = entry + dist * RR if direction > 0 else entry - dist * RR
        return {
            "signal_type":   side,
            "entry_price":   entry,
            "stop_loss":     sl,
            "take_profit_1": tp,
            "take_profit_2": tp,  # el EA usa un solo TP (RR x riesgo)
            "confidence":    CONFIDENCE_BY_SCORE.get(score, 76),
            "strategy":      STRATEGY_NAME,
            "timeframe":     ENTRY_TF,
            "atr":           atr,
            "reasons":       [sig["kind"], f"nivel {sig['level']:.2f}", detail, f"score {score}/3", estructura],
            "candle_time":   trigger_bar["time"],
        }

    log(f"{tag}: sin setup | {estructura}")
    return None


# =========================================================================
# Tamaño de posicion (CalcLots del EA) — lo usa bot_engine.py
# =========================================================================

def calc_lots(mt5, symbol, side, entry, sl):
    """
    Devuelve (lotes, detalle) o (None, motivo).
    Riesgo RISK_PCT % del equity; si ni el lote minimo cabe, usa el minimo
    solo si arriesga <= MAX_RISK_PCT_MIN_LOT %, si no SALTA el trade.
    Margen del trade <= MAX_MARGIN_USE_PCT % del margen libre.
    """
    acct = mt5.account_info()
    info = mt5.symbol_info(symbol)
    if acct is None or info is None:
        return None, "sin account_info / symbol_info"
    equity = acct.equity
    if equity <= 0:
        return None, f"equity invalido: {equity}"

    order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
    loss_per_lot = 0.0
    pnl = mt5.order_calc_profit(order_type, symbol, 1.0, entry, sl)
    if pnl is not None and pnl < 0:
        loss_per_lot = -pnl
    elif info.trade_tick_size and info.trade_tick_value:
        loss_per_lot = abs(entry - sl) / info.trade_tick_size * info.trade_tick_value
    if loss_per_lot <= 0:
        return None, "no se pudo calcular la perdida por lote"

    min_lot, max_lot = info.volume_min, info.volume_max
    step = info.volume_step or min_lot
    risk_money = equity * RISK_PCT / 100.0
    lots = int(risk_money / loss_per_lot / step + 1e-9) * step

    if lots < min_lot:
        min_lot_risk_pct = min_lot * loss_per_lot / equity * 100.0
        if min_lot_risk_pct > MAX_RISK_PCT_MIN_LOT:
            return None, (f"SKIP: lote minimo {min_lot:.2f} arriesgaria {min_lot_risk_pct:.1f}% del equity "
                          f"(limite {MAX_RISK_PCT_MIN_LOT:.1f}%). SL muy ancho para esta cuenta.")
        lots = min_lot
    lots = round(min(lots, max_lot), 2)

    margin = mt5.order_calc_margin(order_type, symbol, lots, entry)
    if margin is None:
        return None, "order_calc_margin fallo"
    if margin > acct.margin_free * MAX_MARGIN_USE_PCT / 100.0:
        return None, f"SKIP: margen {margin:.2f} > {MAX_MARGIN_USE_PCT:.0f}% del margen libre {acct.margin_free:.2f}"

    riesgo = lots * loss_per_lot
    return lots, {"equity": round(equity, 2), "riesgo_dinero": round(riesgo, 2),
                  "riesgo_pct": round(riesgo / equity * 100.0, 2)}
