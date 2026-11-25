"""Accuracy tests: every number the app computes is checked against an
independent hand calculation. Tolerance is 2% (most checks are exact to the cent)."""
import math
import random
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import app as appmod
import db
import engines
import market

TOL = 0.02  # 2% maximum error


def close(a, b, tol=TOL):
    return abs(a - b) <= tol * max(abs(b), 1e-9) + 0.01


# ------------------------------------------------------------- position sizing (skill script)

def hand_shares(account, entry, stop, risk_pct, max_pct):
    by_risk = math.floor(account * risk_pct / 100 / (entry - stop))
    by_cap = math.floor(account * max_pct / 100 / entry)
    return min(by_risk, by_cap)


@pytest.mark.parametrize("seed", range(12))
def test_position_sizer_matches_hand_calc(seed):
    rnd = random.Random(seed)
    account = rnd.choice([5_000, 25_000, 100_000, 750_000])
    entry = round(rnd.uniform(5, 900), 2)
    stop = round(entry * rnd.uniform(0.85, 0.99), 2)
    risk, cap = rnd.choice([0.5, 1, 2]), rnd.choice([10, 20, 50])
    r = engines.position_size(account, entry, stop=stop, risk_pct=risk, max_position_pct=cap)
    exp = hand_shares(account, entry, stop, risk, cap)
    assert abs(r["final_recommended_shares"] - exp) <= max(1, 0.02 * exp)
    assert close(r["final_position_value"], r["final_recommended_shares"] * entry)
    assert close(r["final_risk_dollars"], r["final_recommended_shares"] * (entry - stop))


def test_atr_sizing_uses_two_atr_stop():
    r = engines.position_size(100_000, 50, atr=2, risk_pct=1, max_position_pct=100)
    assert r["calculations"]["atr_based"]["stop_price"] == 46
    assert r["final_recommended_shares"] == 250  # $1,000 / $4


# ------------------------------------------------------------- trade + portfolio maths

@pytest.mark.parametrize("entry,exit_,shares,stop,pnl,r", [
    (100, 110, 50, 95, 500.00, 2.0),
    (100, 90, 50, 95, -500.00, -2.0),
    (42.37, 45.12, 333, 40.00, 915.75, 1.16),
    (250, 250, 10, None, 0.0, None),
])
def test_trade_math(entry, exit_, shares, stop, pnl, r):
    got_pnl, got_r = appmod.trade_math(entry, exit_, shares, stop)
    assert got_pnl == pytest.approx(pnl, abs=0.01)
    assert got_r == (pytest.approx(r, abs=0.01) if r is not None else None)


def test_portfolio_summary():
    hs = [
        {"symbol": "AAA", "shares": 10, "cost_basis": 100, "price": 120, "sector": "Tech", "dividend_rate": 2},
        {"symbol": "BBB", "shares": 40, "cost_basis": 50, "price": 45, "sector": "Energy", "dividend_rate": 1.5},
    ]
    s = appmod.portfolio_summary(hs, cash=1200, exposure={"exposure_ceiling_pct": 60})
    assert s["total_value"] == 1200 + 1800
    assert s["cost"] == 1000 + 2000
    assert hs[0]["gain"] == 200 and hs[1]["gain"] == -200
    assert hs[0]["weight_pct"] == 40.0 and hs[1]["weight_pct"] == 60.0
    assert s["annual_income"] == 20 + 60
    assert hs[0]["yield_pct"] == pytest.approx(2 / 120 * 100, abs=0.01)
    assert s["equity_pct"] == pytest.approx(3000 / 4200 * 100, abs=0.1)
    assert any("Exposure Coach ceiling" in w for w in s["warnings"])  # 71% > 60%


def test_atr_and_quote_maths():
    idx = pd.date_range("2026-01-01", periods=30, freq="B")
    df = pd.DataFrame({"High": [11.0] * 30, "Low": [9.0] * 30, "Close": [10.0] * 30}, index=idx)
    assert market.atr(df) == pytest.approx(2.0)


# ------------------------------------------------------------- circuit breaker thresholds

def _user(email):
    return db.insert("INSERT INTO users (email, pw_hash, created_at) VALUES (?,?,?)", (email, "x", db.now_iso()))


def test_circuit_breaker_daily_limit():
    db.init()
    uid = _user("cb@test.local")
    now = datetime.now(timezone.utc).isoformat()
    db.insert("INSERT INTO journal (user_id, pnl, closed_at) VALUES (?,?,?)", (uid, -1999, now))
    assert engines.circuit_breaker(uid, 100_000, streak_n=99)["recommendation"] == "TRADING_ALLOWED"
    db.insert("INSERT INTO journal (user_id, pnl, closed_at) VALUES (?,?,?)", (uid, -1, now))
    cb = engines.circuit_breaker(uid, 100_000, streak_n=99)
    assert cb["recommendation"] == "HALTED" and cb["pnl_today"] == -2000


def test_circuit_breaker_streak_cooldown():
    uid = _user("streak@test.local")
    for i in range(2):
        t = (datetime.now(timezone.utc) - timedelta(minutes=10 - i)).isoformat()
        db.insert("INSERT INTO journal (user_id, pnl, closed_at) VALUES (?,?,?)", (uid, -10, t))
    assert engines.circuit_breaker(uid, 100_000)["recommendation"] == "COOLDOWN"


# ------------------------------------------------------------- backtest has no look-ahead

def test_backtest_forward_return_is_hand_checkable():
    syms = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "LLY", "JPM", "V", "MA", "COST"]
    bt = market.backtest("vcp", syms)
    assert bt["supported"] and bt["picks"] > 0
    data = market.bulk_history(sorted(set(syms) | {"SPY"}), "2y")
    p = bt["recent"][0]
    c = data[p["symbol"]]["Close"]
    t0 = c.index.get_loc(pd.Timestamp(p["date"]))
    spy = data["SPY"]["Close"]
    t1_date = spy.index[spy.index.get_loc(pd.Timestamp(p["date"])) + market.FWD]
    assert p["ret"] == pytest.approx(c.loc[t1_date] / c.iloc[t0] - 1, rel=1e-9)
    assert t1_date > pd.Timestamp(p["date"])  # the return is measured strictly after the signal


def test_fundamental_screens_refuse_fake_backtests():
    assert market.backtest("value", ["KO"])["supported"] is False
