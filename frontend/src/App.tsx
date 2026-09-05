import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api";

type Item = {
  symbol?: string; ticker?: string; price?: number; current_price?: number;
  personal_delta_pct?: number | null; anomaly_zscore?: number | null; volume_spike_ratio?: number | null;
  previous_session_close?: number | null; previous_session_delta_pct?: number | null;
  flagged?: boolean; is_stale?: boolean; data_age_seconds?: number | null;
  source?: string; winning_source?: string; source_conflict?: boolean; conflict?: boolean; conflict_detected?: boolean;
  recent_prices?: number[]; prices?: number[];
};

const defaultUser = localStorage.getItem("watch-ledger-user") || "demo";
const numeric = (value: unknown) => typeof value === "number" && Number.isFinite(value) ? value : null;
const pct = (value: unknown) => numeric(value) === null ? "—" : `${numeric(value)!.toFixed(2)}%`;
const ratio = (value: unknown) => numeric(value) === null ? "—" : `${numeric(value)!.toFixed(2)}×`;
const inr = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", minimumFractionDigits: 2, maximumFractionDigits: 2 });
const money = (value: unknown) => numeric(value) === null ? "—" : inr.format(numeric(value)!);

function normalize(payload: unknown): Item[] {
  if (Array.isArray(payload)) return payload as Item[];
  if (payload && typeof payload === "object") {
    const record = payload as Record<string, unknown>;
    for (const key of ["items", "watchlist", "symbols", "data"]) if (Array.isArray(record[key])) return record[key] as Item[];
  }
  return [];
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
  const source = item.winning_source ?? item.source;
  const history = item.recent_prices ?? item.prices ?? [];
  const stale = item.is_stale;
  const conflict = item.source_conflict ?? item.conflict ?? item.conflict_detected;
  return <article className={`row ${item.flagged ? "flagged" : ""} ${expanded ? "expanded" : ""}`}>
    <button className="row-main" onClick={onToggle} aria-expanded={expanded} aria-controls={`detail-${symbol}`}>
      <span className="symbol"><strong>{symbol}</strong><small>{item.flagged ? "needs attention" : "stable"}</small></span>
      <span className="price">{money(price)}</span>
      <span className="spark-wrap"><Sparkline values={history} /></span>
      <span className="badges">{item.flagged && <i className="flag">FLAGGED</i>}{stale && <i className="stale">STALE</i>}</span>
      <span className="chevron" aria-hidden="true">{expanded ? "−" : "+"}</span>
    </button>
    {expanded && <div className="details" id={`detail-${symbol}`}>
      <dl><div><dt>Since you looked</dt><dd className={(numeric(item.personal_delta_pct) ?? 0) < 0 ? "negative" : "positive"}>{pct(item.personal_delta_pct)}</dd></div><div><dt>Price anomaly</dt><dd>{numeric(item.anomaly_zscore) === null ? "—" : `${numeric(item.anomaly_zscore)!.toFixed(2)}σ`}</dd></div><div><dt>Volume vs. usual</dt><dd>{ratio(item.volume_spike_ratio)}</dd></div><div><dt>Previous session close</dt><dd>{money(item.previous_session_close)} <small className={(numeric(item.previous_session_delta_pct) ?? 0) < 0 ? "negative" : "positive"}>{pct(item.previous_session_delta_pct)}</small></dd></div></dl>
      <div className="provenance"><span>Data: {source || "unknown source"}</span><span>{numeric(item.data_age_seconds) === null ? "age unavailable" : `${Math.round(item.data_age_seconds!)}s old`}</span>{conflict && <span className="conflict">source conflict resolved</span>}<button onClick={onDelete} className="remove">Remove</button></div>
    </div>}
  </article>;
}

export default function App() {
  const [username, setUsername] = useState(defaultUser), [draftUser, setDraftUser] = useState(defaultUser), [symbol, setSymbol] = useState("");
  const [items, setItems] = useState<Item[]>([]), [expanded, setExpanded] = useState<string | null>(null), [loading, setLoading] = useState(true), [error, setError] = useState("");
  const [busy, setBusy] = useState(false), [updated, setUpdated] = useState<Date | null>(null);
  const refresh = useCallback(async (quiet = false) => { if (!quiet) setLoading(true); try { setItems(normalize(await api.watchlist(username))); setError(""); setUpdated(new Date()); } catch (e) { setError(e instanceof Error ? e.message : "Unable to reach the market service."); } finally { if (!quiet) setLoading(false); } }, [username]);
  useEffect(() => { void refresh(); const id = window.setInterval(() => void refresh(true), 10_000); return () => window.clearInterval(id); }, [refresh]);
  const sorted = useMemo(() => items.map((item, index) => ({ item, index })).sort((a, b) => Number(Boolean(b.item.flagged)) - Number(Boolean(a.item.flagged)) || a.index - b.index), [items]);
  const changeUser = (e: FormEvent) => { e.preventDefault(); const next = draftUser.trim(); if (!next) return; localStorage.setItem("watch-ledger-user", next); setUsername(next); setExpanded(null); };
  const action = async (fn: () => Promise<unknown>) => { setBusy(true); try { await fn(); await refresh(true); } catch (e) { setError(e instanceof Error ? e.message : "Action failed."); } finally { setBusy(false); } };
  return <main><header><p className="eyebrow">Market attention ledger</p><h1>What changed<br /><em>while you were away?</em></h1><p className="lede">Signals are personal: change since <b>{username}</b> last checked, unusual price movement, and unusual volume.</p>{username === "demo" && <p className="lede"><small>Showing a bundled demo dataset. Set a live provider key to replace it with live OHLCV data.</small></p>}</header>
    <section className="toolbar"><form onSubmit={changeUser}><label>Viewing as<input value={draftUser} onChange={(e) => setDraftUser(e.target.value)} aria-label="Username" /></label><button>Switch</button></form><form onSubmit={(e) => { e.preventDefault(); const next = symbol.trim().toUpperCase(); if (next) void action(() => api.addSymbol(username, next)); setSymbol(""); }}><label>Add ticker<input value={symbol} onChange={(e) => setSymbol(e.target.value)} placeholder="e.g. RELIANCE" maxLength={12} aria-label="Stock symbol" /></label><button disabled={busy}>Add</button></form><div className="actions"><button onClick={() => void action(() => api.markSeen(username))} disabled={busy || !items.length}>Mark all seen</button><button onClick={() => void action(api.tick)} disabled={busy}>Simulate activity</button></div></section>
    <section className="ledger" aria-live="polite"><div className="ledger-head"><span>{items.length} watched</span><span>{updated ? `updated ${updated.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "connecting…"}</span></div>{loading ? <div className="state">Opening your ledger…</div> : error ? <div className="state error"><strong>Market service unavailable.</strong><span>{error}</span><button onClick={() => void refresh()}>Try again</button></div> : !items.length ? <div className="state"><strong>Your watchlist is empty.</strong><span>Add a ticker to start noticing change, not just prices.</span></div> : <div className="rows">{sorted.map(({ item, index }) => { const key = item.symbol ?? item.ticker ?? String(index); return <Row key={key} item={item} expanded={expanded === key} onToggle={() => setExpanded(expanded === key ? null : key)} onDelete={() => void action(() => api.removeSymbol(username, key))} />; })}</div>}</section>
    <footer>Refreshes every 10 seconds · <span className="dot" /> flag = a personal move &gt;5%, price anomaly &gt;2σ, or volume &gt;2×</footer>
  </main>;
}
