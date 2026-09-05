import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { Line, LineChart, ResponsiveContainer, YAxis } from "recharts";
import { api } from "./api";

type Item = {
  symbol?: string; ticker?: string; price?: number; current_price?: number;
  personal_delta_pct?: number | null; anomaly_zscore?: number | null; volume_spike_ratio?: number | null;
  previous_session_close?: number | null; previous_session_delta_pct?: number | null;
  flagged?: boolean; is_stale?: boolean; data_age_seconds?: number | null;
  source?: string; winning_source?: string; source_conflict?: boolean; conflict?: boolean; conflict_detected?: boolean;
  source_winner?: string; single_sourced?: boolean; simulated?: boolean;
  conflict_other_source?: string | null; conflict_other_price_inr?: number | null;
  recent_prices?: number[]; prices?: number[];
};

type Stats = { unique_symbols: number; users: number };

const numeric = (value: unknown) => typeof value === "number" && Number.isFinite(value) ? value : null;
const pct = (value: unknown) => numeric(value) === null ? "—" : `${numeric(value)!.toFixed(2)}%`;
const ratio = (value: unknown) => numeric(value) === null ? "—" : `${numeric(value)!.toFixed(2)}×`;
const sigma = (value: unknown) => numeric(value) === null ? "—" : `${numeric(value)!.toFixed(2)}σ`;
const inr = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", minimumFractionDigits: 2, maximumFractionDigits: 2 });
const money = (value: unknown) => numeric(value) === null ? "—" : inr.format(numeric(value)!);
const ageText = (value: unknown) => numeric(value) === null ? "age unavailable" : `${Math.round(numeric(value)!)}s old`;

function normalize(payload: unknown): Item[] {
  if (Array.isArray(payload)) return payload as Item[];
  if (payload && typeof payload === "object") {
    const record = payload as Record<string, unknown>;
    for (const key of ["items", "watchlist", "symbols", "data"]) if (Array.isArray(record[key])) return record[key] as Item[];
  }
  return [];
}

function HistoryChart({ values }: { values: number[] }) {
  const points = values.filter((v) => Number.isFinite(v));
  if (points.length < 2) return <p className="no-history">Not enough history yet — the poller is collecting it.</p>;
  const data = points.map((price, index) => ({ index, price }));
  const up = points.at(-1)! >= points[0];
  return <div className="history-chart">
    <ResponsiveContainer width="100%" height={130}>
      <LineChart data={data} margin={{ top: 8, right: 8, bottom: 4, left: 8 }}>
        <YAxis domain={["auto", "auto"]} hide />
        <Line type="monotone" dataKey="price" stroke={up ? "#24734f" : "#b64a3a"} strokeWidth={1.7} dot={false} isAnimationActive={false} />
      </LineChart>
    </ResponsiveContainer>
  </div>;
}

function Sparkline({ values }: { values: number[] }) {
  const points = values.filter((v) => Number.isFinite(v));
  if (points.length < 2) return <span className="no-spark">awaiting history</span>;
  const low = Math.min(...points), high = Math.max(...points), range = high - low || 1;
  const d = points.map((v, index) => `${(index / (points.length - 1)) * 100},${35 - ((v - low) / range) * 30}`).join(" ");
  const up = points.at(-1)! >= points[0];
  return <svg className={`spark ${up ? "up" : "down"}`} viewBox="0 0 100 40" preserveAspectRatio="none" aria-label="Recent price trend"><polyline points={d} /></svg>;
}

