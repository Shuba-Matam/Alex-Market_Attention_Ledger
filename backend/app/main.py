import asyncio
import math
import os
from contextlib import asynccontextmanager, suppress
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import auth
from .db import connection, init_db
from .providers import YahooChartProvider
from .service import (
    AuthError,
    add_symbol,
    initialize_seen_price,
    list_users,
    login_demo,
    login_user,
    login_with_google,
    mark_seen,
    register_user,
    remove_symbol,
    seed_users,
    stats,
    tick,
    user_by_id,
    user_owns_symbol,
    user_watchlist_symbols,
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


app = FastAPI(title="Alex Watchlist API", version="3.0.0", lifespan=lifespan)
_allowed_origins = [origin.strip() for origin in os.getenv(
    "ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if origin.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_allowed_origins,
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


class SymbolBody(BaseModel):
    symbol: str = Field(min_length=1, max_length=15)


class PollBody(BaseModel):
    symbol: Optional[str] = None


class CredentialsBody(BaseModel):
    email: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=1, max_length=200)


class DemoBody(BaseModel):
    username: str = Field(min_length=1, max_length=15)


class GoogleBody(BaseModel):
    credential: str = Field(min_length=1, max_length=4096)


def current_user(authorization: Optional[str] = Header(None)) -> dict:
    """Resolve the caller from their bearer token.

    The authenticated account is the only source of identity: no endpoint takes
    a username from the path or body, so a caller cannot address another user's
    ledger at all.
    """
    user_id = auth.verify_token(auth.bearer_token(authorization))
    user = user_by_id(user_id) if user_id is not None else None
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/users")
def get_users():
    """Seeded demo accounts for the login screen. Registered accounts are not listed."""
    return {"users": list_users()}


@app.get("/auth/config")
def get_auth_config():
    """Lets the frontend show the Google button only when the server can verify it."""
    return {"google_client_id": auth.google_client_id()}


@app.post("/auth/register", status_code=201)
def post_register(body: CredentialsBody):
    try:
        return register_user(body.email, body.password)
    except AuthError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/auth/login")
def post_login(body: CredentialsBody, request: Request):
    # Throttle by client + attempted email so one noisy client cannot grind
    # through passwords, while other users behind the same IP stay unaffected.
    key = f"{request.client.host if request.client else 'unknown'}:{body.email.strip().lower()}"
    wait = auth.rate_limit_wait(key)
    if wait > 0:
        raise HTTPException(
            status_code=429,
            detail=f"Too many sign-in attempts. Try again in {math.ceil(wait / 60)} minute(s).",
            headers={"Retry-After": str(math.ceil(wait))},
        )
    try:
        session = login_user(body.email, body.password)
    except AuthError as error:
        auth.record_auth_failure(key)
        raise HTTPException(status_code=401, detail=str(error)) from error
    auth.clear_auth_failures(key)
    return session


@app.post("/auth/google")
def post_google(body: GoogleBody):
    try:
        return login_with_google(body.credential)
    except AuthError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error


@app.post("/auth/demo")
def post_demo(body: DemoBody):
    """Open a seeded demo ledger. Still issues a real token, so the demo path
    and the registered path are authorised identically downstream."""
    try:
        return login_demo(body.username)
    except AuthError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/auth/me")
def get_me(user: dict = Depends(current_user)):
    return {"username": user["username"], "is_demo": user["is_demo"]}


@app.get("/stats")
def get_stats():
    return stats()


@app.get("/watchlist")
def get_watchlist(user: dict = Depends(current_user)):
    return {"username": user["username"], "items": watchlist(user["user_id"])}


@app.post("/watchlist/symbols", status_code=201)
def post_symbol(body: SymbolBody, user: dict = Depends(current_user)):
    try:
        symbol = add_symbol(user["user_id"], body.symbol)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    # Reject unknown tickers only when no live or historical data can be obtained.
    if not _has_any_data(symbol):
        remove_symbol(user["user_id"], symbol)
        raise HTTPException(
            status_code=422,
            detail=f"'{symbol}' is not a recognised ticker on Yahoo Finance or Twelve Data",
        )
    # A newly added symbol should be meaningful immediately, rather than waiting
    # up to one polling interval for the background worker.
    tick(symbol)
    initialize_seen_price(user["user_id"], symbol)
    return {"symbol": symbol}


@app.delete("/watchlist/symbols/{symbol}")
def delete_symbol(symbol: str, user: dict = Depends(current_user)):
    try:
        if not remove_symbol(user["user_id"], symbol):
            raise HTTPException(status_code=404, detail="symbol is not in this watchlist")
        return {"deleted": True}
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/watchlist/seen")
def post_seen(user: dict = Depends(current_user)):
    return {"marked": mark_seen(user["user_id"])}


@app.post("/poll")
def post_poll(body: PollBody, user: dict = Depends(current_user)):
    """Trigger an immediate poll instead of waiting for the background worker.

    Scoped to the caller's own symbols: an unauthenticated or arbitrary-symbol
    poll would let anyone drive outbound requests to the upstream providers.
    """
    try:
        if body.symbol is None:
            return {"updates": [u for s in user_watchlist_symbols(user["user_id"])
                                for u in tick(s)]}
        if not user_owns_symbol(user["user_id"], body.symbol):
            raise HTTPException(status_code=404, detail="symbol is not in this watchlist")
        return {"updates": tick(body.symbol)}
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
