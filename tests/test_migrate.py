"""The one-time database move copies every row, keeps ids, and new rows don't collide."""
import os
import uuid

import pytest

import db

pytestmark = pytest.mark.skipif(not db.PG, reason="needs Postgres (TEST_DATABASE_URL)")


def _fresh_db():
    import psycopg
    from psycopg.rows import dict_row
    name = "mig_" + uuid.uuid4().hex[:8]
    with psycopg.connect(db.DATABASE_URL, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
    from psycopg.conninfo import make_conninfo
    conn = psycopg.connect(make_conninfo(db.DATABASE_URL, dbname=name), row_factory=dict_row, autocommit=True)
    for stmt in filter(str.strip, db.SCHEMA.split(";")):
        conn.execute(stmt)
    return conn


def test_copy_keeps_every_row_and_id():
    src, dst = _fresh_db(), _fresh_db()
    uid = src.execute("INSERT INTO users (email, pw_hash) VALUES ('m@test.local', 'h') RETURNING id").fetchone()["id"]
    src.execute("INSERT INTO users (email, pw_hash) VALUES ('n@test.local', 'h')")
    src.execute("INSERT INTO user_settings (user_id, key, value) VALUES (%s, 'risk_pct', '1.5')", (uid,))
    for i in range(3):
        src.execute("INSERT INTO journal (user_id, symbol, pnl) VALUES (%s, %s, %s)", (uid, f"S{i}", i * 10.0))
    src.execute("INSERT INTO watchlist (user_id, symbol) VALUES (%s, 'NVDA')", (uid,))
    src.execute("INSERT INTO forward_days (kind, pick_date, n) VALUES ('vcp', '2026-11-25', 1)")
    src.execute("INSERT INTO forward_picks (kind, pick_date, symbol, rank, entry_price) VALUES ('vcp', '2026-11-25', 'AAPL', 1, 200)")
    src.execute("INSERT INTO app_cache (key, value) VALUES ('k', '{}')")
    counts = db.copy_tables(src, dst)
    assert counts["users"] == 2 and counts["journal"] == 3 and counts["forward_picks"] == 1
    for t in db.TABLES:
        assert dst.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] == src.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"], t
    assert dst.execute("SELECT id FROM users WHERE email='m@test.local'").fetchone()["id"] == uid
    assert dst.execute("SELECT COUNT(*) AS n FROM journal WHERE user_id=%s", (uid,)).fetchone()["n"] == 3
    # new rows after the move get fresh ids, not ones that already exist
    new = dst.execute("INSERT INTO journal (user_id, symbol) VALUES (%s, 'X') RETURNING id", (uid,)).fetchone()["id"]
    assert new > max(r["id"] for r in src.execute("SELECT id FROM journal").fetchall())
    # running it again changes nothing
    db.copy_tables(src, dst)
    assert dst.execute("SELECT COUNT(*) AS n FROM journal").fetchone()["n"] == 4


def test_migrate_skips_when_target_has_users():
    db.execute("INSERT INTO users (email, pw_hash) VALUES (?, 'h') ON CONFLICT (email) DO NOTHING", ("exists@test.local",))
    r = db.migrate_from("postgresql://u:p@example.invalid/x")
    assert r["status"] == "skipped" and "already has users" in r["reason"]
    assert db.migrate_from(db.DATABASE_URL)["status"] == "skipped"
