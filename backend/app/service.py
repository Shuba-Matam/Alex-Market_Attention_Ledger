from __future__ import annotations

import statistics
import time
from typing import Optional

from .db import connection
from .providers import SyntheticProvider, TwelveDataProvider, choose_freshest

PERSONAL_DELTA_THRESHOLD = 5.0
ANOMALY_ZSCORE_THRESHOLD = 2.0
VOLUME_SPIKE_THRESHOLD = 2.0
MAX_SNAPSHOTS_PER_SYMBOL = 100
STALE_AFTER_SECONDS = 45
MIN_HISTORY = 5

# Fixed, representative OHLCV fixture for the first-run demo. It is deliberately
# labelled demo_dataset in the API/UI and is never presented as live market data.
DEMO_SERIES: dict[str, list[tuple[float, int]]] = {
    "AAPL": [(221.6, 48_300_000), (222.4, 45_100_000), (220.9, 49_800_000), (223.1, 46_700_000), (224.0, 47_900_000), (223.5, 44_900_000), (225.2, 51_100_000), (224.7, 46_500_000), (226.1, 48_800_000), (225.6, 47_200_000), (227.0, 50_300_000), (226.5, 45_600_000)],
    "MSFT": [(412.4, 19_100_000), (413.1, 18_700_000), (411.8, 20_400_000), (414.2, 19_600_000), (415.0, 18_900_000), (414.6, 20_100_000), (416.3, 19_300_000), (415.7, 18_600_000), (417.1, 20_000_000), (416.8, 19_500_000), (418.2, 18_800_000), (417.9, 19_200_000)],
    "NVDA": [(121.8, 214_000_000), (122.6, 218_000_000), (121.9, 209_000_000), (123.1, 221_000_000), (122.7, 216_000_000), (123.8, 224_000_000), (124.2, 219_000_000), (123.6, 212_000_000), (125.1, 226_000_000), (124.7, 220_000_000), (126.4, 224_000_000), (133.2, 746_000_000)],
    "TSLA": [(244.8, 88_000_000), (242.9, 91_000_000), (245.4, 85_000_000), (243.7, 89_000_000), (241.9, 93_000_000), (242.6, 87_000_000), (240.8, 92_000_000), (239.7, 94_000_000), (241.1, 88_000_000), (240.4, 90_000_000), (238.9, 96_000_000), (237.6, 98_000_000)],
}


def normalize_symbol(symbol: str) -> str:
    value = symbol.strip().upper()
    if not value or len(value) > 15 or not all(c.isalnum() or c in ".-" for c in value):
        raise ValueError("symbol must be 1-15 letters, numbers, dots, or hyphens")
    return value


def add_symbol(username: str, symbol: str) -> str:
    symbol = normalize_symbol(symbol)
    if not username.strip():
        raise ValueError("username is required")
    now = int(time.time())
    with connection() as conn:
        conn.execute("INSERT OR IGNORE INTO users(username, created_at) VALUES (?, ?)", (username, now))
        conn.execute("INSERT OR IGNORE INTO symbols(symbol, created_at) VALUES (?, ?)", (symbol, now))
        conn.execute("INSERT OR IGNORE INTO watchlist_items(username, symbol, created_at) VALUES (?, ?, ?)", (username, symbol, now))
    return symbol


def seed_demo_watchlist() -> None:
    """Create a useful first-run demo only when the demo account is untouched."""
    now = int(time.time())
    with connection() as conn:
        already_seeded = conn.execute(
            "SELECT 1 FROM watchlist_items WHERE username='demo' LIMIT 1"
        ).fetchone()
        if already_seeded:
            return
        conn.execute("INSERT OR IGNORE INTO users(username, created_at) VALUES ('demo', ?)", (now,))
        for symbol, series in DEMO_SERIES.items():
            conn.execute("INSERT OR IGNORE INTO symbols(symbol, created_at) VALUES (?, ?)", (symbol, now))
            conn.execute(
                "INSERT OR IGNORE INTO watchlist_items(username, symbol, created_at) VALUES ('demo', ?, ?)",
                (symbol, now),
            )
            for index, (price, volume) in enumerate(series):
                timestamp = now - (len(series) - index) * 300
                conn.execute(
                    """INSERT INTO price_snapshots(symbol, price, volume, provider_timestamp, fetched_at, source, conflict_detected, candidate_sources)
                       VALUES (?, ?, ?, ?, ?, 'demo_dataset', 0, 'demo_dataset')""",
                    (symbol, price, volume, timestamp, now),
                )
            # The demo represents a returning user: this makes the personal-change
            # signal meaningful immediately, while remaining deterministic.
            seen_price = series[-6][0]
            conn.execute(
                """INSERT OR REPLACE INTO user_symbol_state(username, symbol, last_seen_price, last_seen_at)
                   VALUES ('demo', ?, ?, ?)""",
                (symbol, seen_price, now - 1800),
            )


def remove_symbol(username: str, symbol: str) -> bool:
    with connection() as conn:
        return conn.execute("DELETE FROM watchlist_items WHERE username=? AND symbol=?", (username, normalize_symbol(symbol))).rowcount > 0


