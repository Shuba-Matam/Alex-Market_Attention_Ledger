import os
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.providers import Quote

USD_TO_INR = 90.0


def _quote(price, timestamp, source="yahoo", currency="INR", previous_close=None):
    return Quote(symbol="RELIANCE", price=price, volume=1_000_000.0, timestamp=timestamp,
                 source=source, currency=currency, previous_close=previous_close)


def _mock_fx(monkeypatch):
    """Keep tests off the network: FX only affects non-INR quotes."""
    from app import providers
    monkeypatch.setattr(providers.FxRateProvider, "get_usd_to_inr",
                        lambda self, force=False: USD_TO_INR)


def _fresh_db(monkeypatch):
    directory = tempfile.TemporaryDirectory()
    path = Path(directory.name) / "test.db"
    os.environ["DATABASE_PATH"] = str(path)
    # DATABASE_PATH is a module constant read at import; repoint it per test.
    from app import db, service
    monkeypatch.setattr(db, "DATABASE_PATH", path)
    # Most tests model an awake market; the simulation test re-enables it explicitly.
    monkeypatch.setattr(service, "SIMULATION_ENABLED", False)
    return directory


def test_watchlist_signals_with_live_sources(monkeypatch):
    with _fresh_db(monkeypatch):
        from app.main import app
        from app import providers

        _mock_fx(monkeypatch)
        clock = iter(range(1, 1000))
        monkeypatch.setattr(providers.YahooChartProvider, "quote",
                            lambda self, symbol: _quote(1420.0, next(clock), previous_close=1400.0))
        monkeypatch.setattr(providers.TwelveDataProvider, "quote", lambda self, symbol: None)

        with TestClient(app) as client:
            assert client.get("/health").json() == {"status": "ok"}
            assert client.post("/watchlist/alice/symbols", json={"symbol": "reliance"}).status_code == 201
            assert client.post("/poll", json={"symbol": "RELIANCE"}).status_code == 200
            data = client.get("/watchlist/alice").json()["items"][0]
            assert data["symbol"] == "RELIANCE"
            # INR prices pass through unconverted; a lone source is marked single-sourced.
            assert data["price"] == 1420.0
            assert data["single_sourced"] is True
            assert data["conflict_detected"] is False
            assert data["previous_session_close"] == 1400.0
            assert client.post("/watchlist/alice/seen").json()["marked"] == 1
            monkeypatch.setattr(providers.YahooChartProvider, "quote",
                                lambda self, symbol: _quote(1420.0 * 1.10, next(clock)))
            client.post("/poll", json={"symbol": "RELIANCE"})
            data = client.get("/watchlist/alice").json()["items"][0]
            assert data["personal_delta_pct"] == 10.0
            assert data["flagged"] is True


def test_conflict_prefers_fresher_timestamp_and_records_winner(monkeypatch):
    with _fresh_db(monkeypatch):
        from app.main import app
        from app import providers

        _mock_fx(monkeypatch)
        stale_yahoo = _quote(100.0, 1000)
        fresh_td = _quote(102.0, 2000, source="twelve_data")  # 2% apart, TD fresher
        monkeypatch.setattr(providers.YahooChartProvider, "quote", lambda self, symbol: stale_yahoo)
        monkeypatch.setattr(providers.TwelveDataProvider, "quote", lambda self, symbol: fresh_td)

        with TestClient(app) as client:
            client.post("/watchlist/alice/symbols", json={"symbol": "RELIANCE"})
            updates = client.post("/poll", json={"symbol": "RELIANCE"}).json()["updates"][0]
            assert updates["conflict_detected"] is True
            assert updates["single_sourced"] is False
            assert updates["source_winner"] == "twelve_data"
            assert updates["price_inr"] == 102.0
            data = client.get("/watchlist/alice").json()["items"][0]
            assert data["source_winner"] == "twelve_data"
            # Section 4: both disagreeing values are exposed, not just the winner.
            assert data["conflict_other_source"] == "yahoo"
            assert data["conflict_other_price_inr"] == 100.0


