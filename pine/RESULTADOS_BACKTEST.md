# Backtest TradingPro Scalper XAU — velas reales del broker (Supabase)

**Datos:** XAUUSD de XM, del 15-jun al 1-oct-2026 (15,085 velas M5 y 68,513 M1). Faltan unos 28 días de julio.
**Reglas:** las del Pine con sus valores por defecto. Entrada en la vela siguiente a la señal, costo de 0.34 USD/oz por operación y límites del bot de 3 operaciones y 2 pérdidas por día.
**Unidad:** R (1R = 100 USD con 1 % de riesgo sobre 10,000 USD).

| Configuración | Trades | % ganadoras | Profit Factor | Neto (R) | Máx. DD (R) |
|---|---|---|---|---|---|
| M5 ambas señales (por defecto) | 99 | 34.3 | 0.97 | -2.4 | -12.3 |
| M5 solo Pullback | 97 | 34.0 | 0.95 | -3.3 | -13.2 |
| M5 solo Sweep | 4 | 0.0 | 0.00 | -4.1 | -3.1 |
| M5 RR 1.0 | 110 | 55.5 | 1.12 | +6.1 | -9.8 |
| M5 RR 1.5 | 103 | 43.7 | 1.06 | +4.0 | -16.8 |
| M5 sin killzone | 147 | 33.3 | 0.92 | -8.7 | -17.9 |
| M5 sin costos (referencia) | 99 | 34.3 | 1.04 | +2.9 | -11.1 |
| M1 ambas señales | 138 | 36.2 | 0.96 | -3.9 | -10.7 |
| M1 RR 1.0 | 146 | 50.7 | 0.81 | -15.6 | -19.1 |
| M1 sin costos (referencia) | 138 | 36.2 | 1.13 | +11.7 | -9.0 |

**Por mes (M5):** con RR 1.0 agosto da -1.6R y septiembre +3.8R. Con RR 2.0, -0.4R y +3.1R.

## Conclusión
- Con los valores por defecto, la estrategia **no tiene ventaja comprobada**: queda alrededor de cero después de costos.
- La señal Sweep + MSS casi no se activa (4 operaciones en M5) y perdió. La muestra es insuficiente para concluir.
- En M1 el spread se come la ventaja: sin costos el Profit Factor sería 1.13, con costos baja a 0.96.
- M5 con RR 1.0 da un Profit Factor de 1.12, pero agosto fue negativo. Con ~110 operaciones en 2.5 meses no es una ventaja confirmada.

Para repetir la prueba: `python pine/backtest_scalper_xau.py M5 2026-06-01`