def tick(symbol: Optional[str] = None, scenario: str = "normal") -> list[dict]:
    with connection() as conn:
        symbols = [normalize_symbol(symbol)] if symbol else [r["symbol"] for r in conn.execute("SELECT DISTINCT symbol FROM watchlist_items")]
    synthetic, twelve = SyntheticProvider(), TwelveDataProvider()
    result = []
    for item in symbols:
        with connection() as conn:
            sequence = conn.execute("SELECT COUNT(*) AS n FROM price_snapshots WHERE symbol=?", (item,)).fetchone()["n"]
        candidates = [synthetic.quote(item, sequence, scenario)]
        live = twelve.quote(item)
        if live:
            candidates.append(live)
        winner = choose_freshest(candidates)
        now = int(time.time())
        sources = ",".join(sorted(q.source for q in candidates))
        with connection() as conn:
            conn.execute("INSERT OR IGNORE INTO symbols(symbol, created_at) VALUES (?, ?)", (item, now))
            conn.execute("""INSERT INTO price_snapshots(symbol, price, volume, provider_timestamp, fetched_at, source, conflict_detected, candidate_sources)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                         (item, winner.price, winner.volume, winner.timestamp, now, winner.source, len(candidates) > 1, sources))
            conn.execute("""DELETE FROM price_snapshots WHERE id IN (
                            SELECT id FROM price_snapshots WHERE symbol=?
                            ORDER BY provider_timestamp DESC, id DESC LIMIT -1 OFFSET ?)
                         """, (item, MAX_SNAPSHOTS_PER_SYMBOL))
        result.append({"symbol": item, "price": winner.price, "source": winner.source,
                       "candidate_sources": sources, "conflict_detected": len(candidates) > 1})
    return result


def _signal(row, snapshots: list) -> dict:
    prices = [r["price"] for r in snapshots]
    volumes = [r["volume"] for r in snapshots]
    latest = prices[0]
    prior_prices = prices[1:]
    prior_volumes = [v for v in volumes[1:] if v is not None]
    personal = None
    if row["last_seen_price"] not in (None, 0):
        personal = round((latest - row["last_seen_price"]) / row["last_seen_price"] * 100, 2)
    zscore = None
    if len(prior_prices) >= MIN_HISTORY:
        deviation = statistics.pstdev(prior_prices)
        if deviation > 0:
            zscore = round((latest - statistics.mean(prior_prices)) / deviation, 2)
    volume_ratio = None
    if volumes[0] is not None and len(prior_volumes) >= MIN_HISTORY:
        mean_volume = statistics.mean(prior_volumes)
        if mean_volume > 0:
            volume_ratio = round(volumes[0] / mean_volume, 2)
    flagged = ((personal is not None and abs(personal) > PERSONAL_DELTA_THRESHOLD) or
               (zscore is not None and abs(zscore) > ANOMALY_ZSCORE_THRESHOLD) or
               (volume_ratio is not None and volume_ratio > VOLUME_SPIKE_THRESHOLD))
    # Freshness belongs to the latest market snapshot, not the per-user seen state.
    age = max(0, int(time.time()) - snapshots[0]["fetched_at"])
    return {"personal_delta_pct": personal, "anomaly_zscore": zscore, "volume_spike_ratio": volume_ratio,
            "flagged": flagged, "data_age_seconds": age, "is_stale": age > STALE_AFTER_SECONDS}


def watchlist(username: str) -> list[dict]:
    with connection() as conn:
        items = conn.execute("""SELECT w.symbol, state.last_seen_price, state.last_seen_at FROM watchlist_items w
                             LEFT JOIN user_symbol_state state ON state.username=w.username AND state.symbol=w.symbol
                             WHERE w.username=? ORDER BY w.symbol""", (username,)).fetchall()
        result = []
        for item in items:
            snapshots = conn.execute("SELECT * FROM price_snapshots WHERE symbol=? ORDER BY provider_timestamp DESC, id DESC LIMIT 20", (item["symbol"],)).fetchall()
            if not snapshots:
                result.append({"symbol": item["symbol"], "price": None, "volume": None, "source": None,
                               "provider_timestamp": None, "candidate_sources": [], "conflict_detected": False,
                               "recent_prices": [], "personal_delta_pct": None, "anomaly_zscore": None,
                               "volume_spike_ratio": None, "flagged": False, "data_age_seconds": None, "is_stale": True})
                continue
            latest = snapshots[0]
            result.append({"symbol": item["symbol"], "price": latest["price"], "volume": latest["volume"],
                           "source": latest["source"], "provider_timestamp": latest["provider_timestamp"],
                           "candidate_sources": latest["candidate_sources"].split(","),
                           "conflict_detected": bool(latest["conflict_detected"]),
                           "recent_prices": list(reversed([r["price"] for r in snapshots])), **_signal(item, snapshots)})
    return sorted(result, key=lambda x: (not x["flagged"], x["symbol"]))


def mark_seen(username: str) -> int:
    now = int(time.time())
    with connection() as conn:
        rows = conn.execute("""SELECT w.symbol, (SELECT price FROM price_snapshots p WHERE p.symbol=w.symbol
                            ORDER BY provider_timestamp DESC, id DESC LIMIT 1) AS price FROM watchlist_items w WHERE w.username=?""", (username,)).fetchall()
        for row in rows:
            if row["price"] is not None:
                conn.execute("""INSERT INTO user_symbol_state(username,symbol,last_seen_price,last_seen_at) VALUES(?,?,?,?)
                                ON CONFLICT(username,symbol) DO UPDATE SET last_seen_price=excluded.last_seen_price,last_seen_at=excluded.last_seen_at""",
                             (username, row["symbol"], row["price"], now))
    return len(rows)