def test_agreeing_sources_are_not_a_conflict(monkeypatch):
    with _fresh_db(monkeypatch):
        from app.main import app
        from app import providers

        _mock_fx(monkeypatch)
        monkeypatch.setattr(providers.YahooChartProvider, "quote",
                            lambda self, symbol: _quote(100.0, 2000))
        monkeypatch.setattr(providers.TwelveDataProvider, "quote",
                            lambda self, symbol: _quote(100.2, 1000, source="twelve_data"))  # 0.2% apart

        with TestClient(app) as client:
            client.post("/watchlist/alice/symbols", json={"symbol": "RELIANCE"})
            updates = client.post("/poll", json={"symbol": "RELIANCE"}).json()["updates"][0]
            assert updates["conflict_detected"] is False
            assert updates["single_sourced"] is False
            assert updates["source_winner"] == "yahoo"


def test_usd_quotes_are_converted_to_inr(monkeypatch):
    with _fresh_db(monkeypatch):
        from app.main import app
        from app import providers

        _mock_fx(monkeypatch)
        monkeypatch.setattr(providers.YahooChartProvider, "quote",
                            lambda self, symbol: _quote(200.0, 1000, currency="USD"))

        with TestClient(app) as client:
            client.post("/watchlist/alice/symbols", json={"symbol": "AAPL"})
            updates = client.post("/poll", json={"symbol": "AAPL"}).json()["updates"][0]
            assert updates["price_inr"] == 18_000.0
            assert updates["native_price"] == 200.0
            assert updates["native_currency"] == "USD"
            data = client.get("/watchlist/alice").json()["items"][0]
            assert data["price"] == 18_000.0
            assert data["price_display"] == "₹18,000.00"


def test_unknown_ticker_is_rejected(monkeypatch):
    with _fresh_db(monkeypatch):
        from app.main import app
        from app import providers

        _mock_fx(monkeypatch)
        monkeypatch.setattr(providers.YahooChartProvider, "quote", lambda self, symbol: None)

        with TestClient(app) as client:
            response = client.post("/watchlist/alice/symbols", json={"symbol": "NVDIA"})
            assert response.status_code == 422


def test_seeded_users_and_stats(monkeypatch):
    with _fresh_db(monkeypatch):
        from app.main import app

        with TestClient(app) as client:
            users = client.get("/users").json()["users"]
            assert {"demo", "aarav", "priya", "rohan"} <= set(users)
            # Seeding is idempotent.
            with TestClient(app) as again:
                assert again.get("/users").json()["users"] == users
            assert client.get("/stats").json()["users"] == len(users)

            from app import providers
            _mock_fx(monkeypatch)
            monkeypatch.setattr(providers.YahooChartProvider, "quote",
                                lambda self, symbol: _quote(1420.0, 1000))
            client.post("/watchlist/alice/symbols", json={"symbol": "RELIANCE"})
            client.post("/watchlist/bob/symbols", json={"symbol": "RELIANCE"})
            stats = client.get("/stats").json()
            # One symbol watched by two users counts once — dedup proof.
            assert stats["unique_symbols"] == 1


def test_yahoo_prefers_nse_for_dual_listed_tickers(monkeypatch):
    """INFY exists on both NSE (Infosys Ltd) and NYSE (the ADR). India-first
    means the NSE listing must win; US-only tickers still resolve."""
    from app import providers

    fetched = []

    def fake_fetch(market_symbol):
        fetched.append(market_symbol)
        if market_symbol == "INFY.NS":
            return {"meta": {"regularMarketPrice": 1500.0, "currency": "INR", "regularMarketTime": 1000}}
        if market_symbol == "INFY":
            return {"meta": {"regularMarketPrice": 11.7, "currency": "USD", "regularMarketTime": 1000}}
        if market_symbol == "AAPL":
            return {"meta": {"regularMarketPrice": 319.97, "currency": "USD", "regularMarketTime": 1000}}
        return None  # e.g. AAPL.NS does not exist

    monkeypatch.setattr(providers.YahooChartProvider, "_fetch", staticmethod(fake_fetch))
    monkeypatch.setattr(providers.YahooChartProvider, "_SUFFIX_CACHE", {})

    provider = providers.YahooChartProvider()
    nse = provider.quote("INFY")
    assert nse.currency == "INR" and nse.price == 1500.0

    usd = provider.quote("AAPL")
    assert usd.currency == "USD" and usd.price == 319.97

    # Resolution is cached: a repeat poll does not re-probe both suffixes.
    fetched.clear()
    provider.quote("INFY")
    assert fetched == ["INFY.NS"]


