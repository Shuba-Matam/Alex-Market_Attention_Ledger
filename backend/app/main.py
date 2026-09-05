import asyncio
import os
from contextlib import asynccontextmanager, suppress
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .db import connection, init_db
from .providers import YahooChartProvider
from .service import (
    add_symbol,
    initialize_seen_price,
    list_users,
    mark_seen,
    remove_symbol,
    seed_users,
    stats,
    tick,
    watchlist,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_users()
    interval = max(1, int(os.getenv("POLL_INTERVAL_SECONDS", "15")))
    stop = asyncio.Event()

    async def poller() -> None:
        while not stop.is_set():
            # tick is deliberately synchronous because the providers use urllib.
            # It runs off-loop so requests remain responsive while data is fetched.
            try:
                await asyncio.to_thread(tick)
            except Exception:
                # Existing snapshots remain visible and become stale; the next poll retries.
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                continue

    worker = asyncio.create_task(poller())
    try:
        yield
    finally:
        stop.set()
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


def _has_any_data(symbol: str) -> bool:
    """True when a live source returns a quote, or any snapshot already exists for the symbol.
    This prevents persisting typos like NVDIA when no source can price them."""
    if YahooChartProvider().quote(symbol) is not None:
        return True
    with connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM price_snapshots WHERE symbol=? LIMIT 1", (symbol,)
        ).fetchone()
        return row is not None


app = FastAPI(title="Alex Watchlist API", version="2.0.0", lifespan=lifespan)
_allowed_origins = [origin.strip() for origin in os.getenv(
    "ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if origin.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_allowed_origins,
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


class SymbolBody(BaseModel):
    symbol: str = Field(min_length=1, max_length=15)


class PollBody(BaseModel):
    symbol: Optional[str] = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/users")
def get_users():
    """Seeded dummy accounts for the login screen."""
    return {"users": list_users()}


@app.get("/stats")
def get_stats():
    return stats()


@app.get("/watchlist/{username}")
def get_watchlist(username: str):
    return {"username": username, "items": watchlist(username)}


@app.post("/watchlist/{username}/symbols", status_code=201)
def post_symbol(username: str, body: SymbolBody):
    try:
        symbol = add_symbol(username, body.symbol)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    # Reject unknown tickers only when no live or historical data can be obtained.
    if not _has_any_data(symbol):
        remove_symbol(username, symbol)
        raise HTTPException(
            status_code=422,
            detail=f"'{symbol}' is not a recognised ticker on Yahoo Finance or Twelve Data",
        )
    # A newly added symbol should be meaningful immediately, rather than waiting
    # up to one polling interval for the background worker.
    tick(symbol)
    initialize_seen_price(username, symbol)
    return {"symbol": symbol}


@app.delete("/watchlist/{username}/symbols/{symbol}")
def delete_symbol(username: str, symbol: str):
    try:
        if not remove_symbol(username, symbol):
            raise HTTPException(status_code=404, detail="symbol is not in this watchlist")
        return {"deleted": True}
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/watchlist/{username}/seen")
def post_seen(username: str):
    return {"marked": mark_seen(username)}


@app.post("/poll")
def post_poll(body: PollBody):
    """Trigger an immediate poll (all watchlisted symbols, or one) instead of waiting
    for the background worker's next cycle."""
    try:
        return {"updates": tick(body.symbol)}
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
