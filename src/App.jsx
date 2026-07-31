import React, { useState, useEffect, useMemo, useCallback } from "react";
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine } from "recharts";
import { RefreshCw, TrendingUp, TrendingDown, Target, Activity, Zap } from "lucide-react";

const SUPABASE_URL = "https://qilvrvnwdtpbkcfwktqs.supabase.co";
const SUPABASE_ANON_KEY =
  "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InFpbHZydm53ZHRwYmtjZndrdHFzIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODEyMjI3MDcsImV4cCI6MjA5Njc5ODcwN30.kf0jV-496fzXziaPYbsjn5obV6-JUXAXW4hRMDUM6WU";

const GOLD = "#F0B90B";
const TEAL = "#00C9A7";
const RED = "#F6465D";
const BG = "#0A0C0E";
const PANEL = "#12151A";
const LINE = "#1E232B";
const MUTED = "#6B7480";

function fmtUSD(n, forceSign = false) {
  const sign = n > 0 && forceSign ? "+" : "";
  return `${sign}$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function StatCard({ label, value, sub, icon: Icon, accent }) {
  return (
    <div
      style={{
        background: PANEL,
        border: `1px solid ${LINE}`,
        borderRadius: 10,
        padding: "18px 20px",
        flex: "1 1 200px",
        minWidth: 180,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
        <span style={{ fontSize: 12, letterSpacing: "0.06em", textTransform: "uppercase", color: MUTED, fontFamily: "Inter, sans-serif" }}>
          {label}
        </span>
        {Icon && <Icon size={15} color={accent || MUTED} strokeWidth={2} />}
      </div>
      <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 26, fontWeight: 600, color: accent || "#E8EAED", lineHeight: 1 }}>
        {value}
      </div>
      {sub && <div style={{ fontSize: 12, color: MUTED, marginTop: 6, fontFamily: "Inter, sans-serif" }}>{sub}</div>}
    </div>
  );
}

export default function TradingDashboard() {
  const [trades, setTrades] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [lastUpdate, setLastUpdate] = useState(null);

  const fetchTrades = useCallback(async () => {
    try {
      setError(null);
      const res = await fetch(
        `${SUPABASE_URL}/rest/v1/trades_ejecutados?select=*&order=close_time.asc&limit=500`,
        {
          headers: {
            apikey: SUPABASE_ANON_KEY,
            Authorization: `Bearer ${SUPABASE_ANON_KEY}`,
          },
        }
      );
      if (!res.ok) throw new Error(`Supabase respondió ${res.status}`);
      const data = await res.json();
      setTrades(data);
      setLastUpdate(new Date());
    } catch (e) {
      setError(e.message || "Error al conectar con Supabase");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchTrades();
    const id = setInterval(fetchTrades, 60000);
    return () => clearInterval(id);
  }, [fetchTrades]);

  const stats = useMemo(() => {
    if (!trades.length) return null;
    const wins = trades.filter((t) => Number(t.profit_neto) > 0);
    const losses = trades.filter((t) => Number(t.profit_neto) <= 0);
    const totalProfit = trades.reduce((s, t) => s + Number(t.profit_neto), 0);
    const grossWin = wins.reduce((s, t) => s + Number(t.profit_neto), 0);
    const grossLoss = Math.abs(losses.reduce((s, t) => s + Number(t.profit_neto), 0));
    const winRate = (wins.length / trades.length) * 100;
    const profitFactor = grossLoss === 0 ? grossWin : grossWin / grossLoss;
    const avgWin = wins.length ? grossWin / wins.length : 0;
    const avgLoss = losses.length ? grossLoss / losses.length : 0;

    let equity = 0;
    let peak = 0;
    let maxDD = 0;
    const curve = trades.map((t, i) => {
      equity += Number(t.profit_neto);
      peak = Math.max(peak, equity);
      maxDD = Math.max(maxDD, peak - equity);
      return { i, equity: Number(equity.toFixed(2)), close_time: t.close_time };
    });

    // Hoy (usando la fecha del último trade como referencia de "hoy" del bot)
    const today = new Date().toDateString();
    const todayTrades = trades.filter((t) => new Date(t.close_time).toDateString() === today);
    const todayPnl = todayTrades.reduce((s, t) => s + Number(t.profit_neto), 0);

    return { wins, losses, totalProfit, winRate, profitFactor, avgWin, avgLoss, curve, maxDD, todayPnl, todayCount: todayTrades.length };
  }, [trades]);

  return (
    <div
      style={{
        background: BG,
        minHeight: "100%",
        padding: "28px",
        fontFamily: "Inter, -apple-system, sans-serif",
        color: "#E8EAED",
      }}
    >
      <div style={{ maxWidth: 980, margin: "0 auto" }}>
        {/* Header */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 24, flexWrap: "wrap", gap: 12 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <div
              style={{
                width: 34,
                height: 34,
                borderRadius: 8,
                background: `linear-gradient(135deg, ${GOLD}, #B8860B)`,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontWeight: 700,
                fontSize: 15,
                color: "#0A0C0E",
              }}
            >
              Au
            </div>
            <div>
              <div style={{ fontSize: 17, fontWeight: 700, letterSpacing: "-0.01em" }}>TradingProEA</div>
              <div style={{ fontSize: 12, color: MUTED }}>XAUUSD · Magic 20260601 · Scalping SMC</div>
            </div>
          </div>
          <button
            onClick={fetchTrades}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 7,
              background: PANEL,
              border: `1px solid ${LINE}`,
              borderRadius: 8,
              padding: "8px 14px",
              color: MUTED,
              fontSize: 13,
              cursor: "pointer",
            }}
          >
            <RefreshCw size={13} className={loading ? "spin" : ""} />
            {lastUpdate ? `Actualizado ${lastUpdate.toLocaleTimeString()}` : "Cargando..."}
          </button>
        </div>

        {error && (
          <div style={{ background: "rgba(246,70,93,0.1)", border: `1px solid ${RED}`, color: RED, borderRadius: 8, padding: 14, marginBottom: 20, fontSize: 13 }}>
            No se pudo cargar: {error}
          </div>
        )}

        {loading && !trades.length && !error && (
          <div style={{ color: MUTED, textAlign: "center", padding: 60 }}>Cargando trades desde Supabase...</div>
        )}

        {!loading && !trades.length && !error && (
          <div style={{ color: MUTED, textAlign: "center", padding: 60, background: PANEL, borderRadius: 10, border: `1px solid ${LINE}` }}>
            Aún no hay trades sincronizados. El bot los irá agregando automáticamente.
          </div>
        )}

        {stats && (
          <>
            {/* Hero PnL */}
            <div style={{ background: PANEL, border: `1px solid ${LINE}`, borderRadius: 12, padding: "24px 28px", marginBottom: 20 }}>
              <div style={{ fontSize: 12, color: MUTED, textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 8 }}>
                P&amp;L total acumulado
              </div>
              <div
                style={{
                  fontFamily: "'JetBrains Mono', monospace",
                  fontSize: 44,
                  fontWeight: 700,
                  color: stats.totalProfit >= 0 ? TEAL : RED,
                  lineHeight: 1,
                }}
              >
                {fmtUSD(stats.totalProfit, true)}
              </div>
              <div style={{ display: "flex", gap: 18, marginTop: 10, flexWrap: "wrap" }}>
                <span style={{ fontSize: 13, color: MUTED }}>
                  Hoy: <b style={{ color: stats.todayPnl >= 0 ? TEAL : RED, fontFamily: "'JetBrains Mono', monospace" }}>{fmtUSD(stats.todayPnl, true)}</b> ({stats.todayCount} trades)
                </span>
                <span style={{ fontSize: 13, color: MUTED }}>
                  Total trades: <b style={{ color: "#E8EAED" }}>{trades.length}</b>
                </span>
              </div>

              {/* Equity curve */}
              <div style={{ height: 160, marginTop: 20, marginLeft: -10 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={stats.curve}>
                    <defs>
                      <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor={TEAL} stopOpacity={0.35} />
                        <stop offset="100%" stopColor={TEAL} stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid stroke={LINE} vertical={false} />
                    <XAxis dataKey="i" hide />
                    <YAxis hide domain={["dataMin - 5", "dataMax + 5"]} />
                    <ReferenceLine y={0} stroke={MUTED} strokeDasharray="3 3" />
                    <Tooltip
                      contentStyle={{ background: "#191D24", border: `1px solid ${LINE}`, borderRadius: 8, fontSize: 12 }}
                      labelFormatter={() => ""}
                      formatter={(v) => [fmtUSD(v, true), "Equity"]}
                    />
                    <Area type="monotone" dataKey="equity" stroke={TEAL} strokeWidth={2} fill="url(#equityFill)" />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </div>

            {/* Stat cards */}
            <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginBottom: 20 }}>
              <StatCard label="Win Rate" value={`${stats.winRate.toFixed(1)}%`} sub={`${stats.wins.length}W / ${stats.losses.length}L`} icon={Target} accent={stats.winRate >= 50 ? TEAL : RED} />
              <StatCard label="Profit Factor" value={stats.profitFactor.toFixed(2)} sub={stats.profitFactor >= 1.5 ? "Saludable" : "A vigilar"} icon={Activity} accent={stats.profitFactor >= 1 ? TEAL : RED} />
              <StatCard label="Ganancia Prom." value={fmtUSD(stats.avgWin)} icon={TrendingUp} accent={TEAL} />
              <StatCard label="Pérdida Prom." value={fmtUSD(stats.avgLoss)} icon={TrendingDown} accent={RED} />
              <StatCard label="Max Drawdown" value={fmtUSD(stats.maxDD)} icon={Zap} accent={GOLD} />
            </div>

            {/* Recent trades table */}
            <div style={{ background: PANEL, border: `1px solid ${LINE}`, borderRadius: 12, overflow: "hidden" }}>
              <div style={{ padding: "14px 20px", borderBottom: `1px solid ${LINE}`, fontSize: 13, fontWeight: 600, color: MUTED }}>
                Últimos trades
              </div>
              <div style={{ maxHeight: 320, overflowY: "auto" }}>
                {[...trades].reverse().slice(0, 25).map((t) => (
                  <div
                    key={t.id}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                      padding: "11px 20px",
                      borderBottom: `1px solid ${LINE}`,
                      fontSize: 13,
                    }}
                  >
                    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                      <span
                        style={{
                          fontSize: 10,
                          fontWeight: 700,
                          padding: "2px 7px",
                          borderRadius: 4,
                          background: t.tipo === "BUY" ? "rgba(0,201,167,0.15)" : "rgba(246,70,93,0.15)",
                          color: t.tipo === "BUY" ? TEAL : RED,
                        }}
                      >
                        {t.tipo}
                      </span>
                      <span style={{ color: MUTED }}>{t.symbol}</span>
                      <span style={{ color: MUTED, fontSize: 12 }}>
                        {new Date(t.close_time).toLocaleString("es-DO", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}
                      </span>
                    </div>
                    <span style={{ fontFamily: "'JetBrains Mono', monospace", fontWeight: 600, color: Number(t.profit_neto) >= 0 ? TEAL : RED }}>
                      {fmtUSD(Number(t.profit_neto), true)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </>
        )}
      </div>
      <style>{`
        .spin { animation: spin 1s linear infinite; }
        @keyframes spin { to { transform: rotate(360deg); } }
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-thumb { background: ${LINE}; border-radius: 3px; }
      `}</style>
    </div>
  );
}
