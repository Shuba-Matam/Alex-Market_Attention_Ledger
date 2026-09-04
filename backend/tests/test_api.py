import os
import tempfile

from fastapi.testclient import TestClient


def test_watchlist_signals_and_snapshot_cap():
    with tempfile.TemporaryDirectory() as directory:
        os.environ["DATABASE_PATH"] = os.path.join(directory, "test.db")
        # Import after env setup because DB location is a module constant.
        from app.main import app
        with TestClient(app) as client:
            assert client.get("/health").json() == {"status": "ok"}
            assert client.post("/watchlist/alice/symbols", json={"symbol": "aapl"}).status_code == 201
            for _ in range(7):
                assert client.post("/demo/tick", json={"symbol": "AAPL"}).status_code == 200
            data = client.get("/watchlist/alice").json()["items"][0]
            assert data["symbol"] == "AAPL"
            # Adding a symbol captures the first snapshot, then seven manual ticks follow.
            assert len(data["recent_prices"]) == 8
            assert data["anomaly_zscore"] is not None
            assert client.post("/watchlist/alice/seen").json()["marked"] == 1
            assert client.post("/demo/tick", json={"symbol": "AAPL", "scenario": "price_jump"}).status_code == 200
            assert client.get("/watchlist/alice").json()["items"][0]["personal_delta_pct"] is not None
