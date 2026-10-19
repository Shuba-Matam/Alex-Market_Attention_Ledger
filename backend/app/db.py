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
        # Per-connection, not per-database: without this on every connection the
        # ON DELETE CASCADE rules below are silently inert and deleting a user
        # would leave its watchlist rows orphaned.
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


TABLES = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  created_at INTEGER NOT NULL,
  email TEXT,
  password_hash TEXT,
  google_sub TEXT,
  is_demo INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS symbols (
  symbol TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL,
  last_polled_at INTEGER
);
CREATE TABLE IF NOT EXISTS watchlist_items (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  symbol TEXT NOT NULL REFERENCES symbols(symbol) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (user_id, symbol)
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
CREATE TABLE IF NOT EXISTS user_symbol_state (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  symbol TEXT NOT NULL REFERENCES symbols(symbol) ON DELETE CASCADE,
  last_seen_price REAL,
  last_seen_at INTEGER,
  PRIMARY KEY (user_id, symbol)
);
"""

INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email
  ON users(email) WHERE email IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_sub
  ON users(google_sub) WHERE google_sub IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_snapshots_symbol_time
  ON price_snapshots(symbol, provider_timestamp DESC, id DESC);
"""


def _apply(conn: sqlite3.Connection, script: str) -> None:
    """Statement-by-statement rather than executescript(), which issues an
    implicit COMMIT and would silently end the migration transaction."""
    for statement in (part.strip() for part in script.split(";")):
        if statement:
            conn.execute(statement)


def init_db() -> None:
    # A dedicated connection with autocommit (isolation_level=None) because
    # PRAGMA foreign_keys cannot change inside a transaction, and the
    # username -> user_id rebuild below must run with enforcement off.
    conn = sqlite3.connect(DATABASE_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=OFF")
        _apply(conn, TABLES)
        # Column adds run before the rebuild so a legacy table is guaranteed to
        # have every column the rebuild copies across, and before INDEXES
        # because the partial indexes reference those new columns.
        migrate_conflict_columns(conn)
        migrate_add_last_polled(conn)
        migrate_add_auth_columns(conn)
        migrate_to_user_ids(conn)
        _apply(conn, INDEXES)
    finally:
        conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def migrate_to_user_ids(conn: sqlite3.Connection) -> None:
    """Rebuild username-keyed tables onto a surrogate users.id tenant key.

    Identity used to be the email-derived username, which meant an attacker
    could occupy a name before its owner registered, and a rename would
    re-point an existing ledger. user_id is opaque and immutable, so per-user
    scoping cannot be influenced by anything a client sends.
    """
    if not _table_exists(conn, "users") or "id" in _table_columns(conn, "users"):
        return
    old = _table_columns(conn, "users")
    email_expr = "email" if "email" in old else "NULL"
    password_expr = "password_hash" if "password_hash" in old else "NULL"
    conn.execute("BEGIN")
    try:
        for table in ("users", "watchlist_items", "user_symbol_state"):
            if _table_exists(conn, table):
                conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
        # Indexes follow their renamed table, so the names must be freed first.
        conn.execute("DROP INDEX IF EXISTS idx_users_email")
        conn.execute("DROP INDEX IF EXISTS idx_users_google_sub")
        _apply(conn, TABLES)
        # Accounts without a password were the open-by-design demo accounts.
        conn.execute(
            f"""INSERT INTO users(username, created_at, email, password_hash, is_demo)
                SELECT username, created_at, {email_expr}, {password_expr},
                       CASE WHEN {password_expr} IS NULL OR {password_expr}='' THEN 1 ELSE 0 END
                FROM users_old"""
        )
        if _table_exists(conn, "watchlist_items_old"):
            conn.execute(
                """INSERT OR IGNORE INTO watchlist_items(user_id, symbol, created_at)
                   SELECT u.id, w.symbol, w.created_at FROM watchlist_items_old w
                   JOIN users u ON u.username = w.username"""
            )
        if _table_exists(conn, "user_symbol_state_old"):
            conn.execute(
                """INSERT OR IGNORE INTO user_symbol_state(user_id, symbol, last_seen_price, last_seen_at)
                   SELECT u.id, s.symbol, s.last_seen_price, s.last_seen_at FROM user_symbol_state_old s
                   JOIN users u ON u.username = s.username"""
            )
        for table in ("users_old", "watchlist_items_old", "user_symbol_state_old"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def migrate_conflict_columns(conn: sqlite3.Connection) -> None:
    """Idempotent: older DBs predate the conflict-visibility columns."""
    existing = _table_columns(conn, "price_snapshots")
    if "conflict_other_source" not in existing:
        conn.execute("ALTER TABLE price_snapshots ADD COLUMN conflict_other_source TEXT NOT NULL DEFAULT ''")
    if "conflict_other_price_inr" not in existing:
        conn.execute("ALTER TABLE price_snapshots ADD COLUMN conflict_other_price_inr REAL")


def migrate_add_last_polled(conn: sqlite3.Connection) -> None:
    """Idempotent: per-symbol ingestion freshness, kept even when a poll finds
    nothing new (so 'is this feed alive' is not confused with 'nothing moved')."""
    if "last_polled_at" not in _table_columns(conn, "symbols"):
        conn.execute("ALTER TABLE symbols ADD COLUMN last_polled_at INTEGER")


def migrate_add_auth_columns(conn: sqlite3.Connection) -> None:
    """Idempotent: credential columns for users (demo accounts stay NULL)."""
    existing = _table_columns(conn, "users")
    if "email" not in existing:
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
    if "password_hash" not in existing:
        conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
    if "google_sub" not in existing:
        conn.execute("ALTER TABLE users ADD COLUMN google_sub TEXT")
    if "is_demo" not in existing:
        conn.execute("ALTER TABLE users ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 0")