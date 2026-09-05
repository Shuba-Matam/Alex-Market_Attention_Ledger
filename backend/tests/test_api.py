import os
import tempfile

from fastapi.testclient import TestClient


def test_watchlist_signals_and_snapshot_cap(monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        os.environ["DATABASE_PATH"] = os.path.join(directory, "test.db")
        # Import after env setup because DB location is a module constant.
        from app.main import app
        from app.providers import YFinanceProvider

        # Unit tests exercise the fallback contract without depending on Yahoo's network service.
        monkeypatch.setattr(YFinanceProvider, "quote", lambda self, symbol: None)
        with TestClient(app) as client:
            assert client.get("/health").json() == {"status": "ok"}
            assert client.post("/watchlist/alice/symbols", json={"symbol": "reliance"}).status_code == 201
            for _ in range(7):
                assert client.post("/demo/tick", json={"symbol": "RELIANCE"}).status_code == 200
            data = client.get("/watchlist/alice").json()["items"][0]
            assert data["symbol"] == "RELIANCE"
            # The Indian demo seeds RELIANCE and the background poller can add one point on startup.
            assert 20 <= len(data["recent_prices"]) <= 30
            assert data["anomaly_zscore"] is not None
            assert data["previous_session_close"] is not None
            assert client.post("/watchlist/alice/seen").json()["marked"] == 1
            assert client.post("/demo/tick", json={"symbol": "RELIANCE", "scenario": "price_jump"}).status_code == 200
            assert client.get("/watchlist/alice").json()["items"][0]["personal_delta_pct"] is not None


def test_format_inr_uses_indian_grouping():
    from app.service import format_inr

    assert format_inr(1420) == "₹1,420.00"
    assert format_inr(12345678.9) == "₹1,23,45,678.90"
    assert format_inr(None) is None
