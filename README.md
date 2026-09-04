# Signal Watchlist

An attention-first market watchlist built for Groww Code 2026. It answers **what changed while I was away?** rather than presenting an undifferentiated price list.

## What it does

- Persists username-scoped watchlists in SQLite.
- Ingests a distinct symbol once every 15 seconds, not once per browser session.
- Shows three independent, explainable signals: change since last seen, price anomaly z-score, and volume versus its rolling average.
- Flags a symbol when any signal crosses its named threshold; it never hides the reason inside a blended score.
- Keeps the latest 100 snapshots per symbol and returns the latest 20 for an inline sparkline.
- Makes data age, source, and a resolved multi-source conflict visible in the UI.
- Runs with no API key through a deterministic synthetic provider, including price-jump and volume-spike demo scenarios.

## Run it

Open two PowerShell windows from this project root.

```powershell
cd backend
.\.venv\Scripts\python -m uvicorn app.main:app --reload
```

```powershell
cd frontend
npm run dev
```

Visit the Vite URL printed in the second terminal (normally `http://localhost:5173`). Add a ticker, select **Mark all seen**, then select **Simulate activity** a few times to make the signals and sparkline evolve.

To use optional live OHLCV data from Twelve Data in the backend terminal:

```powershell
$env:TWELVE_DATA_API_KEY="your-key"
.\.venv\Scripts\python -m uvicorn app.main:app --reload
```

The application still works without this key. The winning source and conflicts are exposed in each watchlist response.

## Verification

```powershell
cd backend
.\.venv\Scripts\python -m pytest tests -q

cd ..\frontend
npm run build
```

## Submission trade-offs

**Polling instead of streaming.** A 15-second backend poller and 10-second UI refresh give predictable, debuggable behaviour in a 72-hour build. The provider interface and cached snapshots isolate ingestion so WebSockets can replace polling later without changing the API or UI. Symbols are deduplicated across users before ingestion.

**Resilience without pretending.** The synthetic source is deliberately a deterministic demo/resilience provider, not falsely described as live market data. Twelve Data is an optional OHLCV provider. When both report, the freshest provider timestamp wins, a fixed source-priority rule breaks ties, and the result retains provenance. Failure leaves the latest persisted data visible and eventually marked stale.

**No AI feature.** Every alert is deterministic and auditable. The UI shows the exact signal that caused the flag, avoiding opaque or investment-advice-like recommendations.
