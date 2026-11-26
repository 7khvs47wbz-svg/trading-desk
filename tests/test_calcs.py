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
    bt = market.backtest("leaders", syms)
    assert bt["supported"] and bt["picks"] > 0
    data = market.bulk_history(sorted(set(syms) | {"SPY"}), "5y")
    p = bt["recent"][0]
    spy = data["SPY"]["Close"]
    t0 = spy.index.get_loc(pd.Timestamp(p["date"]))
    t1_date = spy.index[t0 + bt["holding_days"]]
    c = data[p["symbol"]]["Close"]
    assert p["ret"] == pytest.approx(c.loc[t1_date] / c.loc[pd.Timestamp(p["date"])] - 1, rel=1e-9)
    assert t1_date > pd.Timestamp(p["date"])  # the return is measured strictly after the signal


def test_fundamental_screens_refuse_fake_backtests():
    assert market.backtest("value", ["KO"])["supported"] is False


# ------------------------------------------------------------- trade review

def _t(setup, pnl, r, days, gate="GO", over=0, plan=None, reason="target"):
    opened = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return {"setup": setup, "pnl": pnl, "r_multiple": r, "gate_decision": gate, "overridden": over,
            "followed_plan": plan, "exit_reason": reason, "symbol": "X", "lessons": None,
            "opened_at": opened.isoformat(), "closed_at": (opened + timedelta(days=days)).isoformat()}


def test_trade_review_numbers_and_insights():
    trades = [_t("VCP", 300, 1.5, 4, plan=1), _t("VCP", 200, 1.0, 6, plan=1), _t("VCP", -100, -0.5, 10, plan=1),
              _t("Burst", -200, -1.0, 12, over=1, gate="NO_GO", plan=0), _t("Burst", -400, -2.0, 14, over=1, gate="NO_GO", plan=0),
              _t("Burst", 50, 0.25, 3, plan=1, reason="manual")]
    v = appmod.trade_review(trades)
    assert v["trades"] == 6
    vcp = next(r for r in v["by_setup"] if r["setup"] == "VCP")
    assert vcp == {"setup": "VCP", "trades": 3, "win_rate_pct": 66.7, "avg_pnl": 133.33, "net_pnl": 400, "avg_r": 0.67}
    assert v["gate"]["overridden"]["avg_pnl"] == -300 and v["gate"]["followed"]["avg_pnl"] == 112.5
    assert v["plan"]["broken"]["trades"] == 2
    assert [b["count"] for b in v["r_buckets"]] == [0, 1, 2, 1, 2, 0]
    assert v["hold_days"] == {"winners": 4.3, "losers": 12.0}
    text = " ".join(v["insights"])
    assert "overrode the discipline gate averaged −$300.00" in text
    assert "lost more than 1R" in text and "held losers 12.0 days" in text
    assert v["biggest_losses"][0]["pnl"] == -400


def test_trade_review_empty():
    assert appmod.trade_review([])["trades"] == 0


def test_break_even_trades_are_not_losses():
    v = appmod.trade_review([_t("A", 0, 0.0, 1), _t("A", 0, 0.0, 2), _t("A", 0, 0.0, 3)])
    assert v["biggest_losses"] == [] and v["hold_days"]["losers"] is None
    assert "break-even so far" in " ".join(v["insights"])