function Row({ item, expanded, onToggle, onDelete }: { item: Item; expanded: boolean; onToggle: () => void; onDelete: () => void }) {
  const symbol = item.symbol ?? item.ticker ?? "UNKNOWN";
  const price = item.current_price ?? item.price;
  const source = item.source_winner ?? item.winning_source ?? item.source;
  const history = item.recent_prices ?? item.prices ?? [];
  const stale = Boolean(item.is_stale);
  const simulated = Boolean(item.simulated);
  const status = simulated ? "simulated · market closed" : item.flagged ? "needs attention" : stale ? `data ${ageText(item.data_age_seconds)}` : "stable";
  return <article className={`row ${item.flagged ? "flagged" : ""} ${expanded ? "expanded" : ""}`}>
    <button className="row-main" onClick={onToggle} aria-expanded={expanded} aria-controls={`detail-${symbol}`}>
      <span className="symbol"><strong>{symbol}</strong><small className={stale && !item.flagged ? "stale-note" : ""}>{status}</small></span>
      <span className="price">{money(price)}</span>
      <span className="spark-wrap"><Sparkline values={history} /></span>
      <span className="badges">{item.flagged && <i className="flag">FLAGGED</i>}{simulated && <i className="sim">SIMULATED</i>}{stale && <i className="stale">STALE</i>}</span>
      <span className="chevron" aria-hidden="true">{expanded ? "−" : "+"}</span>
    </button>
    {expanded && <div className="details" id={`detail-${symbol}`}>
      <dl><div><dt>Since you looked</dt><dd className={(numeric(item.personal_delta_pct) ?? 0) < 0 ? "negative" : "positive"}>{pct(item.personal_delta_pct)}</dd></div><div><dt>Price anomaly</dt><dd>{sigma(item.anomaly_zscore)}</dd></div><div><dt>Volume vs. usual</dt><dd>{ratio(item.volume_spike_ratio)}</dd></div><div><dt>Previous session close</dt><dd>{money(item.previous_session_close)} <small className={(numeric(item.previous_session_delta_pct) ?? 0) < 0 ? "negative" : "positive"}>{pct(item.previous_session_delta_pct)}</small></dd></div></dl>
      <HistoryChart values={history} />
      <div className="provenance">
        {simulated ? <span className="sim-note">simulated data — this market is closed; a random walk from the last real close keeps the signals alive</span> : <span>Data: {source || "unknown source"}{item.single_sourced ? " (single source)" : ""}</span>}
        {!simulated && <span>{ageText(item.data_age_seconds)}</span>}
        {!simulated && item.conflict_detected && item.conflict_other_source
          ? <span className="conflict">sources disagreed: {source} {money(price)} vs {item.conflict_other_source} {money(item.conflict_other_price_inr)} — {source} was fresher</span>
          : null}
        {!simulated && item.single_sourced && source ? <span>single source this cycle</span> : null}
        <button onClick={onDelete} className="remove">Remove</button>
      </div>
    </div>}
  </article>;
}

function LoginScreen({ users, onPick }: { users: string[]; onPick: (user: string) => void }) {
  return <main className="login">
    <header><p className="eyebrow">Market attention ledger</p><h1>Who is<br /><em>checking in?</em></h1>
      <p className="lede">Pick an account to open its ledger. State persists per user — what you marked seen stays seen.</p></header>
    <section className="user-picker" aria-label="Accounts">
      {users.length
        ? users.map((user) => <button key={user} onClick={() => onPick(user)}><strong>{user}</strong><small>open ledger →</small></button>)
        : <p className="lede">Loading accounts…</p>}
    </section>
    <footer>Demo accounts by design — nothing here needs a password, and each ledger is independent.</footer>
  </main>;
}

function WelcomeScreen({ user, items, loading, onEnter }: { user: string; items: Item[]; loading: boolean; onEnter: () => void }) {
  const changed = items.filter((i) => Math.abs(numeric(i.personal_delta_pct) ?? 0) > 0.01);
  const flagged = items.filter((i) => i.flagged).length;
  const movers = [...changed].sort((a, b) => Math.abs(numeric(b.personal_delta_pct)!) - Math.abs(numeric(a.personal_delta_pct)!)).slice(0, 4);
  const anySimulated = items.some((i) => i.simulated);
  return <main className="login">
    <header><p className="eyebrow">Welcome back, {user}</p><h1>While you<br /><em>were away…</em></h1>
      <p className="lede">{loading ? "Checking your ledger…"
        : items.length ? `${flagged ? `${flagged} of ${items.length} watched symbols need attention. ` : "Nothing is flagged. "}${changed.length ? `${changed.length} moved since your last visit.` : "No price moved since your last visit."}`
        : "Your ledger is empty — add a ticker to start noticing change."}</p></header>
    {loading ? null : <section className="user-picker" aria-label="Changes since your last visit">
      {movers.length
        ? movers.map((item) => <button key={item.symbol ?? item.ticker} onClick={onEnter}><strong>{item.symbol ?? item.ticker}</strong><small>{pct(item.personal_delta_pct)} since you looked · open row →</small></button>)
        : items.length ? <p className="lede">Your ledger is exactly as you left it.</p> : null}
    </section>}
    {anySimulated && <p className="lede sim-note">Some markets are asleep — rows marked SIMULATED are generated from the last real close, not live prices.</p>}
    <footer><button onClick={onEnter} disabled={loading}>Open the full ledger →</button></footer>
  </main>;
}

