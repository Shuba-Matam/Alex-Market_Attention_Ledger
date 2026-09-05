from __future__ import annotations

import statistics
import time
from typing import Optional

from .db import connection
from .providers import SyntheticProvider, TwelveDataProvider, YFinanceProvider, choose_freshest

PERSONAL_DELTA_THRESHOLD = 5.0
ANOMALY_ZSCORE_THRESHOLD = 2.0
VOLUME_SPIKE_THRESHOLD = 2.0
MAX_SNAPSHOTS_PER_SYMBOL = 100
STALE_AFTER_SECONDS = 45
MIN_HISTORY = 5

def _demo_series(base: float, volume: int) -> list[tuple[float, int]]:
    """Representative INR OHLCV fixture, spanning more than one market day."""
    moves = (-0.016, -0.009, -0.004, 0.003, -0.006, 0.008, 0.013, 0.007, 0.018, 0.012, 0.024, 0.019)
    return [(round(base * (1 + move), 2), int(volume * (0.84 + (index % 5) * 0.07)))
            for index, move in enumerate(moves)]


# Fixed Indian-market demo data. It is labelled demo_dataset and is never shown as
# live market data. Prices remain numeric in SQLite; formatting occurs at the API/UI boundary.
DEMO_SERIES: dict[str, list[tuple[float, int]]] = {
    "RELIANCE": _demo_series(1_420, 4_800_000),
    "TCS": _demo_series(3_860, 1_900_000),
    "INFY": _demo_series(1_520, 5_400_000),
    "HDFCBANK": _demo_series(1_770, 6_200_000),
    "ICICIBANK": _demo_series(1_360, 8_300_000),
    "SBIN": _demo_series(820, 12_100_000),
    "BHARTIARTL": _demo_series(1_650, 4_100_000),
    "ITC": _demo_series(470, 9_500_000),
    "LT": _demo_series(3_640, 1_600_000),
    "HINDUNILVR": _demo_series(2_370, 1_100_000),
    "KOTAKBANK": _demo_series(1_940, 2_700_000),
    "AXISBANK": _demo_series(1_150, 6_800_000),
}


def format_inr(amount: Optional[float]) -> Optional[str]:
    """Format a numeric amount with Indian lakh/crore separators without changing storage."""
    if amount is None:
        return None
    sign = "-" if amount < 0 else ""
    whole, fraction = f"{abs(amount):.2f}".split(".")
    if len(whole) > 3:
        tail, prefix = whole[-3:], whole[:-3]
        groups = []
        while prefix:
            groups.append(prefix[-2:])
            prefix = prefix[:-2]
        whole = ",".join(reversed(groups)) + "," + tail
    return f"{sign}₹{whole}.{fraction}"


def normalize_symbol(symbol: str) -> str:
    value = symbol.strip().upper()
    if value.endswith(".NS"):
        value = value[:-3]
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


def initialize_seen_price(username: str, symbol: str) -> None:
    """Set a first baseline only when this user has never seen this symbol."""
    now = int(time.time())
    with connection() as conn:
        price = conn.execute(
            "SELECT price FROM price_snapshots WHERE symbol=? ORDER BY provider_timestamp DESC, id DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        if price and price["price"] is not None:
            conn.execute(
                """INSERT OR IGNORE INTO user_symbol_state(username, symbol, last_seen_price, last_seen_at)
                   VALUES (?, ?, ?, ?)""",
                (username, symbol, price["price"], now),
            )


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
                timestamp = now - (len(series) - index - 1) * 10_800
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
    synthetic, twelve, yfinance = SyntheticProvider(), TwelveDataProvider(), YFinanceProvider()
    result = []
    for item in symbols:
        with connection() as conn:
            sequence = conn.execute("SELECT COUNT(*) AS n FROM price_snapshots WHERE symbol=?", (item,)).fetchone()["n"]
        candidates = [synthetic.quote(item, sequence, scenario)]
        live = twelve.quote(item)
        if live:
            candidates.append(live)
        nse_quote = yfinance.quote(item)
        if nse_quote:
            candidates.append(nse_quote)
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
            snapshots = conn.execute("SELECT * FROM price_snapshots WHERE symbol=? ORDER BY provider_timestamp DESC, id DESC LIMIT 30", (item["symbol"],)).fetchall()
            if not snapshots:
                result.append({"symbol": item["symbol"], "price": None, "volume": None, "source": None,
                               "provider_timestamp": None, "candidate_sources": [], "conflict_detected": False,
                               "recent_prices": [], "personal_delta_pct": None, "anomaly_zscore": None,
                               "volume_spike_ratio": None, "flagged": False, "data_age_seconds": None, "is_stale": True})
                continue
            latest = snapshots[0]
            prior_session = next(
                (snapshot for snapshot in snapshots[1:]
                 if snapshot["provider_timestamp"] <= latest["provider_timestamp"] - 86_400),
                None,
            )
            previous_close = prior_session["price"] if prior_session else None
            previous_delta = (round((latest["price"] - previous_close) / previous_close * 100, 2)
                              if previous_close not in (None, 0) else None)
            result.append({"symbol": item["symbol"], "price": latest["price"], "volume": latest["volume"],
                           "price_display": format_inr(latest["price"]),
                           "source": latest["source"], "provider_timestamp": latest["provider_timestamp"],
                           "candidate_sources": latest["candidate_sources"].split(","),
                           "conflict_detected": bool(latest["conflict_detected"]),
                           "recent_prices": list(reversed([r["price"] for r in snapshots])),
                           "previous_session_close": previous_close,
                           "previous_session_close_display": format_inr(previous_close),
                           "previous_session_delta_pct": previous_delta,
                           **_signal(item, snapshots)})
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
