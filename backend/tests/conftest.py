"""Autouse fixture: the lifespan poller now seeds and polls watchlists at startup,
so every test needs the provider boundary mocked before TestClient starts.
Individual tests override these with their own scenario-specific mocks."""
import os
import tempfile
import time
from pathlib import Path

import pytest

from app.providers import Quote


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    from app import providers
    monkeypatch.setattr(providers.YahooChartProvider, "quote",
                        lambda self, symbol: Quote(symbol=symbol, price=1420.0, volume=1_000_000.0,
                                                   timestamp=int(time.time()), source="yahoo",
                                                   currency="INR", previous_close=1400.0))
    monkeypatch.setattr(providers.FxRateProvider, "get_usd_to_inr", lambda self, force=False: 90.0)
    # The startup poller now backfills history for seeded symbols; keep tests off the wire.
    monkeypatch.setattr(providers.YahooChartProvider, "history", lambda self, symbol, **kwargs: [])


@pytest.fixture(autouse=True)
def clear_login_throttle():
    """Sign-in counters are process-global; isolate every test from the last."""
    from app import auth
    auth.reset_auth_failures()
    yield
    auth.reset_auth_failures()


class AuthedClient:
    """Wraps TestClient so each test acts as one authenticated account.

    Every watchlist endpoint now derives identity from the bearer token, so
    tests must sign in rather than naming a user in the path. `as_new_user`
    and `as_demo` return independent sessions for isolation assertions.
    """

    def __init__(self, client):
        self._client = client
        self._token = None
        self.username = None
        self.user_id = None

    def __getattr__(self, name):
        return getattr(self._client, name)

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def _use(self, session: dict) -> dict:
        self._token = session["token"]
        self.username = session["username"]
        self.user_id = session["user_id"]
        return session

    def as_new_user(self, email="alice@example.com", password="correct horse battery") -> dict:
        response = self._client.post("/auth/register", json={"email": email, "password": password})
        assert response.status_code == 201, response.text
        return self._use(response.json())

    def as_demo(self, username="demo") -> dict:
        response = self._client.post("/auth/demo", json={"username": username})
        assert response.status_code == 200, response.text
        return self._use(response.json())

    def get(self, url, **kwargs):
        kwargs.setdefault("headers", self.headers)
        return self._client.get(url, **kwargs)

    def post(self, url, **kwargs):
        kwargs.setdefault("headers", self.headers)
        return self._client.post(url, **kwargs)

    def delete(self, url, **kwargs):
        kwargs.setdefault("headers", self.headers)
        return self._client.delete(url, **kwargs)

    def add(self, symbol: str):
        return self.post("/watchlist/symbols", json={"symbol": symbol})

    def items(self) -> list:
        response = self.get("/watchlist")
        assert response.status_code == 200, response.text
        return response.json()["items"]


@pytest.fixture
def client(monkeypatch):
    """A TestClient on a fresh database with simulation off (auth tests etc.)."""
    directory = tempfile.TemporaryDirectory()
    path = Path(directory.name) / "test.db"
    os.environ["DATABASE_PATH"] = str(path)
    from app import db, service
    monkeypatch.setattr(db, "DATABASE_PATH", path)
    monkeypatch.setattr(service, "SIMULATION_ENABLED", False)
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as test_client:
        yield test_client
    monkeypatch.undo()


@pytest.fixture
def authed(client):
    """The `client` fixture, signed in as a fresh registered account."""
    wrapper = AuthedClient(client)
    wrapper.as_new_user()
    return wrapper
