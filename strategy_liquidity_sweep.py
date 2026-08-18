# -*- coding: utf-8 -*-
"""
strategy_liquidity_sweep.py
============================
Estrategia: Institutional Sweep & Displacement Scalp
Adaptada para TradingProEA (XAUUSD / GOLD)

Lógica (resumen):
  1. Contexto 15m/5m: barrido (sweep) de un nivel clave de liquidez
     (mínimo/máximo de la sesión asiática, PDH/PDL, equal highs/lows).
  2. Confirmación 1m: mecha de rechazo + Market Structure Shift (MSS)
     con vela de desplazamiento (cuerpo grande) + aumento de volumen.
  3. Entrada: NO en el breakout. Se espera pullback a un FVG generado
     por la vela de desplazamiento, o al VWAP, con señal de rechazo.
  4. SL: 1-2 pips/ticks detrás del extremo del barrido.
  5. TP1 (50%): próximo swing interno u VWAP opuesto -> mover SL a BE.
     TP2 (resto): R:R mínimo 1:2 o 1:3, o siguiente nivel de liquidez.
  6. Filtros de riesgo: solo killzones Londres/NY, 0.5-1% riesgo/trade,
     stop diario a las 2 pérdidas consecutivas, blackout de noticias
     ±5 min.

INTEGRACIÓN CON TradingProEA
-----------------------------
Este archivo es AUTOCONTENIDO y sigue el mismo patrón de las otras
estrategias del proyecto (Scalping M5 SMC, Killzone Breakout, FVG Fill
M5, EMA Pullback M5): expone una función `evaluate()` que recibe datos
de mercado (velas M15/M5/M1 vía MT5) y devuelve un dict de señal con
`score` 0-100, compatible con `signal_engine.py`.

Pasos para integrar (vía GitHub web editor):
  1. Sube este archivo a la raíz del repo TradingProEA, junto a
     signal_engine.py, bot_engine.py, etc.
  2. En signal_engine.py:
        from strategy_liquidity_sweep import evaluate as eval_sweep_displacement
     y agrega su resultado a la lista de señales candidatas, igual que
     las demás estrategias (mismo formato de dict).
  3. Agrega "Liquidity_Sweep_Displacement" a ALLOWED_STRATEGIES.
  4. Ajusta las constantes de la sección CONFIG según tu bróker
     (symbol point, spread típico de GOLD, etc.).
  5. Los helpers `is_in_killzone()` y `is_news_blackout()` están escritos
     para poder reemplazarse por tus funciones ya existentes
     (rd_to_broker_time / news_engine.py) — ver notas inline.

NOTA: Este módulo NO ejecuta órdenes ni gestiona el ciclo de vida del
trade (breakeven, trailing, cierre parcial). Esa lógica ya vive en
bot_engine.py / result_tracker.py; aquí solo se generan la señal y los
niveles (entry, sl, tp1, tp2, tp1_close_pct, move_be_at_tp1).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional


# =========================================================================
# CONFIG — ajustar según symbol real (GOLD en XMGlobal) y preferencias
# =========================================================================

STRATEGY_NAME = "Liquidity_Sweep_Displacement"

SYMBOL_POINT = 0.01          # tamaño de punto para GOLD en XMGlobal (ajustar si difiere)
SL_BUFFER_POINTS = 20        # "1-2 pips" en gold suelen ser ~15-30 puntos; ajustar a gusto
MIN_DISPLACEMENT_ATR_MULT = 1.2   # la vela de desplazamiento debe superar 1.2x el ATR(14) de 1m
VOLUME_SPIKE_MULT = 1.5           # volumen de la vela de rebote vs promedio de las últimas 20 velas
SWING_LOOKBACK = 8                # velas hacia atrás para detectar swing high/low menor en 1m
FVG_LOOKBACK = 15                 # velas hacia atrás para buscar el FVG generado por el desplazamiento

RISK_PCT_PER_TRADE = 0.0075       # 0.75% (rango permitido 0.5%-1%)
TP1_CLOSE_PCT = 0.5               # cerrar 50% en TP1
MIN_RR_TP2 = 2.0                  # ratio mínimo TP2 respecto al riesgo (usa 2.0; sube a 3.0 si prefieres)

SWEEP_MAX_CONSECUTIVE_LOSSES = 2  # regla de oro: 2 pérdidas seguidas -> apagar esta estrategia por el día
NEWS_BLACKOUT_MINUTES = 5         # ±5 minutos alrededor de noticias de alto impacto
MIN_SCORE_TO_TRADE = 75           # alineado con MIN_SCORE global del proyecto

# Killzones en horario RD (UTC-4), igual convención que el resto de TradingProEA.
# Londres 08:00-11:00 UTC -> 04:00-07:00 RD | NY 13:30-16:30 UTC -> 09:30-12:30 RD
KILLZONES_RD = [
    ("04:00", "07:00"),   # Londres
    ("09:30", "12:30"),   # Nueva York
]


# =========================================================================
# ESTRUCTURAS DE DATOS
# =========================================================================

@dataclass
class Candle:
    time: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float  # tick_volume de MT5


@dataclass
class SweepSignal:
    strategy: str = STRATEGY_NAME
    direction: str = ""            # "BUY" | "SELL"
    score: int = 0
    entry: float = 0.0
    sl: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    tp1_close_pct: float = TP1_CLOSE_PCT
    move_be_at_tp1: bool = True
    risk_pct: float = RISK_PCT_PER_TRADE
    reason: str = ""
    swept_level: Optional[float] = None
    fvg_zone: Optional[tuple] = None
    valid: bool = False


# =========================================================================
# HELPERS DE CONTEXTO (15m/5m): niveles clave de liquidez
# =========================================================================

def get_asian_session_range(candles_m15: list[Candle], ref_date: dt.date) -> tuple[float, float]:
    """Rango de la sesión asiática (00:00-08:00 UTC) del día de referencia.
    Ajustar ventana horaria si tu bróker reporta en otro huso (EET/EEST)."""
    session = [c for c in candles_m15
               if c.time.date() == ref_date and 0 <= c.time.hour < 8]
    if not session:
        return None, None
    return max(c.high for c in session), min(c.low for c in session)


def get_previous_day_high_low(candles_m15: list[Candle], ref_date: dt.date) -> tuple[float, float]:
    prev_day = ref_date - dt.timedelta(days=1)
    session = [c for c in candles_m15 if c.time.date() == prev_day]
    if not session:
        return None, None
    return max(c.high for c in session), min(c.low for c in session)


def get_equal_highs_lows(candles_m5: list[Candle], tolerance_points: float = 30) -> dict:
    """Detecta equal highs / equal lows recientes (aproximación simple:
    dos máximos/mínimos dentro de `tolerance_points` en las últimas 40 velas)."""
    recent = candles_m5[-40:]
    result = {"equal_highs": None, "equal_lows": None}
    highs = sorted(recent, key=lambda c: c.high, reverse=True)[:5]
    for i in range(len(highs)):
        for j in range(i + 1, len(highs)):
            if abs(highs[i].high - highs[j].high) <= tolerance_points * SYMBOL_POINT:
                result["equal_highs"] = max(highs[i].high, highs[j].high)
                break
        if result["equal_highs"]:
            break
    lows = sorted(recent, key=lambda c: c.low)[:5]
    for i in range(len(lows)):
        for j in range(i + 1, len(lows)):
            if abs(lows[i].low - lows[j].low) <= tolerance_points * SYMBOL_POINT:
                result["equal_lows"] = min(lows[i].low, lows[j].low)
                break
        if result["equal_lows"]:
            break
    return result


def get_key_levels(candles_m15: list[Candle], candles_m5: list[Candle], now: dt.datetime) -> dict:
    """Reúne todos los niveles candidatos a ser barridos."""
    asia_high, asia_low = get_asian_session_range(candles_m15, now.date())
    pdh, pdl = get_previous_day_high_low(candles_m15, now.date())
    eq = get_equal_highs_lows(candles_m5)
    return {
        "asia_high": asia_high, "asia_low": asia_low,
        "pdh": pdh, "pdl": pdl,
        "equal_highs": eq["equal_highs"], "equal_lows": eq["equal_lows"],
    }


# =========================================================================
# CONFIRMACIÓN 1m: sweep + rechazo + MSS + volumen
# =========================================================================

def atr(candles: list[Candle], period: int = 14) -> float:
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return 0.0
    return sum(trs[-period:]) / period


def find_last_minor_swing(candles_m1: list[Candle], direction: str, lookback: int = SWING_LOOKBACK) -> Optional[float]:
    """Swing high/low menor en 1m dentro del lookback (excluye la última vela)."""
    window = candles_m1[-(lookback + 1):-1]
    if len(window) < 3:
        return None
    if direction == "BUY":
        # swing high menor = máximo local más reciente antes del impulso
        return max(c.high for c in window)
    else:
        return min(c.low for c in window)


def detect_sweep_and_mss(candles_m1: list[Candle], key_level: float, direction: str) -> dict:
    """
    direction="BUY"  -> barrido de un mínimo, se busca rebote alcista con MSS al alza.
    direction="SELL" -> barrido de un máximo, se busca rechazo bajista con MSS a la baja.
    Devuelve dict con validez y datos del rebote (vela de desplazamiento).
    """
    if len(candles_m1) < SWING_LOOKBACK + 3 or key_level is None:
        return {"valid": False}

    last = candles_m1[-1]
    avg_vol = sum(c.volume for c in candles_m1[-21:-1]) / 20 if len(candles_m1) >= 21 else last.volume
    a = atr(candles_m1, 14)
    body = abs(last.close - last.open)

    if direction == "BUY":
        # 1) barrido: la mecha toca/perfora el mínimo clave
        swept = any(c.low < key_level for c in candles_m1[-3:])
        if not swept:
            return {"valid": False}
        # 2) rechazo: la vela de barrido no cierra abajo (cierre por encima del nivel)
        sweep_candle = min(candles_m1[-3:], key=lambda c: c.low)
        rejection = sweep_candle.close > key_level
        # 3) MSS: vela de desplazamiento rompe al alza el último swing high menor
        swing = find_last_minor_swing(candles_m1, "BUY")
        mss = swing is not None and last.close > swing and last.close > last.open
        # 4) desplazamiento real (cuerpo grande) + volumen
        displacement = a > 0 and body >= MIN_DISPLACEMENT_ATR_MULT * a
        vol_spike = avg_vol > 0 and last.volume >= VOLUME_SPIKE_MULT * avg_vol

        return {
            "valid": swept and rejection and mss and displacement and vol_spike,
            "swept_extreme": sweep_candle.low,
            "displacement_candle": last,
            "checks": {"swept": swept, "rejection": rejection, "mss": mss,
                       "displacement": displacement, "vol_spike": vol_spike},
        }
    else:  # SELL
        swept = any(c.high > key_level for c in candles_m1[-3:])
        if not swept:
            return {"valid": False}
        sweep_candle = max(candles_m1[-3:], key=lambda c: c.high)
        rejection = sweep_candle.close < key_level
        swing = find_last_minor_swing(candles_m1, "SELL")
        mss = swing is not None and last.close < swing and last.close < last.open
        displacement = a > 0 and body >= MIN_DISPLACEMENT_ATR_MULT * a
        vol_spike = avg_vol > 0 and last.volume >= VOLUME_SPIKE_MULT * avg_vol

        return {
            "valid": swept and rejection and mss and displacement and vol_spike,
            "swept_extreme": sweep_candle.high,
            "displacement_candle": last,
            "checks": {"swept": swept, "rejection": rejection, "mss": mss,
                       "displacement": displacement, "vol_spike": vol_spike},
        }


# =========================================================================
# FVG (Fair Value Gap) generado por la vela de desplazamiento
# =========================================================================

def find_fvg_after_displacement(candles_m1: list[Candle], direction: str) -> Optional[tuple]:
    """Patrón de 3 velas: busca el FVG más reciente formado por el impulso.
    Bullish FVG: high(vela1) < low(vela3)  -> zona = (high1, low3)
    Bearish FVG: low(vela1)  > high(vela3) -> zona = (high3, low1)"""
    window = candles_m1[-FVG_LOOKBACK:]
    for i in range(len(window) - 3, 0, -1):
        c1, c3 = window[i - 1], window[i + 1]
        if direction == "BUY" and c1.high < c3.low:
            return (c1.high, c3.low)  # (parte baja, parte alta de la zona a comprar)
        if direction == "SELL" and c1.low > c3.high:
            return (c3.high, c1.low)
    return None


def calc_vwap(candles: list[Candle]) -> float:
    """VWAP de sesión simple usando precio típico y tick_volume como proxy de volumen."""
    num, den = 0.0, 0.0
    for c in candles:
        typical = (c.high + c.low + c.close) / 3
        num += typical * c.volume
        den += c.volume
    return num / den if den > 0 else candles[-1].close if candles else 0.0


def price_in_entry_zone(price: float, fvg_zone: Optional[tuple], vwap: float,
                         direction: str, tolerance_points: float = 15) -> bool:
    tol = tolerance_points * SYMBOL_POINT
    if fvg_zone and fvg_zone[0] - tol <= price <= fvg_zone[1] + tol:
        return True
    if abs(price - vwap) <= tol:
        return True
    return False


# =========================================================================
# FILTROS DE RIESGO / HORARIO
# =========================================================================

def is_in_killzone(now_rd: dt.datetime) -> bool:
    """Reemplazar por tu conversión rd_to_broker_time() ya existente si aplica
    a la hora local del servidor. Aquí se asume `now_rd` ya en hora RD."""
    hm = now_rd.strftime("%H:%M")
    for start, end in KILLZONES_RD:
        if start <= hm <= end:
            return True
    return False


def is_news_blackout(now: dt.datetime, news_events: list[dt.datetime],
                      buffer_minutes: int = NEWS_BLACKOUT_MINUTES) -> bool:
    """`news_events` = lista de datetimes de noticias de alto impacto
    (CPI, NFP, decisiones FED), típicamente obtenida de news_engine.py."""
    for ev in news_events:
        if abs((now - ev).total_seconds()) <= buffer_minutes * 60:
            return True
    return False


def consecutive_losses_hit(recent_results: list[str], limit: int = SWEEP_MAX_CONSECUTIVE_LOSSES) -> bool:
    """`recent_results` = lista cronológica de 'WIN'/'LOSS' de ESTA estrategia hoy.
    Se apaga la estrategia si las últimas `limit` fueron todas LOSS."""
    if len(recent_results) < limit:
        return False
    return all(r == "LOSS" for r in recent_results[-limit:])


# =========================================================================
# SCORING (0-100) — mismo estilo que las demás estrategias del proyecto
# =========================================================================

def score_signal(checks: dict, in_killzone: bool, in_entry_zone: bool, rr_tp2: float) -> int:
    score = 0
    score += 20 if checks.get("swept") else 0
    score += 20 if checks.get("rejection") else 0
    score += 20 if checks.get("mss") else 0
    score += 15 if checks.get("displacement") else 0
    score += 15 if checks.get("vol_spike") else 0
    score += 5 if in_killzone else 0
    score += 5 if in_entry_zone else 0
    if rr_tp2 < MIN_RR_TP2:
        score -= 15  # penaliza fuertemente si el TP2 no alcanza el R:R mínimo
    return max(0, min(100, score))


# =========================================================================
# FUNCIÓN PRINCIPAL — evaluate()
# =========================================================================

def evaluate(candles_m15: list[Candle], candles_m5: list[Candle], candles_m1: list[Candle],
             now: dt.datetime, now_rd: dt.datetime,
             recent_results_today: Optional[list[str]] = None,
             news_events: Optional[list[dt.datetime]] = None) -> SweepSignal:
    """
    Punto de entrada del módulo. Devuelve un SweepSignal con `valid=True`
    solo si TODAS las condiciones (contexto + confirmación + entrada +
    filtros de riesgo) se cumplen.
    """
    recent_results_today = recent_results_today or []
    news_events = news_events or []
    signal = SweepSignal()

    # --- Filtros duros de horario / noticias / rachas de pérdidas ---
    if not is_in_killzone(now_rd):
        signal.reason = "Fuera de killzone (Londres/NY)"
        return signal
    if is_news_blackout(now, news_events):
        signal.reason = "Blackout de noticias de alto impacto"
        return signal
    if consecutive_losses_hit(recent_results_today):
        signal.reason = "Daily stop: 2 pérdidas consecutivas en esta estrategia"
        return signal

    levels = get_key_levels(candles_m15, candles_m5, now)
    vwap = calc_vwap(candles_m5[-96:])  # ~8h de M5 como ventana de sesión

    # --- Intentar setup de COMPRA: barrido de mínimos ---
    for level_name in ("asia_low", "pdl", "equal_lows"):
        level = levels.get(level_name)
        if level is None:
            continue
        result = detect_sweep_and_mss(candles_m1, level, "BUY")
        if not result.get("valid"):
            continue

        fvg = find_fvg_after_displacement(candles_m1, "BUY")
        last_price = candles_m1[-1].close
        in_zone = price_in_entry_zone(last_price, fvg, vwap, "BUY")
        if not in_zone:
            continue  # esperar retroceso; no perseguir el breakout

        sl = result["swept_extreme"] - SL_BUFFER_POINTS * SYMBOL_POINT
        risk = last_price - sl
        swing_target = find_last_minor_swing(candles_m1, "SELL")  # próximo swing interno opuesto
        tp1 = swing_target if swing_target else vwap
        tp2 = last_price + risk * MIN_RR_TP2
        rr_tp2 = (tp2 - last_price) / risk if risk > 0 else 0

        sc = score_signal(result["checks"], True, in_zone, rr_tp2)
        if sc < MIN_SCORE_TO_TRADE:
            continue

        signal = SweepSignal(
            direction="BUY", score=sc, entry=last_price, sl=sl, tp1=tp1, tp2=tp2,
            reason=f"Sweep de {level_name} + MSS alcista + entrada en FVG/VWAP",
            swept_level=level, fvg_zone=fvg, valid=True,
        )
        return signal

    # --- Intentar setup de VENTA: barrido de máximos ---
    for level_name in ("asia_high", "pdh", "equal_highs"):
        level = levels.get(level_name)
        if level is None:
            continue
        result = detect_sweep_and_mss(candles_m1, level, "SELL")
        if not result.get("valid"):
            continue

        fvg = find_fvg_after_displacement(candles_m1, "SELL")
        last_price = candles_m1[-1].close
        in_zone = price_in_entry_zone(last_price, fvg, vwap, "SELL")
        if not in_zone:
            continue

        sl = result["swept_extreme"] + SL_BUFFER_POINTS * SYMBOL_POINT
        risk = sl - last_price
        swing_target = find_last_minor_swing(candles_m1, "BUY")
        tp1 = swing_target if swing_target else vwap
        tp2 = last_price - risk * MIN_RR_TP2
        rr_tp2 = (last_price - tp2) / risk if risk > 0 else 0

        sc = score_signal(result["checks"], True, in_zone, rr_tp2)
        if sc < MIN_SCORE_TO_TRADE:
            continue

        signal = SweepSignal(
            direction="SELL", score=sc, entry=last_price, sl=sl, tp1=tp1, tp2=tp2,
            reason=f"Sweep de {level_name} + MSS bajista + entrada en FVG/VWAP",
            swept_level=level, fvg_zone=fvg, valid=True,
        )
        return signal

    signal.reason = "No hay sweep + MSS + entrada válida en este ciclo"
    return signal


# =========================================================================
# EJEMPLO DE INTEGRACIÓN EN signal_engine.py (referencia, no ejecutar aquí)
# =========================================================================
"""
from strategy_liquidity_sweep import evaluate as eval_sweep_displacement, Candle

def build_candles(mt5_rates) -> list[Candle]:
    return [Candle(time=dt.datetime.fromtimestamp(r['time']),
                    open=r['open'], high=r['high'], low=r['low'],
                    close=r['close'], volume=r['tick_volume']) for r in mt5_rates]

# Dentro del loop principal de signal_engine.py:
sig = eval_sweep_displacement(
    candles_m15=build_candles(rates_m15),
    candles_m5=build_candles(rates_m5),
    candles_m1=build_candles(rates_m1),
    now=datetime.utcnow(),
    now_rd=rd_to_broker_time(datetime.utcnow(), inverse=True),  # ajustar según tu helper real
    recent_results_today=get_today_results(strategy="Liquidity_Sweep_Displacement"),
    news_events=get_high_impact_news_today(),  # desde news_engine.py
)

if sig.valid and sig.score >= MIN_SCORE:
    push_signal_to_supabase(
        strategy=sig.strategy, direction=sig.direction, score=sig.score,
        entry=sig.entry, sl=sig.sl, tp1=sig.tp1, tp2=sig.tp2,
        tp1_close_pct=sig.tp1_close_pct, move_be_at_tp1=sig.move_be_at_tp1,
        risk_pct=sig.risk_pct, reason=sig.reason,
    )
"""