export default function App() {
  const [user, setUser] = useState<string | null>(() => localStorage.getItem("watch-ledger-user"));
  const [welcome, setWelcome] = useState(false);
  const [accounts, setAccounts] = useState<string[]>([]);
  const [symbol, setSymbol] = useState("");
  const [items, setItems] = useState<Item[]>([]), [expanded, setExpanded] = useState<string | null>(null), [loading, setLoading] = useState(true), [error, setError] = useState("");
  const [busy, setBusy] = useState(false), [updated, setUpdated] = useState<Date | null>(null), [stats, setStats] = useState<Stats | null>(null);

  useEffect(() => {
    if (user) return;
    api.users().then((res) => setAccounts(res.users)).catch(() => setAccounts([]));
  }, [user]);

  const refresh = useCallback(async (quiet = false) => {
    if (!user) return;
    if (!quiet) setLoading(true);
    try {
      const [watchlist, statResult] = await Promise.all([api.watchlist(user), api.stats().catch(() => null)]);
      setItems(normalize(watchlist));
      if (statResult) setStats(statResult);
      setError(""); setUpdated(new Date());
    } catch (e) { setError(e instanceof Error ? e.message : "Unable to reach the market service."); }
    finally { if (!quiet) setLoading(false); }
  }, [user]);

  useEffect(() => {
    if (!user) return;
    void refresh();
    const id = window.setInterval(() => void refresh(true), 10_000);
    return () => window.clearInterval(id);
  }, [refresh, user]);

  const sorted = useMemo(() => items.map((item, index) => ({ item, index })).sort((a, b) => Number(Boolean(b.item.flagged)) - Number(Boolean(a.item.flagged)) || a.index - b.index), [items]);
  const digest = useMemo(() => {
    const withDelta = items.filter((i) => numeric(i.personal_delta_pct) !== null);
    const flagged = items.filter((i) => i.flagged).length;
    if (!withDelta.length) return null;
    const biggest = withDelta.reduce((a, b) => Math.abs(numeric(b.personal_delta_pct)!) > Math.abs(numeric(a.personal_delta_pct)!) ? b : a);
    const move = numeric(biggest.personal_delta_pct)!;
    return `${flagged ? `${flagged} of ${items.length} need attention · ` : ""}biggest move since you left: ${biggest.symbol ?? biggest.ticker} ${move > 0 ? "+" : ""}${move.toFixed(2)}%`;
  }, [items]);
  const anySimulated = items.some((i) => i.simulated);
  const login = (next: string) => { localStorage.setItem("watch-ledger-user", next); setExpanded(null); setItems([]); setUser(next); setWelcome(true); };
  const logout = () => { localStorage.removeItem("watch-ledger-user"); setUser(null); setWelcome(false); setItems([]); setExpanded(null); };
  const action = async (fn: () => Promise<unknown>) => { setBusy(true); try { await fn(); await refresh(true); } catch (e) { setError(e instanceof Error ? e.message : "Action failed."); } finally { setBusy(false); } };
  const addTicker = (e: FormEvent) => { e.preventDefault(); const next = symbol.trim().toUpperCase(); if (next) void action(() => api.addSymbol(user!, next)); setSymbol(""); };

  if (!user) return <LoginScreen users={accounts} onPick={login} />;
  if (welcome) return <WelcomeScreen user={user} items={items} loading={loading} onEnter={() => setWelcome(false)} />;

  return <main><header>
    <p className="eyebrow">Market attention ledger</p>
    <h1>What changed<br /><em>while you were away?</em></h1>
    <p className="lede">Signals are personal: change since <b>{user}</b> last checked, unusual price movement, and unusual volume. All prices in INR.</p>
  </header>
    <section className="toolbar">
      <form onSubmit={(e) => { e.preventDefault(); void logout(); }}><label>Signed in as<input value={user} readOnly aria-label="Active user" /><button>Switch</button></label></form>
      <form onSubmit={addTicker}><label>Add ticker<input value={symbol} onChange={(e) => setSymbol(e.target.value)} placeholder="e.g. RELIANCE" maxLength={12} aria-label="Stock symbol" /></label><button disabled={busy}>Add</button></form>
      <div className="actions"><button onClick={() => void action(() => api.markSeen(user))} disabled={busy || !items.length}>Mark all seen</button><button onClick={() => void action(api.tick)} disabled={busy}>Refresh prices</button></div>
    </section>
    <section className="ledger" aria-live="polite"><div className="ledger-head"><span>{items.length} watched</span>{digest && <span className="digest">{digest}</span>}<span>{updated ? `updated ${updated.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "connecting…"}</span></div>{loading ? <div className="state">Opening your ledger…</div> : error ? <div className="state error"><strong>Market service unavailable.</strong><span>{error}</span><button onClick={() => void refresh()}>Try again</button></div> : !items.length ? <div className="state"><strong>Your watchlist is empty.</strong><span>Add a ticker to start noticing change, not just prices.</span></div> : <div className="rows">{sorted.map(({ item, index }) => { const key = item.symbol ?? item.ticker ?? String(index); return <Row key={key} item={item} expanded={expanded === key} onToggle={() => setExpanded(expanded === key ? null : key)} onDelete={() => void action(() => api.removeSymbol(user!, key))} />; })}</div>}</section>
    <footer>{anySimulated && <span className="sim-note">Market asleep: rows marked SIMULATED are generated from the last real close so the change signals keep running — not live prices. </span>}<span className="dot" /> {stats ? `${stats.unique_symbols} unique symbols tracked across ${stats.users} users — ingestion is deduplicated per symbol, not per viewer. ` : ""}Refreshes every 10 seconds · flag = a personal move &gt;5%, price anomaly &gt;2σ, or volume &gt;2×</footer>
  </main>;
}
