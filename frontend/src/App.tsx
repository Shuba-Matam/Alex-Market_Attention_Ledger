import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api, Session, TOKEN_KEY } from "./api";

const USER_KEY = "watch-ledger-user";

type Item = {
  symbol?: string; ticker?: string; price?: number; current_price?: number;
  personal_delta_pct?: number | null; anomaly_zscore?: number | null; volume_spike_ratio?: number | null;
  previous_session_close?: number | null; previous_session_delta_pct?: number | null;
  flagged?: boolean; is_stale?: boolean; data_age_seconds?: number | null;
  source?: string; winning_source?: string; source_conflict?: boolean; conflict?: boolean; conflict_detected?: boolean;
  source_winner?: string; single_sourced?: boolean; simulated?: boolean;
  conflict_other_source?: string | null; conflict_other_price_inr?: number | null;
  recent_prices?: number[]; prices?: number[]; recent_sources?: string[];
  last_seen_price?: number | null; last_seen_at?: number | null;
  history?: { t: number; price: number; source: string }[];
};

type Stats = { unique_symbols: number; users: number };

const numeric = (value: unknown) => typeof value === "number" && Number.isFinite(value) ? value : null;
const pct = (value: unknown) => numeric(value) === null ? "—" : `${numeric(value)!.toFixed(2)}%`;
const ratio = (value: unknown) => numeric(value) === null ? "—" : `${numeric(value)!.toFixed(2)}×`;
const sigma = (value: unknown) => numeric(value) === null ? "—" : `${numeric(value)!.toFixed(2)}σ`;
const inr = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", minimumFractionDigits: 2, maximumFractionDigits: 2 });
const money = (value: unknown) => numeric(value) === null ? "—" : inr.format(numeric(value)!);
const ageText = (value: unknown) => numeric(value) === null ? "age unavailable" : `${Math.round(numeric(value)!)}s old`;
const signedPct = (value: unknown) => {
  const n = numeric(value);
  return n === null ? "—" : `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;
};
const toneOf = (value: unknown) => {
  const n = numeric(value);
  return n === null ? "flat" : n > 0.005 ? "positive" : n < -0.005 ? "negative" : "flat";
};
const clockTime = (seconds: unknown) => {
  const n = numeric(seconds);
  return n === null ? null : new Date(n * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
};
/** "2 hours ago" / "just now" - the human answer to "when did I last check?". */
const sinceText = (seconds: unknown) => {
  const n = numeric(seconds);
  if (n === null) return null;
  const delta = Math.max(0, Math.floor(Date.now() / 1000 - n));
  if (delta < 60) return "just now";
  const units: [number, string][] = [[86_400 * 7, "week"], [86_400, "day"], [3_600, "hour"], [60, "minute"]];
  for (const [size, label] of units) {
    if (delta >= size) {
      const count = Math.floor(delta / size);
      return `${count} ${label}${count > 1 ? "s" : ""} ago`;
    }
  }
  return "just now";
};
const fullStamp = (seconds: unknown) => {
  const n = numeric(seconds);
  return n === null ? null : new Date(n * 1000).toLocaleString([], {
    weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  });
};
/** The most recent moment the user actually looked at this ledger. */
const lastCheckedAt = (items: Item[]) => {
  const stamps = items.map((i) => numeric(i.last_seen_at)).filter((v): v is number => v !== null);
  return stamps.length ? Math.max(...stamps) : null;
};

function normalize(payload: unknown): Item[] {
  if (Array.isArray(payload)) return payload as Item[];
  if (payload && typeof payload === "object") {
    const record = payload as Record<string, unknown>;
    for (const key of ["items", "watchlist", "symbols", "data"]) if (Array.isArray(record[key])) return record[key] as Item[];
  }
  return [];
}

type ChartPoint = { t: number; index: number; real: number | null; simulated: number | null; price: number; source: string };

function ChartTooltip({ active, payload }: { active?: boolean; payload?: { payload: ChartPoint }[] }) {
  if (!active || !payload?.length) return null;
  const point = payload[0].payload;
  const simulated = point.source === "simulated";
  return <div className="chart-tip">
    <strong>{money(point.price)}</strong>
    <span>{fullStamp(point.t) ?? "time unavailable"}</span>
    <span className={simulated ? "sim-note" : ""}>{simulated ? "simulated - market closed" : `live - ${point.source}`}</span>
  </div>;
}

function HistoryChart({ item, values, sources }: { item: Item; values: number[]; sources?: string[] }) {
  // Prefer the richer `history` payload (real timestamps per point); fall back to
  // the flat price array so an older API response still renders.
  const points: ChartPoint[] = (item.history?.length
    ? item.history.map((h, index) => ({ t: h.t, index, price: h.price, source: h.source,
                                        real: h.source !== "simulated" ? h.price : null,
                                        simulated: h.source === "simulated" ? h.price : null }))
    : values.map((price, index) => ({ t: 0, index, price, source: sources?.[index] ?? "unknown",
                                      real: sources?.[index] !== "simulated" ? price : null,
                                      simulated: sources?.[index] === "simulated" ? price : null })))
    .filter((p) => Number.isFinite(p.price));

  if (points.length < 2) return <p className="no-history">Not enough history yet - the poller is collecting it.</p>;

  const anySimulated = points.some((p) => p.source === "simulated");
  const current = points.at(-1)!.price;
  const seen = numeric(item.last_seen_price);
  // Domain spans the data *and* the baseline, so the "you saw" line is never
  // clipped outside the plot area.
  const prices = points.map((p) => p.price).concat(seen === null ? [] : [seen]);
  const low = Math.min(...prices), high = Math.max(...prices);
  const pad = (high - low || Math.abs(high) * 0.01 || 1) * 0.12;
  const move = seen !== null && seen !== 0 ? ((current - seen) / seen) * 100 : null;

  return <div className="history-chart">
    <div className="chart-head">
      <div className="chart-readout">
        <span className="chart-now"><em>Current</em><strong>{money(current)}</strong></span>
        {seen !== null && <span className="chart-seen"><em>When you last looked</em><strong>{money(seen)}</strong></span>}
        {move !== null && <span className={`chart-move ${toneOf(move)}`}>{signedPct(move)}</span>}
      </div>
      <ul className="chart-legend" aria-label="Chart legend">
        <li><i className="swatch live" />Live price</li>
        {anySimulated && <li><i className="swatch sim" />Simulated</li>}
        {seen !== null && <li><i className="swatch seen" />You last saw</li>}
      </ul>
    </div>
    <ResponsiveContainer width="100%" height={168}>
      <LineChart data={points} margin={{ top: 6, right: 14, bottom: 4, left: 6 }}>
        <CartesianGrid stroke="#e0e3d8" vertical={false} />
        <XAxis dataKey="index" hide />
        <YAxis domain={[low - pad, high + pad]} width={54} tickLine={false} axisLine={false}
               tick={{ fontSize: 10, fill: "#7a8578" }}
               tickFormatter={(v: number) => Math.round(v).toLocaleString("en-IN")} />
        <Tooltip content={<ChartTooltip />} isAnimationActive={false}
                 cursor={{ stroke: "#9aa698", strokeDasharray: "3 3" }} />
        {seen !== null && <ReferenceLine y={seen} stroke="#8a6d1f" strokeDasharray="2 3" strokeWidth={1.2}
                                         label={{ value: "you saw", position: "insideBottomLeft",
                                                  fontSize: 9, fill: "#8a6d1f" }} />}
        <Line type="monotone" dataKey="real" name="Live price" stroke="#24734f" strokeWidth={1.7}
              dot={false} activeDot={{ r: 3.2, fill: "#24734f" }} isAnimationActive={false} connectNulls={false} />
        {anySimulated && <Line type="monotone" dataKey="simulated" name="Simulated" stroke="#4a3a78"
                               strokeDasharray="5 4" strokeWidth={1.7} dot={false}
                               activeDot={{ r: 3.2, fill: "#4a3a78" }} isAnimationActive={false} connectNulls={false} />}
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
      <span className="price">
        <span className="price-now">{money(price)}</span>
        <small className={`price-delta ${toneOf(item.personal_delta_pct)}`}>
          {signedPct(item.personal_delta_pct)} <em>since you looked</em>
        </small>
      </span>
      <span className="spark-wrap"><Sparkline values={history} /></span>
      <span className="badges">{item.flagged && <i className="flag">FLAGGED</i>}{simulated && <i className="sim">SIMULATED</i>}{stale && <i className="stale">STALE</i>}</span>
      <span className="chevron" aria-hidden="true">{expanded ? "−" : "+"}</span>
    </button>
    {expanded && <div className="details" id={`detail-${symbol}`}>
      <dl><div><dt>Since you looked</dt><dd className={toneOf(item.personal_delta_pct)}>{signedPct(item.personal_delta_pct)}</dd></div><div><dt>Price when you looked</dt><dd>{money(item.last_seen_price)} <small>{sinceText(item.last_seen_at) ?? "not yet recorded"}</small></dd></div><div><dt>Price anomaly</dt><dd>{sigma(item.anomaly_zscore)}</dd></div><div><dt>Volume vs. usual</dt><dd>{ratio(item.volume_spike_ratio)}</dd></div><div><dt>Previous session close</dt><dd>{money(item.previous_session_close)} <small className={(numeric(item.previous_session_delta_pct) ?? 0) < 0 ? "negative" : "positive"}>{pct(item.previous_session_delta_pct)}</small></dd></div></dl>
      <HistoryChart item={item} values={history} sources={item.recent_sources} />
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

declare global {
  interface Window { google?: any }
}

/** Renders the official Google button once the GIS script and client ID exist. */
function GoogleButton({ clientId, onSession, onError }: { clientId: string; onSession: (s: Session) => void; onError: (m: string) => void }) {
  const holder = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    const SRC = "https://accounts.google.com/gsi/client";

    const render = () => {
      if (cancelled || !holder.current || !window.google?.accounts?.id) return;
      window.google.accounts.id.initialize({
        client_id: clientId,
        callback: async (response: { credential?: string }) => {
          if (!response?.credential) return onError("Google sign-in returned no credential.");
          try {
            onSession(await api.loginWithGoogle(response.credential));
          } catch (e) {
            onError(e instanceof Error ? e.message : "Google sign-in failed.");
          }
        },
      });
      holder.current.innerHTML = "";
      window.google.accounts.id.renderButton(holder.current, {
        theme: "outline", size: "large", text: "continue_with", shape: "rectangular", width: 260,
      });
    };

    const existing = document.querySelector<HTMLScriptElement>(`script[src="${SRC}"]`);
    if (window.google?.accounts?.id) render();
    else if (existing) existing.addEventListener("load", render);
    else {
      const script = document.createElement("script");
      script.src = SRC;
      script.async = true;
      script.defer = true;
      script.onload = render;
      script.onerror = () => !cancelled && onError("Could not load Google sign-in.");
      document.head.appendChild(script);
    }
    return () => { cancelled = true; existing?.removeEventListener("load", render); };
  }, [clientId, onSession, onError]);

  return <div className="google-btn" ref={holder} />;
}

function LoginScreen({ users, googleClientId, onDemo, onSession }: { users: string[]; googleClientId: string | null; onDemo: (user: string) => void; onSession: (s: Session) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [authError, setAuthError] = useState("");
  const [authBusy, setAuthBusy] = useState(false);

  const submit = async (mode: "login" | "register") => {
    setAuthBusy(true);
    setAuthError("");
    try {
      onSession(mode === "login" ? await api.login(email, password) : await api.register(email, password));
    } catch (e) {
      setAuthError(e instanceof Error ? e.message : "Something went wrong.");
    } finally {
      setAuthBusy(false);
    }
  };

  const showError = useCallback((message: string) => setAuthError(message), []);

  return <main className="login">
    <header><p className="eyebrow">Alex - Market Attention Ledger</p><h1>Who is<br /><em>checking in?</em></h1>
      <p className="lede">Create your own account with email or Google - your watchlist is private to you. Demo accounts are shared and open to anyone.</p></header>
    <section className="email-auth" aria-label="Sign in">
      <form onSubmit={(e) => { e.preventDefault(); void submit("login"); }}>
        <label>Email<input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" autoComplete="email" /></label>
        <label>Password<input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="8+ characters" autoComplete="current-password" minLength={8} /></label>
        <div className="auth-buttons">
          <button type="submit" disabled={authBusy || !email || !password}>Sign in</button>
          <button type="button" disabled={authBusy || !email || password.length < 8} onClick={() => void submit("register")}>Create account</button>
        </div>
      </form>
      {googleClientId && <div className="google-row"><span className="or">or</span>
        <GoogleButton clientId={googleClientId} onSession={onSession} onError={showError} /></div>}
      {authError && <p className="error">{authError}</p>}
      <small>Your account is private - only your signed-in session can open your ledger.</small>
    </section>
    <section className="user-picker" aria-label="Demo accounts">
      <p className="picker-note">Or explore a shared demo ledger (no password, open by design):</p>
      {users.length
        ? users.map((user) => <button key={user} onClick={() => onDemo(user)}><strong>{user}</strong><small>open demo ledger →</small></button>)
        : <p className="lede">Loading demo accounts…</p>}
    </section>
    <footer>Demo ledgers are shared and resettable. Create an account to keep a watchlist that is yours alone.</footer>
  </main>;
}

function WelcomeScreen({ user, items, loading, onEnter }: { user: string; items: Item[]; loading: boolean; onEnter: () => void }) {
  const changed = items.filter((i) => Math.abs(numeric(i.personal_delta_pct) ?? 0) > 0.01);
  const flagged = items.filter((i) => i.flagged).length;
  const movers = [...changed].sort((a, b) => Math.abs(numeric(b.personal_delta_pct)!) - Math.abs(numeric(a.personal_delta_pct)!)).slice(0, 4);
  const anySimulated = items.some((i) => i.simulated);
  const checked = lastCheckedAt(items);
  return <main className="login">
    <header><p className="eyebrow">Welcome back, {user}</p><h1>While you<br /><em>were away…</em></h1>
      {/* The landing moment should answer "away since when?" explicitly, not
          leave "since you last checked" as an unanchored phrase. */}
      {!loading && <p className="last-checked">
        {checked
          ? <>You last checked <strong>{sinceText(checked)}</strong><span className="stamp">{fullStamp(checked)}</span></>
          : <>This is your <strong>first visit</strong><span className="stamp">baselines start from now</span></>}
      </p>}
      <p className="lede">{loading ? "Checking your ledger…"
        : items.length ? `${flagged ? `${flagged} of ${items.length} watched symbols need attention. ` : "Nothing is flagged. "}${changed.length ? `${changed.length} moved since your last visit.` : "No price moved since your last visit."}`
        : "Your ledger is empty — add a ticker to start noticing change."}</p></header>
    {loading ? null : <section className="user-picker" aria-label="Changes since your last visit">
      {movers.length
        ? movers.map((item) => <button key={item.symbol ?? item.ticker} onClick={onEnter}>
            <strong>{item.symbol ?? item.ticker}</strong>
            <small><span className={toneOf(item.personal_delta_pct)}>{signedPct(item.personal_delta_pct)}</span> {money(item.last_seen_price)} → {money(item.current_price ?? item.price)}</small>
          </button>)
        : items.length ? <p className="lede">Your ledger is exactly as you left it.</p> : null}
    </section>}
    {anySimulated && <p className="lede sim-note">Some watched markets are still asleep — rows marked SIMULATED are generated from the last real close, not live prices. Live markets are updating normally.</p>}
    <footer><button onClick={onEnter} disabled={loading}>Open the full ledger →</button></footer>
  </main>;
}

export default function App() {
  // A session is only real if we hold a token; the username alone is a label.
  const [user, setUser] = useState<string | null>(() => (
    localStorage.getItem(TOKEN_KEY) ? localStorage.getItem(USER_KEY) : null));
  const [welcome, setWelcome] = useState(false);
  const [accounts, setAccounts] = useState<string[]>([]);
  const [googleClientId, setGoogleClientId] = useState<string | null>(null);
  const [symbol, setSymbol] = useState("");
  const [items, setItems] = useState<Item[]>([]), [expanded, setExpanded] = useState<string | null>(null), [loading, setLoading] = useState(true), [error, setError] = useState("");
  const [busy, setBusy] = useState(false), [updated, setUpdated] = useState<Date | null>(null), [stats, setStats] = useState<Stats | null>(null);

  useEffect(() => {
    if (user) return;
    api.users().then((res) => setAccounts(res.users)).catch(() => setAccounts([]));
    // The server is authoritative; a build-time ID keeps static hosting working
    // when /auth/config has not been reached yet.
    const buildTimeClientId = (import.meta.env.VITE_GOOGLE_CLIENT_ID as string | undefined) || null;
    api.authConfig()
      .then((res) => setGoogleClientId(res.google_client_id ?? buildTimeClientId))
      .catch(() => setGoogleClientId(buildTimeClientId));
  }, [user]);

  const refresh = useCallback(async (quiet = false) => {
    if (!user) return;
    if (!quiet) setLoading(true);
    try {
      const [watchlist, statResult] = await Promise.all([api.watchlist(), api.stats().catch(() => null)]);
      setItems(normalize(watchlist));
      if (statResult) setStats(statResult);
      setError(""); setUpdated(new Date());
    } catch (e) {
      const err = e as Error & { status?: number };
      if (err.status === 401) {
        // Expired or invalid session — drop back to the login screen.
        logout();
        return;
      }
      setError(err.message || "Unable to reach the market service.");
    }
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
    const simTag = biggest.simulated ? " (simulated)" : "";
    return `${flagged ? `${flagged} of ${items.length} need attention · ` : ""}biggest move since you left: ${biggest.symbol ?? biggest.ticker} ${move > 0 ? "+" : ""}${move.toFixed(2)}%${simTag}`;
  }, [items]);
  const anySimulated = items.some((i) => i.simulated);
  const checkedLabel = useMemo(() => sinceText(lastCheckedAt(items)), [items]);
  const enterWithSession = useCallback((session: Session) => {
    localStorage.setItem(TOKEN_KEY, session.token);
    localStorage.setItem(USER_KEY, session.username);
    setExpanded(null); setItems([]); setError(""); setUser(session.username); setWelcome(true);
  }, []);
  // Demo accounts are passwordless but still token-authenticated.
  const enterDemo = useCallback((username: string) => {
    api.loginDemo(username).then(enterWithSession)
      .catch((e) => setError(e instanceof Error ? e.message : "Could not open that demo ledger."));
  }, [enterWithSession]);
  const logout = () => { localStorage.removeItem(TOKEN_KEY); localStorage.removeItem(USER_KEY); setUser(null); setWelcome(false); setItems([]); setExpanded(null); };
  const action = async (fn: () => Promise<unknown>) => { setBusy(true); try { await fn(); await refresh(true); } catch (e) { setError(e instanceof Error ? e.message : "Action failed."); } finally { setBusy(false); } };
  const addTicker = (e: FormEvent) => { e.preventDefault(); const next = symbol.trim().toUpperCase(); if (next) void action(() => api.addSymbol(next)); setSymbol(""); };

  if (!user) return <LoginScreen users={accounts} googleClientId={googleClientId} onDemo={enterDemo} onSession={enterWithSession} />;
  if (welcome) return <WelcomeScreen user={user} items={items} loading={loading} onEnter={() => setWelcome(false)} />;

  return <main><header>
    <p className="eyebrow">Alex - Market Attention Ledger</p>
    <h1>What changed<br /><em>while you were away?</em></h1>
    <p className="lede">Signals are personal: change since <b>{user}</b> last checked{checkedLabel ? <> (<b>{checkedLabel}</b>)</> : null}, unusual price movement, and unusual volume. All prices in INR.</p>
  </header>
    <section className="toolbar">
      <form onSubmit={(e) => { e.preventDefault(); void logout(); }}><label>Signed in as<input value={user} readOnly aria-label="Active user" /><button>Switch</button></label></form>
      <form onSubmit={addTicker}><label>Add ticker<input value={symbol} onChange={(e) => setSymbol(e.target.value)} placeholder="e.g. RELIANCE" maxLength={12} aria-label="Stock symbol" /></label><button disabled={busy}>Add</button></form>
      <div className="actions"><button onClick={() => void action(() => api.markSeen())} disabled={busy || !items.length}>Mark all seen</button><button onClick={() => void action(api.tick)} disabled={busy}>Refresh prices</button></div>
    </section>
    <section className="ledger" aria-live="polite"><div className="ledger-head"><span>{items.length} watched</span>{digest && <span className="digest">{digest}</span>}<span>{updated ? `updated ${updated.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "connecting…"}</span></div>{loading ? <div className="state">Opening your ledger…</div> : error ? <div className="state error"><strong>Market service unavailable.</strong><span>{error}</span><button onClick={() => void refresh()}>Try again</button></div> : !items.length ? <div className="state"><strong>Your watchlist is empty.</strong><span>Add a ticker to start noticing change, not just prices.</span></div> : <div className="rows">{sorted.map(({ item, index }) => { const key = item.symbol ?? item.ticker ?? String(index); return <Row key={key} item={item} expanded={expanded === key} onToggle={() => setExpanded(expanded === key ? null : key)} onDelete={() => void action(() => api.removeSymbol(key))} />; })}</div>}</section>
    <footer>{anySimulated && <span className="sim-note">Rows marked SIMULATED belong to markets that are asleep — not live prices. </span>}<span className="dot" /> {stats ? `${stats.unique_symbols} unique symbols tracked across ${stats.users} users — ingestion is deduplicated per symbol, not per viewer. ` : ""}Refreshes every 10 seconds · flag = a personal move &gt;5%, price anomaly &gt;2σ, or volume &gt;2× · Alex - Market Attention Ledger</footer>
  </main>;
}
