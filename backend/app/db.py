import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", Path(__file__).resolve().parents[1] / "watchlist.db"))


@contextmanager
def connection():
    # timeout/busy_timeout: the background poller and API handlers write
    # concurrently; a short lock wait is cheaper than a failed request.
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connection() as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS users (
              username TEXT PRIMARY KEY,
              created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS symbols (
              symbol TEXT PRIMARY KEY,
              created_at INTEGER NOT NULL,
              last_polled_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS watchlist_items (
              username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
              symbol TEXT NOT NULL REFERENCES symbols(symbol) ON DELETE CASCADE,
              created_at INTEGER NOT NULL,
              PRIMARY KEY (username, symbol)
            );
            CREATE TABLE IF NOT EXISTS price_snapshots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              symbol TEXT NOT NULL REFERENCES symbols(symbol) ON DELETE CASCADE,
              price REAL NOT NULL,
              native_currency TEXT NOT NULL DEFAULT 'INR',
              price_inr REAL NOT NULL,
              usd_to_inr REAL,
              previous_close REAL,
              previous_close_inr REAL,
              volume REAL,
              provider_timestamp INTEGER NOT NULL,
              fetched_at INTEGER NOT NULL,
              source TEXT NOT NULL,
              source_winner TEXT NOT NULL DEFAULT '',
              candidate_sources TEXT NOT NULL DEFAULT '',
              conflict_detected INTEGER NOT NULL DEFAULT 0,
              single_sourced INTEGER NOT NULL DEFAULT 0,
              conflict_other_source TEXT NOT NULL DEFAULT '',
              conflict_other_price_inr REAL
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_symbol_time
              ON price_snapshots(symbol, provider_timestamp DESC, id DESC);
            CREATE TABLE IF NOT EXISTS user_symbol_state (
              username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
              symbol TEXT NOT NULL REFERENCES symbols(symbol) ON DELETE CASCADE,
              last_seen_price REAL,
              last_seen_at INTEGER,
              PRIMARY KEY (username, symbol)
            );
            """
        )
        migrate_conflict_columns(conn)
        migrate_add_last_polled(conn)


def migrate_conflict_columns(conn: sqlite3.Connection) -> None:
    """Idempotent: older DBs predate the conflict-visibility columns."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(price_snapshots)")}
    if "conflict_other_source" not in existing:
        conn.execute("ALTER TABLE price_snapshots ADD COLUMN conflict_other_source TEXT NOT NULL DEFAULT ''")
    if "conflict_other_price_inr" not in existing:
        conn.execute("ALTER TABLE price_snapshots ADD COLUMN conflict_other_price_inr REAL")


def migrate_add_last_polled(conn: sqlite3.Connection) -> None:
    """Idempotent: per-symbol ingestion freshness, kept even when a poll finds
    nothing new (so 'is this feed alive' is not confused with 'nothing moved')."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(symbols)")}
    if "last_polled_at" not in existing:
        conn.execute("ALTER TABLE symbols ADD COLUMN last_polled_at INTEGER")
