"""
backtest_scalper_xau.py
=======================
Backtest en Python de TradingPro_Scalper_XAU_Strategy.pine usando las velas reales
del broker guardadas en Supabase (tabla ohlc_candles, hora UTC).
Sirve cuando no se puede usar el Strategy Tester de TradingView.

Uso:
    python pine/backtest_scalper_xau.py                 # M5, desde 2026-06-01
    python pine/backtest_scalper_xau.py M1 2026-08-01

Reglas del port (iguales al Pine):
- Señal al cierre de la vela, entrada en la apertura de la siguiente.
- SL/TP fijos; si SL y TP caen en la misma vela de M5 se resuelve con M1
  (si no hay M1, se asume SL).
- Costo por operación: 0.34 USD/oz (spread 0.30 + slippage 0.02 x 2).
- Resultados en R (1R = riesgo de la operación). Con 1% de riesgo y 10,000 USD, 1R = 100 USD.
"""
import os
import sys
import requests
from dotenv import load_dotenv
import numpy as np
import pandas as pd

P = dict(emaF=9, emaS=21, emaT=200, htf='15min', htfEma=50, useBias=True,
         kz=((300, 600), (900, 1200)), onlyKZ=True,
         useSweep=True, usePull=True, pivotLen=5, minorLook=8, mssWindow=15,
         allowLong=True, allowShort=True,
         atrLen=14, slBuffer=0.30, rr=2.0, minDispAtr=1.2, maxSlAtr=3.0,
         maxTrades=3, maxLosses=2, cost=0.34)   # cost: spread 0.30 + slippage 0.02x2 por onza

def ema(s, n): return s.ewm(span=n, adjust=False).mean()
def rma(s, n): return s.ewm(alpha=1.0/n, adjust=False).mean()

def htf_prev(df, rule, fn, offset=None):
    """Valor de la vela HTF ANTERIOR ya cerrada (equivale a expr[1] con lookahead_on)."""
    key = (df.index + (offset or pd.Timedelta(0))).floor(rule) if rule != 'D' else None
    g = df.groupby(key)
    h = pd.DataFrame({'o': g.o.first(), 'h': g.h.max(), 'l': g.l.min(), 'c': g.c.last()})
    vals = fn(h).shift(1)
    return vals.reindex(key).set_axis(df.index)

