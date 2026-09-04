import asyncio
import os
from contextlib import asynccontextmanager, suppress
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .db import init_db
from .service import add_symbol, mark_seen, remove_symbol, seed_demo_watchlist, tick, watchlist


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_demo_watchlist()
    interval = max(1, int(os.getenv("POLL_INTERVAL_SECONDS", "15")))
    stop = asyncio.Event()

    async def poller() -> None:
        while not stop.is_set():
            # tick is deliberately synchronous because the optional provider uses urllib.
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


app = FastAPI(title="Signal Watchlist API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


class SymbolBody(BaseModel):
    symbol: str = Field(min_length=1, max_length=15)


class TickBody(BaseModel):
    symbol: Optional[str] = None
    scenario: str = "normal"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/watchlist/{username}")
def get_watchlist(username: str):
    return {"username": username, "items": watchlist(username)}


@app.post("/watchlist/{username}/symbols", status_code=201)
def post_symbol(username: str, body: SymbolBody):
    try:
        symbol = add_symbol(username, body.symbol)
        # A newly added symbol should be meaningful immediately, rather than waiting
        # up to one polling interval for the background worker.
        tick(symbol)
        return {"symbol": symbol}
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


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


@app.post("/demo/tick")
def post_tick(body: TickBody):
    try:
        return {"updates": tick(body.symbol, body.scenario)}
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
