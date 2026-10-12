"""
position_sizing.py
===================
Calcula el lote correcto segun balance real, % de riesgo y distancia
del stop loss — reemplaza el lote fijo (LOT_SIZE = 0.02) de bot_engine.py.

FIX 2026-10-12:
- Tope de apalancamiento nocional (MAX_NOTIONAL_LEVERAGE, default 30:1).
  Con SL estrechos el nocional se disparaba a 100:1+ en silencio.
- Validación de risk_percent en (0, 5] para atrapar errores de unidades.
- Regla dura: si el lote mínimo del broker implica más de 1.5x el riesgo
  configurado, se rechaza la señal en vez de solo avisar.
"""

import math
import os

MAX_NOTIONAL_LEVERAGE = float(os.getenv("MAX_NOTIONAL_LEVERAGE", "30.0"))
MAX_RISK_MULTIPLIER_VS_CONFIG = 1.5


def calculate_lot_size(mt5, symbol, entry_price, stop_loss, balance, risk_percent):
    """
    Devuelve (lote_final, detalle) o (None, razon_del_error).

    mt5            -> el modulo MetaTrader5 ya importado (import MetaTrader5 as mt5)
    symbol         -> string, ej "GOLD" (MT5_SYMBOL)
    entry_price    -> precio de entrada de la señal
    stop_loss      -> precio de stop loss de la señal (ya con el ajuste anti-hunt si aplica)
    balance        -> balance de la cuenta (mt5.account_info().balance,
                       o .equity si prefieres protegerte mas en drawdown)
    risk_percent   -> ej 0.5 para arriesgar 0.5% del balance por operacion
    """

    # --- Validación de entrada (FIX 2026-10-12) ---
    try:
        risk_percent = float(risk_percent)
    except (TypeError, ValueError):
        return None, "risk_percent inválido (no numérico)"
    if not (0 < risk_percent <= 5.0):
        return None, f"risk_percent fuera de rango (0, 5]: {risk_percent}"
    if balance is None or balance <= 0:
        return None, f"balance inválido: {balance}"

    info = mt5.symbol_info(symbol)
    if info is None:
        return None, f"symbol_info no encontrado para {symbol}"

    tick_value = info.trade_tick_value
    tick_size = info.trade_tick_size
    vol_min = info.volume_min
    vol_max = info.volume_max
    vol_step = info.volume_step
    contract_size = getattr(info, "trade_contract_size", 100.0)

    if not tick_value or not tick_size or tick_size == 0:
        return None, "trade_tick_value o trade_tick_size invalido (revisa symbol_info)"

    distancia_stop = abs(entry_price - stop_loss)
    if distancia_stop <= 0:
        return None, "Distancia de stop loss invalida (entry == sl o SL al lado equivocado)"

    riesgo_dinero = balance * (risk_percent / 100)

    # Valor monetario de la distancia del stop loss, para 1.0 lote
    valor_por_punto_1_lote = tick_value / tick_size
    perdida_si_toca_sl_1_lote = distancia_stop * valor_por_punto_1_lote

    if perdida_si_toca_sl_1_lote <= 0:
        return None, "Perdida calculada en 0 — revisa entry/sl/tick_value"

    lote_ideal = riesgo_dinero / perdida_si_toca_sl_1_lote

    # Redondear al step del broker (hacia abajo, para no pasarte del riesgo)
    lote_ajustado = math.floor(lote_ideal / vol_step) * vol_step
    lote_ajustado = round(lote_ajustado, 2)

    # --- Tope de apalancamiento nocional (FIX 2026-10-12) ---
    precio_ref = entry_price if entry_price > 0 else 0

    def _leverage_of(lote):
        nocional = lote * contract_size * precio_ref
        return (nocional / balance if balance > 0 else float("inf")), nocional

    lev_ideal, noc_ideal = _leverage_of(lote_ajustado)

    detalle = {
        "balance": round(balance, 2),
        "risk_percent": risk_percent,
        "riesgo_dinero": round(riesgo_dinero, 2),
        "distancia_stop_pts": round(distancia_stop, 2),
        "lote_ideal": round(lote_ideal, 4),
        "nocional_estimado": round(noc_ideal, 2),
        "apalancamiento_nocional": round(lev_ideal, 1),
        "lote_min_broker": vol_min,
        "lote_max_broker": vol_max,
        "lote_step_broker": vol_step,
    }

    if lev_ideal > MAX_NOTIONAL_LEVERAGE:
        lote_tope = (MAX_NOTIONAL_LEVERAGE * balance) / (contract_size * precio_ref) if precio_ref > 0 else 0
        lote_tope = math.floor(lote_tope / vol_step) * vol_step
        lote_tope = round(max(lote_tope, 0), 2)
        detalle["bloqueado_por_apalancamiento"] = True
        detalle["apalancamiento_maximo"] = MAX_NOTIONAL_LEVERAGE
        if lote_tope < vol_min:
            return None, (
                f"Señal rechazada: para respetar el tope de apalancamiento "
                f"{MAX_NOTIONAL_LEVERAGE}:1 el lote sería {lote_tope}, bajo el mínimo "
                f"del broker ({vol_min}). Riesgo original implicaba {lev_ideal:.1f}:1."
            )
        detalle["lote_final"] = lote_tope
        detalle["aviso"] = (
            f"Lote reducido de {lote_ajustado} a {lote_tope} por tope de "
            f"apalancamiento {MAX_NOTIONAL_LEVERAGE}:1 (original: {lev_ideal:.1f}:1)."
        )
        return lote_tope, detalle

    if lote_ajustado < vol_min:
        riesgo_real = vol_min * perdida_si_toca_sl_1_lote
        riesgo_real_pct = riesgo_real / balance * 100
        # Regla dura en vez de solo aviso
        if riesgo_real_pct > risk_percent * MAX_RISK_MULTIPLIER_VS_CONFIG:
            return None, (
                f"Señal omitida: el lote mínimo del broker ({vol_min}) implica "
                f"riesgo real de {riesgo_real_pct:.2f}% vs configurado {risk_percent}% "
                f"({riesgo_real_pct / risk_percent:.1f}x)."
            )
        detalle["aviso"] = (
            f"Lote ideal ({lote_ideal:.4f}) es menor al minimo del broker ({vol_min}). "
            f"Se usara el lote minimo. Riesgo real: ${riesgo_real:.2f} "
            f"({riesgo_real_pct:.2f}% del balance, no {risk_percent}%)."
        )
        detalle["lote_final"] = vol_min
        return vol_min, detalle

    if lote_ajustado > vol_max:
        detalle["aviso"] = f"Lote ideal excede el maximo del broker ({vol_max}). Se usara el maximo."
        detalle["lote_final"] = vol_max
        return vol_max, detalle

    detalle["lote_final"] = lote_ajustado
    return lote_ajustado, detalle