def run(df, m1=None, **kw):
    p = {**P, **kw}
    o, h, l, c = (df[k].to_numpy() for k in 'ohlc')
    n = len(df)
    emaF, emaS, emaT = (ema(df.c, p[k]).to_numpy() for k in ('emaF', 'emaS', 'emaT'))
    tr = pd.concat([df.h - df.l, (df.h - df.c.shift()).abs(), (df.l - df.c.shift()).abs()], axis=1).max(axis=1)
    atr = rma(tr, p['atrLen']).to_numpy()
    rd = df.index - pd.Timedelta(hours=4)                      # hora RD
    hhmm = (rd.hour * 100 + rd.minute).to_numpy()
    inKZ = np.zeros(n, bool)
    for a, b in p['kz']: inKZ |= (hhmm >= a) & (hhmm < b)
    kzOk = inKZ if p['onlyKZ'] else np.ones(n, bool)
    rdDay = rd.normalize().to_numpy()
    # sesgo HTF
    bias = htf_prev(df, p['htf'], lambda x: pd.DataFrame({'c': x.c, 'e': ema(x.c, p['htfEma'])}))
    bu = (bias.c > bias.e).to_numpy(); bd = (bias.c < bias.e).to_numpy()
    longOk = p['allowLong'] & (bu if p['useBias'] else True)
    shortOk = p['allowShort'] & (bd if p['useBias'] else True)
    longOk = np.broadcast_to(longOk, n); shortOk = np.broadcast_to(shortOk, n)
    # PDH/PDL: día del broker (cierre 21:00 UTC en verano)
    bday = (df.index + pd.Timedelta(hours=3)).floor('D')
    g = df.groupby(bday)
    dh = g.h.max().shift(1); dl = g.l.min().shift(1)
    pdh = dh.reindex(bday).to_numpy(); pdl = dl.reindex(bday).to_numpy()
    L = p['pivotLen']; ml = p['minorLook']
    minorLow = df.l.rolling(ml).min().shift(1).to_numpy()
    minorHigh = df.h.rolling(ml).max().shift(1).to_numpy()
    lo5 = df.l.rolling(5).min().to_numpy(); hi5 = df.h.rolling(5).max().to_numpy()
    body = np.abs(c - o)

    lastSH = lastSL = np.nan
    bear = dict(bar=None, ext=np.nan, lvl=np.nan, mss=np.nan)
    bull = dict(bar=None, ext=np.nan, lvl=np.nan, mss=np.nan)
    pullBuyPrev = pullSellPrev = False
    tradesToday = lossesToday = 0; prevDay = None
    pos = None; pending = None; trades = []

    def exit_check(i):
        nonlocal pos
        side, sl, tp = pos['side'], pos['sl'], pos['tp']
        hitSL = l[i] <= sl if side > 0 else h[i] >= sl
        hitTP = h[i] >= tp if side > 0 else l[i] <= tp
        if not (hitSL or hitTP): return None
        if hitSL and hitTP and m1 is not None:                 # resolver con M1
            sub = m1.loc[df.index[i]: df.index[i] + (df.index[i+1]-df.index[i] if i+1 < n else pd.Timedelta('5min')) - pd.Timedelta('1s')]
            for _, r in sub.iterrows():
                s_ = r.l <= sl if side > 0 else r.h >= sl
                t_ = r.h >= tp if side > 0 else r.l <= tp
                if s_: hitTP = False; break
                if t_: hitSL = False; break
        if hitSL:
            px = sl
            if (side > 0 and o[i] < sl) or (side < 0 and o[i] > sl): px = o[i]   # gap
            return px, 'SL'
        return tp, 'TP'

    for i in range(n):
        # 1) llenar entrada pendiente en la apertura
        if pending is not None:
            pos = {**pending, 'entry': o[i], 'ibar': i, 'etime': df.index[i]}
            pending = None
        # 2) salidas
        closedLoss = 0
        if pos is not None:
            r = exit_check(i)
            if r:
                px, why = r
                pnl = (px - pos['entry']) * pos['side'] - p['cost']
                R = pnl / pos['risk']
                trades.append(dict(time=pos['etime'], tag=pos['tag'], side=pos['side'], entry=pos['entry'],
                                   sl=pos['sl'], tp=pos['tp'], exit=px, why=why, R=R, bars=i - pos['ibar'] + 1))
                closedLoss = int(pnl < 0); pos = None
        # 3) contadores diarios
        if prevDay is None or rdDay[i] != prevDay:
            tradesToday = lossesToday = 0; prevDay = rdDay[i]
        lossesToday += closedLoss
        # 4) estructura
        if i >= 2 * L:
            j = i - L
            win = h[j - L:i + 1]
            if h[j] == win.max() and (win == h[j]).sum() == 1: lastSH = h[j]
            win = l[j - L:i + 1]
            if l[j] == win.min() and (win == l[j]).sum() == 1: lastSL = l[j]
        sHS = not np.isnan(lastSH) and h[i] > lastSH and c[i] < lastSH
        sHP = not np.isnan(pdh[i]) and h[i] > pdh[i] and c[i] < pdh[i]
        sLS = not np.isnan(lastSL) and l[i] < lastSL and c[i] > lastSL
        sLP = not np.isnan(pdl[i]) and l[i] < pdl[i] and c[i] > pdl[i]
        if sHS or sHP:
            bear.update(bar=i, ext=h[i], lvl=pdh[i] if sHP else lastSH, mss=minorLow[i])
            if sHS: lastSH = np.nan
        if sLS or sLP:
            bull.update(bar=i, ext=l[i], lvl=pdl[i] if sLP else lastSL, mss=minorHigh[i])
            if sLS: lastSL = np.nan
        if bear['bar'] is not None and i > bear['bar']:
            if c[i] > bear['lvl'] or i - bear['bar'] > p['mssWindow']: bear['bar'] = None
            elif h[i] > bear['ext']: bear['ext'] = h[i]
        if bull['bar'] is not None and i > bull['bar']:
            if c[i] < bull['lvl'] or i - bull['bar'] > p['mssWindow']: bull['bar'] = None
            elif l[i] < bull['ext']: bull['ext'] = l[i]
        big = body[i] >= p['minDispAtr'] * atr[i]
        sweepSell = p['useSweep'] and bear['bar'] is not None and i > bear['bar'] and c[i] < bear['mss'] and c[i] < o[i] and big and shortOk[i] and kzOk[i]
        sweepBuy = p['useSweep'] and bull['bar'] is not None and i > bull['bar'] and c[i] > bull['mss'] and c[i] > o[i] and big and longOk[i] and kzOk[i]
        tU = emaF[i] > emaS[i] > emaT[i]; tD = emaF[i] < emaS[i] < emaT[i]
        pbr = tU and l[i] <= emaS[i] and c[i] > emaF[i] and c[i] > o[i] and longOk[i] and kzOk[i]
        psr = tD and h[i] >= emaS[i] and c[i] < emaF[i] and c[i] < o[i] and shortOk[i] and kzOk[i]
        pullBuy = p['usePull'] and pbr and not pullBuyPrev
        pullSell = p['usePull'] and psr and not pullSellPrev
        pullBuyPrev, pullSellPrev = pbr, psr
        # 5) entradas (se llenan en la apertura siguiente)
        if i < 250 or i + 1 >= n: continue
        if (df.index[i+1] - df.index[i]) > pd.Timedelta('2h'): continue   # no entrar antes de un hueco
        canTrade = pos is None and tradesToday < p['maxTrades'] and lossesToday < p['maxLosses']
        slB = bull['ext'] - p['slBuffer'] if sweepBuy else lo5[i] - p['slBuffer']
        slS = bear['ext'] + p['slBuffer'] if sweepSell else hi5[i] + p['slBuffer']
        rB, rS = c[i] - slB, slS - c[i]
        goLong = canTrade and (sweepBuy or pullBuy) and 0 < rB <= p['maxSlAtr'] * atr[i]
        goShort = canTrade and not goLong and (sweepSell or pullSell) and 0 < rS <= p['maxSlAtr'] * atr[i]
        if goLong:
            pending = dict(side=1, sl=slB, tp=c[i] + p['rr'] * rB, risk=rB, tag='Sweep' if sweepBuy else 'Pullback')
            tradesToday += 1; bull['bar'] = None
        elif goShort:
            pending = dict(side=-1, sl=slS, tp=c[i] - p['rr'] * rS, risk=rS, tag='Sweep' if sweepSell else 'Pullback')
            tradesToday += 1; bear['bar'] = None
    return pd.DataFrame(trades)

