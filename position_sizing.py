"""
position_sizing.py
===================
Calcula el lote correcto segun balance real, % de riesgo y distancia
del stop loss — reemplaza el lote fijo (LOT_SIZE = 0.02) de bot_engine.py.

Por que importa con poco capital:
Un lote fijo arriesga montos distintos en cada operacion porque el SL
cambia de señal a señal (ATR variable). Con $500 de cuenta, un SL de
20 puntos con 0.02 lotes puede ser 3x mas riesgo que un SL de 6 puntos
con el mismo lote. Este modulo normaliza el riesgo: siempre arriesgas
el mismo % de tu balance, sin importar que tan ancho sea el stop.

Formula (estandar MT5):
    riesgo_dinero   = balance * (risk_percent / 100)
    valor_por_punto = (trade_tick_value / trade_tick_size) * distancia_stop
    lote            = riesgo_dinero / valor_por_punto

Todo se obtiene de mt5.symbol_info() en tiempo real — no hay que
adivinar el contract size ni el valor por punto, MT5 ya lo sabe segun
tu broker (XMGlobal) y tipo de cuenta.
"""

import math


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

    info = mt5.symbol_info(symbol)
    if info is None:
        return None, f"symbol_info no encontrado para {symbol}"

    tick_value = info.trade_tick_value
    tick_size = info.trade_tick_size
    vol_min = info.volume_min
    vol_max = info.volume_max
    vol_step = info.volume_step

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

    detalle = {
        "balance": round(balance, 2),
        "risk_percent": risk_percent,
        "riesgo_dinero": round(riesgo_dinero, 2),
        "distancia_stop_pts": round(distancia_stop, 2),
        "lote_ideal": round(lote_ideal, 4),
        "lote_min_broker": vol_min,
        "lote_max_broker": vol_max,
        "lote_step_broker": vol_step,
    }

    if lote_ajustado < vol_min:
        # El riesgo calculado es menor que el lote minimo permitido.
        # Usamos el lote minimo pero avisamos que el riesgo real sera mayor al configurado.
        riesgo_real = vol_min * perdida_si_toca_sl_1_lote
        detalle["aviso"] = (
            f"Lote ideal ({lote_ideal:.4f}) es menor al minimo del broker ({vol_min}). "
            f"Se usara el lote minimo. Riesgo real: ${riesgo_real:.2f} "
            f"({riesgo_real / balance * 100:.2f}% del balance, no {risk_percent}%)."
        )
        detalle["lote_final"] = vol_min
        return vol_min, detalle

    if lote_ajustado > vol_max:
        detalle["aviso"] = f"Lote ideal excede el maximo del broker ({vol_max}). Se usara el maximo."
        detalle["lote_final"] = vol_max
        return vol_max, detalle

    detalle["lote_final"] = lote_ajustado
    return lote_ajustado, detalle
