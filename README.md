# Signal Watchlist

An attention-first market watchlist built for Groww Code 2026. It answers **what changed while I was away?** rather than presenting an undifferentiated price list.

## What it does

- Persists per-user watchlists in SQLite; login is a picker over seeded dummy accounts — the thing being demonstrated is per-user state persistence, not security.
- **Two live sources, no synthetic data**: Yahoo Finance's keyless chart endpoint is primary; Twelve Data (`/quote`, free tier, throttled to ~800 requests/day) cross-checks when an API key is set. Bare NSE tickers are retried with `.NS` automatically.
- **Everything is displayed in INR.** Quotes are stored in their native currency and converted once at the API boundary using a cached live USD/INR rate (`INR=X`, refreshed every 5 minutes — FX doesn't need 15-second freshness). NSE quotes are never double-converted.
- **Conflict handling is real and visible.** When both sources respond, both are converted to INR first, then compared. A spread beyond 0.5% is a real conflict: the fresher timestamp wins, and the expanded row shows both disagreeing values and which source was used. A lone source is marked single-sourced, not silently agreed-upon.
- Three independent, explainable signals per row: change since last seen, price anomaly z-score, and volume versus its rolling average. A row is flagged when any signal crosses its named threshold — never via a blended score.
- Expanded rows show a recharts line graph of recent price history plus staleness ("data is Xs old") and full source provenance.
- A footer stat ("N unique symbols tracked across M users") is the demoable proof that ingestion is deduplicated per symbol, not per user-per-symbol — the actual scaling lever.
- Symbols poll every 15 seconds via a background worker, deduplicated across users; the UI refreshes every 10 seconds.

## Run it locally

```powershell
cd backend
.\.venv\Scripts\python -m uvicorn app.main:app --reload
```

```powershell
cd frontend
npm run dev
```

Visit `http://localhost:5173`, pick an account, add tickers (NSE names like `RELIANCE` and US names like `AAPL` both work), select **Mark all seen**, then compare after the next poll.

Optional secondary source:

```powershell
$env:TWELVE_DATA_API_KEY="your-key"
.\.venv\Scripts\python -m uvicorn app.main:app --reload
```

The app is fully functional without the key — rows are simply single-sourced (and labelled as such).

## Deploy

- **Backend**: `render.yaml` deploys to Render's free tier (`pip install` + `uvicorn`). Two honest caveats, documented in that file: free-tier services sleep after ~15 minutes without traffic (the background poller only runs while awake), and SQLite sits on an ephemeral disk — data survives a running service's restarts but is reset on redeploy. Attach a persistent disk (paid) or Railway volumes if the demo must survive redeploys. Set `TWELVE_DATA_API_KEY` and `ALLOWED_ORIGINS` (your frontend URL) in the dashboard.
- **Frontend**: deploy `frontend/` to Vercel or Netlify (near-zero-config for Vite) and set `VITE_API_BASE` to the deployed backend URL — the API base has been an environment variable from the start (`frontend/.env.example`).

## Verification

```powershell
cd backend
.\.venv\Scripts\python -m pytest tests -q

cd ..\frontend
npm run build
```

## Submission trade-offs

**Polling instead of streaming.** A 15-second backend poller and 10-second UI refresh give predictable, debuggable behaviour in a 72-hour build. The provider interface and cached snapshots isolate ingestion so WebSockets can replace polling later without changing the API or UI. Symbols are deduplicated across users before ingestion — the footer stat makes that lever visible.

**Resilience without pretending.** The freshest provider timestamp wins conflicts, a fixed source-priority rule breaks ties, and the result retains provenance. Total source failure leaves the latest persisted data visible and eventually marked stale; single-source polls are labelled rather than passed off as verified.

**Dummy auth.** A user picker over seeded rows instead of passwords: nothing in this project needs protecting, and the point being demonstrated is per-user persistence across sessions and devices.

**No AI feature.** Every alert is deterministic and auditable. The UI shows the exact signal that caused the flag, avoiding opaque or investment-advice-like recommendations.