def stats(t, label=''):
    if t.empty: return dict(cfg=label, trades=0)
    R = t.R
    eq = R.cumsum(); dd = (eq - eq.cummax()).min()
    gp, gl = R[R > 0].sum(), -R[R < 0].sum()
    return dict(cfg=label, trades=len(t), win=round((R > 0).mean() * 100, 1),
                PF=round(gp / gl, 2) if gl else np.inf, avgR=round(R.mean(), 3),
                netR=round(R.sum(), 1), maxDD_R=round(dd, 1),
                usd_1pct_10k=round(R.sum() * 100), dd_usd=round(dd * 100))


load_dotenv()
PAGE_SIZE = 1000


def fetch(timeframe, start_iso, instrument="XAUUSD"):
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
    hdr = {"apikey": key, "Authorization": f"Bearer {key}"}
    rows, offset = [], 0
    while True:
        r = requests.get(f"{url}/rest/v1/ohlc_candles", headers=hdr, timeout=30, params={
            "select": "candle_time,open,high,low,close", "instrument": f"eq.{instrument}",
            "timeframe": f"eq.{timeframe}", "candle_time": f"gte.{start_iso}",
            "order": "candle_time.asc", "limit": str(PAGE_SIZE), "offset": str(offset)})
        r.raise_for_status()
        chunk = r.json()
        rows.extend(chunk)
        if len(chunk) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    df = pd.DataFrame(rows).rename(columns={"open": "o", "high": "h", "low": "l", "close": "c"})
    df["time"] = pd.to_datetime(df.candle_time, utc=True)
    df = df.drop_duplicates("time").set_index("time").sort_index()
    return df[["o", "h", "l", "c"]].astype(float)


if __name__ == "__main__":
    tf = sys.argv[1] if len(sys.argv) > 1 else "M5"
    start = sys.argv[2] if len(sys.argv) > 2 else "2026-06-01"
    df = fetch(tf, start)
    m1 = fetch("M1", start) if tf == "M5" else None
    out = []
    for name, kw in [("ambas", {}), ("solo Sweep", dict(usePull=False)), ("solo Pullback", dict(useSweep=False)),
                     ("RR 1.0", dict(rr=1.0)), ("RR 1.5", dict(rr=1.5)), ("sin sesgo", dict(useBias=False)),
                     ("sin killzone", dict(onlyKZ=False))]:
        out.append(stats(run(df, m1, **kw), f"{tf} {name}"))
    print(pd.DataFrame(out).to_string(index=False))
