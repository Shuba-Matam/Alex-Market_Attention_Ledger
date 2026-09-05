"""Autouse fixture: the lifespan poller now seeds and polls watchlists at startup,
so every test needs the provider boundary mocked before TestClient starts.
Individual tests override these with their own scenario-specific mocks."""
import time

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
