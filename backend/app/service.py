from __future__ import annotations

import statistics
import time
from typing import Optional

from .db import connection
from .providers import (
    FxRateProvider,
    TwelveDataProvider,
    YahooChartProvider,
    choose_freshest,
    convert_to_inr,
    is_conflict,
)

PERSONAL_DELTA_THRESHOLD = 5.0
ANOMALY_ZSCORE_THRESHOLD = 2.0
VOLUME_SPIKE_THRESHOLD = 2.0
MAX_SNAPSHOTS_PER_SYMBOL = 100
STALE_AFTER_SECONDS = 45
MIN_HISTORY = 5


def format_inr(amount: Optional[float]) -> Optional[str]:
    """Format a numeric INR amount with Indian lakh/crore separators without changing storage."""
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
    """Set a first baseline (in INR) only when this user has never seen this symbol."""
    now = int(time.time())
    with connection() as conn:
        price = conn.execute(
            "SELECT price_inr FROM price_snapshots WHERE symbol=? ORDER BY provider_timestamp DESC, id DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        if price and price["price_inr"] is not None:
            conn.execute(
                """INSERT OR IGNORE INTO user_symbol_state(username, symbol, last_seen_price, last_seen_at)
                   VALUES (?, ?, ?, ?)""",
                (username, symbol, price["price_inr"], now),
            )


def remove_symbol(username: str, symbol: str) -> bool:
    with connection() as conn:
        return conn.execute("DELETE FROM watchlist_items WHERE username=? AND symbol=?", (username, normalize_symbol(symbol))).rowcount > 0


def tick(symbol: Optional[str] = None) -> list[dict]:
    """Poll live sources once for each watchlisted symbol and persist the resolved INR quote.

    Both sources are converted to INR before comparison. A spread beyond
    CONFLICT_THRESHOLD_PCT is a real conflict: the fresher timestamp wins and
    the winner is recorded. A single responding source is recorded as
    single-sourced rather than silently treated as agreed-upon.
    """
    with connection() as conn:
        symbols = [normalize_symbol(symbol)] if symbol else [r["symbol"] for r in conn.execute("SELECT DISTINCT symbol FROM watchlist_items")]
    yahoo, twelve = YahooChartProvider(), TwelveDataProvider()
    usd_to_inr = FxRateProvider().get_usd_to_inr()
    now = int(time.time())
    result = []
    for item in symbols:
        candidates = []
        live = yahoo.quote(item)
        if live:
            candidates.append(live)
        cross_check = twelve.quote(item)
        if cross_check:
            candidates.append(cross_check)
        if not candidates:
            # No source responded; the previous snapshot stays visible and ages toward stale.
            result.append({"symbol": item, "updated": False, "reason": "no source responded"})
            continue

        converted = {quote.source: convert_to_inr(quote.price, quote.currency, usd_to_inr) for quote in candidates}
        conflict = len(candidates) > 1 and is_conflict(converted[candidates[0].source], converted[candidates[1].source])
        if conflict:
            winner = choose_freshest(candidates)
        else:
            # Sources agree (or only one reported): prefer the primary source.
            winner = next((q for q in candidates if q.source == yahoo.source), candidates[0])
        winner_inr = converted[winner.source]
        previous_close = winner.previous_close
        previous_close_inr = convert_to_inr(previous_close, winner.currency, usd_to_inr) if previous_close is not None else None
        sources = ",".join(sorted(q.source for q in candidates))
        with connection() as conn:
            conn.execute("INSERT OR IGNORE INTO symbols(symbol, created_at) VALUES (?, ?)", (item, now))
            conn.execute("""INSERT INTO price_snapshots(symbol, price, native_currency, price_inr, usd_to_inr,
                         previous_close, previous_close_inr, volume, provider_timestamp, fetched_at, source,
                         source_winner, candidate_sources, conflict_detected, single_sourced)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                         (item, winner.price, winner.currency, winner_inr, usd_to_inr,
                          previous_close, previous_close_inr, winner.volume, winner.timestamp, now, winner.source,
                          winner.source if conflict else "", sources, int(conflict), int(len(candidates) == 1)))
            conn.execute("""DELETE FROM price_snapshots WHERE id IN (
                            SELECT id FROM price_snapshots WHERE symbol=?
                            ORDER BY provider_timestamp DESC, id DESC LIMIT -1 OFFSET ?)
                         """, (item, MAX_SNAPSHOTS_PER_SYMBOL))
        result.append({"symbol": item, "updated": True, "price_inr": winner_inr, "native_price": winner.price,
                       "native_currency": winner.currency, "source_winner": winner.source,
                       "candidate_sources": sources, "conflict_detected": conflict,
                       "single_sourced": len(candidates) == 1})
    return result


def _signal(row, snapshots: list) -> dict:
    prices = [r["price_inr"] for r in snapshots]
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
                               "single_sourced": True, "recent_prices": [], "personal_delta_pct": None,
                               "anomaly_zscore": None, "volume_spike_ratio": None, "flagged": False,
                               "data_age_seconds": None, "is_stale": True})
                continue
            latest = snapshots[0]
            # Prefer the provider's own previous-session close; fall back to a snapshot
            # older than 24h so the delta is still meaningful if the field is missing.
            previous_close = latest["previous_close_inr"]
            if previous_close is None:
                prior_row = conn.execute(
                    "SELECT price_inr FROM price_snapshots WHERE symbol=? AND provider_timestamp <= ? ORDER BY provider_timestamp DESC LIMIT 1",
                    (item["symbol"], latest["provider_timestamp"] - 86_400),
                ).fetchone()
                previous_close = prior_row["price_inr"] if prior_row else None
            previous_delta = (round((latest["price_inr"] - previous_close) / previous_close * 100, 2)
                              if previous_close not in (None, 0) else None)
            result.append({"symbol": item["symbol"], "price": latest["price_inr"],
                           "native_price": latest["price"], "native_currency": latest["native_currency"],
                           "volume": latest["volume"],
                           "price_display": format_inr(latest["price_inr"]),
                           "source": latest["source"], "source_winner": latest["source_winner"] or latest["source"],
                           "provider_timestamp": latest["provider_timestamp"],
                           "candidate_sources": latest["candidate_sources"].split(","),
                           "conflict_detected": bool(latest["conflict_detected"]),
                           "single_sourced": bool(latest["single_sourced"]),
                           "recent_prices": list(reversed([r["price_inr"] for r in snapshots])),
                           "previous_session_close": previous_close,
                           "previous_session_close_display": format_inr(previous_close),
                           "previous_session_delta_pct": previous_delta,
                           **_signal(item, snapshots)})
    return sorted(result, key=lambda x: (not x["flagged"], x["symbol"]))


def mark_seen(username: str) -> int:
    now = int(time.time())
    with connection() as conn:
        rows = conn.execute("""SELECT w.symbol, (SELECT price_inr FROM price_snapshots p WHERE p.symbol=w.symbol
                            ORDER BY provider_timestamp DESC, id DESC LIMIT 1) AS price FROM watchlist_items w WHERE w.username=?""", (username,)).fetchall()
        for row in rows:
            if row["price"] is not None:
                conn.execute("""INSERT INTO user_symbol_state(username,symbol,last_seen_price,last_seen_at) VALUES(?,?,?,?)
                                ON CONFLICT(username,symbol) DO UPDATE SET last_seen_price=excluded.last_seen_price,last_seen_at=excluded.last_seen_at""",
                             (username, row["symbol"], row["price"], now))
    return len(rows)
