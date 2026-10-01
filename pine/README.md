# Indicadores Pine para scalping de XAUUSD

## 1. `TradingPro_Scalper_XAU.pine` (propio)

Indicador Pine v6 que pone en el gráfico la misma lógica que usa TradingProEA, para operar manualmente en M1/M5.

| Bloque | Qué hace | Equivalente en el bot |
|---|---|---|
| Killzones | Sombrea Londres 03:00–06:00 y NY 09:00–12:00 (hora RD) | `is_killzone()` |
| Sweep + MSS | Una mecha barre un swing o el PDH/PDL y la vela cierra de vuelta adentro; después, una vela con cuerpo ≥ 1.2×ATR rompe el swing menor | `strategy_sweep_displacement` |
| Pullback a EMA | Con EMA 9 > 21 > 200, el precio toca la EMA 21 y cierra por encima de la EMA 9 | `strategy_ema_pullback` |
| Sesgo HTF | Cierre M15 contra EMA 50 de M15 (vela cerrada) | filtro de tendencia |
| FVG | Cajas de FVG que se borran cuando se llenan | `detect_fvgs` |
| Contexto | VWAP del día con bandas, PDH/PDL, squeeze BB/Keltner, panel | `calc_session_vwap`, `get_pdh_pdl` |

Cada señal dibuja la entrada, el SL (detrás del extremo barrido + 0.30) y el TP con R:R 2. Se descarta si el SL pasa de 3×ATR.

**No repinta:** las señales se confirman al cierre de la vela y los datos de M15 y del día salen de velas ya cerradas.

### Instalación
1. En TradingView abre el **Pine Editor**, pega el archivo y dale a **Add to chart**.
2. Usa un gráfico de XAUUSD con volumen (por ejemplo OANDA:XAUUSD o el feed de tu broker). Si el símbolo no tiene volumen, el VWAP no se dibuja.
3. Alertas: **Create alert → TP Scalper → "Any alert() function call"** y recibes la entrada, el SL y el TP en el mensaje.

## 2. Indicadores públicos recomendados (gratis, código abierto)

| # | Indicador | Para qué sirve en scalping de oro | Cuidado con |
|---|---|---|---|
| 1 | Smart Money Concepts [LuxAlgo] | BOS/CHoCH, order blocks, equal highs/lows, premium/discount | Muy cargado; desactiva lo que no uses |
| 2 | ICT Killzones + Pivots [TFO] | Sesiones y sus máximos y mínimos hasta que se rompen (liquidez) | Ajustar zona horaria |
| 3 | Fair Value Gap [LuxAlgo] | Zonas de desequilibrio para entrar en el retroceso | En M1 salen demasiados FVG; filtrar por tamaño |
| 4 | VWAP integrado de TradingView (con bandas) | Precio justo intradía; reversión hacia el VWAP o rebote en él | Requiere un feed con volumen |
| 5 | Squeeze Momentum [LazyBear] | Detecta compresión antes de una expansión | Indica cuándo, no hacia dónde |
| 6 | UT Bot Alerts | Trailing stop por ATR; sirve para gestionar la salida | Usado solo da muchas señales falsas en rango |

**Combinación sugerida:** 1 o el TP Scalper para la estructura, 2 para la sesión, 4 para el sesgo intradía y 5 para el timing. No más de 3 o 4 indicadores en pantalla.

## Advertencias
- Ningún indicador tiene una tasa de acierto comprobada en XAUUSD M1. Antes de operar con dinero real, haz forward test o backtest y compara con `result_tracker.py`.
- Evita operar 5–15 minutos alrededor de noticias de alto impacto (CPI, NFP, FOMC): el spread y el slippage en oro se disparan.
- Los pivotes (swings) se confirman `pivotLen` velas después; es lo que evita el repintado, pero mete retraso.
