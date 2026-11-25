"""Storage for users, paper trading, holdings, journal and settings.

Uses Postgres when DATABASE_URL is set (the public deployment) and a local
SQLite file otherwise. Every user-owned row carries a user_id, and every
query in app.py filters on it.
"""
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

def _clean_db_url(raw):
    """Accept the string however it was pasted: with spaces/newlines, wrapped in quotes,
    or as Neon's `psql '…'` snippet. Returns the bare postgres:// URL or ''."""
    import re
    m = re.search(r"postgres(?:ql)?://[^\s'\"]+", raw or "")
    return m.group(0) if m else ""


_RAW_DB_URL = os.environ.get("DATABASE_URL", "")
DATABASE_URL = _clean_db_url(_RAW_DB_URL)
PG = bool(DATABASE_URL)


def db_url_hint():
    """Describe DATABASE_URL without revealing it (for startup errors)."""
    raw = _RAW_DB_URL.strip()
    if not raw:
        return "DATABASE_URL is empty or missing."
    return (f"DATABASE_URL is set ({len(raw)} characters, starts with {raw[:6]!r}) "
            "but contains no postgresql:// address.")
DB_PATH = Path(os.environ.get("SQLITE_PATH", Path(__file__).parent / "data" / "trading.db"))

PK = "SERIAL PRIMARY KEY" if PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS users (
  id {PK}, email TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL, created_at TEXT,
  failed_logins INTEGER DEFAULT 0, locked_until TEXT, token_version INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS user_settings (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, key TEXT NOT NULL, value TEXT,
  PRIMARY KEY (user_id, key));
CREATE TABLE IF NOT EXISTS paper_positions (
  id {PK}, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  symbol TEXT, shares REAL, entry REAL, stop REAL, target REAL, setup TEXT, thesis TEXT,
  opened_at TEXT, gate_decision TEXT, overridden INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS paper_orders (
  id {PK}, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  symbol TEXT, side TEXT, shares REAL, price REAL, at TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS journal (
  id {PK}, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  source TEXT, symbol TEXT, shares REAL, entry REAL, exit REAL, stop REAL, pnl REAL,
  r_multiple REAL, setup TEXT, thesis TEXT, lessons TEXT, followed_plan INTEGER,
  opened_at TEXT, closed_at TEXT, exit_reason TEXT, gate_decision TEXT, overridden INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS holdings (
  id {PK}, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  symbol TEXT, shares REAL, cost_basis REAL, account TEXT, UNIQUE (user_id, symbol));
CREATE INDEX IF NOT EXISTS ix_pos_user ON paper_positions(user_id);
CREATE INDEX IF NOT EXISTS ix_ord_user ON paper_orders(user_id);
CREATE INDEX IF NOT EXISTS ix_jr_user ON journal(user_id);
"""

DEFAULTS = {
    "paper_starting_cash": 100000,
    "paper_cash": 100000,
    "risk_pct": 1.0,
    "max_position_pct": 20,
    "portfolio_cash": 0,
    "universe": "AAPL MSFT NVDA AMZN GOOGL META AVGO TSLA LLY JPM V MA UNH XOM "
    "COST HD PG JNJ ABBV MRK KO PEP WMT BAC CVX ORCL CRM AMD NFLX ADBE TMO "
    "LIN MCD CSCO ACN ABT DHR WFC TXN NEE PM INTU AMGN QCOM IBM CAT GE ISRG "
    "NOW UBER SPGI GS BKNG AMAT RTX HON T VZ LOW PFE UNP BLK SYK ELV PLD "
    "DE MDT ADP LMT PANW ANET MU LRCX KLAC SNPS CDNS CRWD MO SO DUK O VICI "
    "ENB MMM KMB GIS CL",
}

_pool = None
_pool_lock = threading.Lock()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pg_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
            _pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=8, open=True,
                                   kwargs={"row_factory": dict_row, "autocommit": True},
                                   check=ConnectionPool.check_connection)
    return _pool


def _sql(sql):
    return sql.replace("?", "%s") if PG else sql


def _exec(sql, args=(), fetch=False):
    if PG:
        with _pg_pool().connection() as c:
            cur = c.execute(_sql(sql), args)
            return cur.fetchall() if fetch else cur
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    try:
        cur = c.execute(sql, args)
        out = [dict(r) for r in cur.fetchall()] if fetch else cur
        c.commit()
        return out
    finally:
        c.close()


def init():
    for stmt in filter(str.strip, SCHEMA.split(";")):
        _exec(stmt)


def rows(sql, args=()):
    return [dict(r) for r in _exec(sql, args, fetch=True)]


def one(sql, args=()):
    r = rows(sql, args)
    return r[0] if r else None


def execute(sql, args=()):
    _exec(sql, args)


def insert(sql, args=()):
    """Run an INSERT and return the new row id."""
    return _exec(sql + " RETURNING id", args, fetch=True)[0]["id"]


# ------------------------------------------------------------- per-user settings

def get_setting(uid, key):
    r = one("SELECT value FROM user_settings WHERE user_id=? AND key=?", (uid, key))
    return json.loads(r["value"]) if r else DEFAULTS.get(key)


def set_setting(uid, key, value):
    execute("INSERT INTO user_settings (user_id, key, value) VALUES (?,?,?) "
            "ON CONFLICT (user_id, key) DO UPDATE SET value=excluded.value", (uid, key, json.dumps(value)))


def all_settings(uid):
    s = dict(DEFAULTS)
    s.update({r["key"]: json.loads(r["value"])
              for r in rows("SELECT key, value FROM user_settings WHERE user_id=?", (uid,))})
    return s