def test_stale_provider_timestamps_cannot_hijack_latest_snapshot(monkeypatch):
    """Regression: a listing change (e.g. INFY resolving NSE instead of the US ADR)
    produces fresh rows with an OLDER provider_timestamp. Retention and the read
    path must not let stored rows delete or hide genuinely newer fetches."""
    with _fresh_db(monkeypatch):
        from app.main import app
        from app import providers
        from app.db import connection

        _mock_fx(monkeypatch)
        monkeypatch.setattr(providers.YahooChartProvider, "quote",
                            lambda self, symbol: _quote(1130.0, 1000))
        with TestClient(app) as client:
            client.post("/watchlist/alice/symbols", json={"symbol": "RELIANCE"})
            now = int(time.time())
            with connection() as conn:
                for i in range(100):
                    conn.execute(
                        """INSERT INTO price_snapshots(symbol, price, native_currency, price_inr,
                           provider_timestamp, fetched_at, source)
                           VALUES ('RELIANCE', 11.7, 'USD', 1105.5, 99999, ?, 'yahoo')""",
                        (now - i,))
            client.post("/poll", json={"symbol": "RELIANCE"})
            data = client.get("/watchlist/alice").json()["items"][0]
            # The newest fetch wins even though its provider timestamp is older.
            assert data["price"] == 1130.0
            with connection() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) AS n FROM price_snapshots WHERE symbol='RELIANCE'").fetchone()["n"]
            assert count == 100  # cap holds, but by insertion order, not provider time


def test_market_closed_symbols_get_labelled_simulated_ticks(monkeypatch):
    """When the provider's last market event is stale (NSE closed overnight), the
    poller runs a random walk from the last real close, tagged source='simulated'
    so the UI can disclaim it. Awake symbols keep real data."""
    with _fresh_db(monkeypatch):
        from app.main import app
        from app import providers, service

        _mock_fx(monkeypatch)
        monkeypatch.setattr(service, "SIMULATION_ENABLED", True)
        with TestClient(app) as client:
            client.post("/watchlist/alice/symbols", json={"symbol": "RELIANCE"})
            # Market open: a real quote, recent timestamp.
            monkeypatch.setattr(providers.YahooChartProvider, "quote",
                                lambda self, symbol: _quote(1420.0, int(time.time()), previous_close=1400.0))
            client.post("/poll", json={"symbol": "RELIANCE"})
            data = client.get("/watchlist/alice").json()["items"][0]
            assert data["simulated"] is False and data["source"] == "yahoo"
            baseline = data["price"]
            # Market asleep: same price, hours-old market timestamp.
            monkeypatch.setattr(providers.YahooChartProvider, "quote",
                                lambda self, symbol: _quote(1420.0, int(time.time()) - 7_200))
            client.post("/poll", json={"symbol": "RELIANCE"})
            data = client.get("/watchlist/alice").json()["items"][0]
            assert data["simulated"] is True
            assert data["source"] == "simulated"
            # The walk starts from the last real close, not from thin air.
            assert 0.9 * baseline <= data["price"] <= 1.1 * baseline
            # Provenance must not claim live agreement or conflict for simulated rows.
            assert data["conflict_detected"] is False
            assert data["single_sourced"] is False


def test_tickers_with_ampersand_are_valid(monkeypatch):
    """M&M is a real NSE ticker; validation must not reject ampersands."""
    from app.service import normalize_symbol

    assert normalize_symbol("m&m") == "M&M"

    with _fresh_db(monkeypatch):
        from app.main import app
        from app import providers

        _mock_fx(monkeypatch)
        monkeypatch.setattr(providers.YahooChartProvider, "quote",
                            lambda self, symbol: _quote(3200.0, int(time.time())))
        with TestClient(app) as client:
            response = client.post("/watchlist/alice/symbols", json={"symbol": "M&M"})
            assert response.status_code == 201
            assert response.json()["symbol"] == "M&M"


def test_format_inr_uses_indian_grouping():
    from app.service import format_inr

    assert format_inr(1420) == "₹1,420.00"
    assert format_inr(12345678.9) == "₹1,23,45,678.90"
    assert format_inr(None) is None
