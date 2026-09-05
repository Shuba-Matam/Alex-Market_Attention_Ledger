import os
import tempfile
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
    from app import db
    monkeypatch.setattr(db, "DATABASE_PATH", path)
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


def test_format_inr_uses_indian_grouping():
    from app.service import format_inr

    assert format_inr(1420) == "₹1,420.00"
    assert format_inr(12345678.9) == "₹1,23,45,678.90"
    assert format_inr(None) is None
