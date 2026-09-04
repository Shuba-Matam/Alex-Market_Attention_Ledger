import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", Path(__file__).resolve().parents[1] / "watchlist.db"))


@contextmanager
def connection():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
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
              created_at INTEGER NOT NULL
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
              volume REAL,
              provider_timestamp INTEGER NOT NULL,
              fetched_at INTEGER NOT NULL,
              source TEXT NOT NULL,
              conflict_detected INTEGER NOT NULL DEFAULT 0,
              candidate_sources TEXT NOT NULL DEFAULT ''
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
