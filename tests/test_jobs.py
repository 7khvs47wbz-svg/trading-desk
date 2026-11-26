"""Point-in-time membership, trading costs, the S&P 500 job, and the live forward test."""
import numpy as np
import pandas as pd
import pytest

import app as appmod
import db
import jobs
import market
import strategies as st
import universe
from test_strategies import fake_market

EARLY = pd.Timestamp("2000-01-01")


@pytest.fixture(autouse=True)
def clean_tables():
    db.init()
    for t in ("app_cache", "forward_days", "forward_picks"):
        db.execute(f"DELETE FROM {t}")
    jobs._state.clear()
    jobs._score_cache.clear()
    yield


def rocket(data, sym="S00"):
    """Make one stock the strongest by far, so any screen that can pick it will."""
    d = data[sym].copy()
    boost = np.exp(np.linspace(0, 3, len(d)))
    d[["Open", "High", "Low", "Close"]] = d[["Open", "High", "Low", "Close"]].mul(boost, axis=0)
    data[sym] = d
    return data


# ------------------------------------------------------------- membership & costs

def test_stock_is_never_picked_outside_its_index_membership():
    data = rocket(fake_market())
    idx = data["SPY"].index
    syms = [s for s in data if s != "SPY"]
    joined, left = idx[400], idx[560]
    m = pd.DataFrame(True, index=idx, columns=syms)
    m.loc[(m.index < joined) | (m.index >= left), "S00"] = False
    free = st.indicators({s: data[s] for s in syms}, data["SPY"], kinds=["leaders"])
    assert "S00" in st.pick("leaders", free, 350)  # without the membership rule it would be picked early
    ind = st.indicators({s: data[s] for s in syms}, data["SPY"], member=m, kinds=["leaders"])
    for t in range(300, len(idx)):
        picked = "S00" in st.pick("leaders", ind, t)
        assert picked == (joined <= idx[t] < left), idx[t]


def test_costs_are_charged_on_entry_and_exit_exactly():
    data = fake_market()
    gross = st.backtest("leaders", data)
    net = st.backtest("leaders", data, cost_per_side=0.001)
    g, n = gross["recent"][0], net["recent"][0]
    assert (g["date"], g["symbol"]) == (n["date"], n["symbol"])
    assert n["ret_gross"] == pytest.approx(g["ret"], rel=1e-12)
    assert n["ret"] == pytest.approx((1 + g["ret"]) * 0.999 / 1.001 - 1, rel=1e-12)
    assert net["overall"]["cost_drag_pct"] == pytest.approx(0.2, abs=0.01)  # ~0.1% in + 0.1% out
    assert net["overall"]["avg_vs_all_pct"] < gross["overall"]["avg_vs_all_pct"]


def test_delisted_stock_is_sold_at_its_last_price_and_coverage_is_honest():
    data = rocket(fake_market())
    idx = data["SPY"].index
    gone = idx[680]  # inside the last holds, which are the ones listed in "recent"
    data["S00"] = data["S00"].loc[:gone]  # acquired: prices stop
    syms = [s for s in data if s != "SPY"] + ["DEAD"]  # DEAD was a member but we have no prices at all
    m = pd.DataFrame(True, index=idx, columns=syms)
    bt = st.backtest("leaders", data, members=m)
    held = [p for p in bt["recent"] if p["symbol"] == "S00" and pd.Timestamp(p["date"]) < gone < pd.Timestamp(p["date"]) + pd.Timedelta(days=40)]
    for p in held:
        assert p["ret"] == pytest.approx(data["S00"]["Close"].iloc[-1] / data["S00"]["Close"].loc[p["date"]] - 1)
    assert held, "the test needs a hold that spans the delisting"
    assert bt["point_in_time"] and bt["coverage"]["mean_pct"] < 100  # DEAD counts against coverage


def test_membership_frame_from_stints(monkeypatch):
    monkeypatch.setattr(universe, "stints", lambda: [("AAA", pd.Timestamp("2024-01-10"), pd.Timestamp("2024-01-20")),
                                                      ("BBB", EARLY, None)])
    idx = pd.bdate_range("2024-01-01", "2024-01-31")
    m = universe.members(idx, ["AAA", "BBB", "CCC"])
    assert m["AAA"].sum() == len(pd.bdate_range("2024-01-10", "2024-01-19"))
    assert not m.loc["2024-01-22", "AAA"] and m.loc["2024-01-19", "AAA"] and not m.loc["2024-01-09", "AAA"]
    assert m["BBB"].all() and not m["CCC"].any()
    assert universe.current() == ["BBB"]
    assert universe.symbols_since("2024-01-25") == ["BBB"]


def test_renames_follow_chains():
    assert universe._yahoo("hcp") == "DOC" and universe._yahoo("BRK.B") == "BRK-B" and universe._yahoo("AAPL") == "AAPL"


# ------------------------------------------------------------- S&P 500 background job

def _fake_universe(monkeypatch, data):
    syms = [s for s in data if s != "SPY"]
    monkeypatch.setattr(universe, "stints", lambda: [(s, EARLY, None) for s in syms])
    monkeypatch.setattr(jobs, "sp500_data", lambda: (syms, data))
    return syms


def _join():
    for th in list(jobs._threads):
        th.join(timeout=60)


def test_sp500_job_runs_in_background_then_serves_cache(monkeypatch):
    _fake_universe(monkeypatch, fake_market())
    r = jobs.sp500_backtest("leaders", 10)
    assert r["status"] == "running"
    _join()
    r = jobs.sp500_backtest("leaders", 10)
    assert r["status"] == "ready" and r["universe"] == "sp500" and r["point_in_time"]
    assert r["cost_per_side_pct"] == 0.1 and not r["refreshing"]
    n_threads = len(jobs._threads)
    assert jobs.sp500_backtest("vcp", 10)["status"] == "ready"  # one job fills every screen
    assert len(jobs._threads) == n_threads
    assert jobs.sp500_backtest("leaders", 25)["status"] == "running"  # a different cost is its own job
    _join()


def test_sp500_job_error_is_reported(monkeypatch):
    def boom():
        raise RuntimeError("no network")
    monkeypatch.setattr(jobs, "sp500_data", boom)
    assert jobs.sp500_backtest("leaders", 10)["status"] == "running"
    _join()
    r = jobs.sp500_backtest("leaders", 10)
    assert r["status"] == "error" and "retry" in r["error"]


# ------------------------------------------------------------- forward test

def _fake_feeds(monkeypatch, data, upto):
    """Serve the fake market as if today were `upto`."""
    cut = {s: d.loc[:upto] for s, d in data.items()}
    monkeypatch.setattr(market, "history", lambda s, p="1y": cut[s])
    monkeypatch.setattr(market, "bulk_history", lambda syms, p="1y": {s: cut[s] for s in syms if s in cut})
    monkeypatch.setattr(jobs, "_last_completed_session", lambda index: index[-1])
    syms = [s for s in data if s != "SPY"]
    monkeypatch.setattr(universe, "stints", lambda: [(s, EARLY, None) for s in syms])


def test_forward_record_is_idempotent(monkeypatch):
    data = fake_market()
    day = data["SPY"].index[650]
    _fake_feeds(monkeypatch, data, day)
    r1 = jobs.record_forward()
    assert r1 == {"status": "recorded", "pick_date": str(day.date()), "new": 4}
    n = db.one("SELECT COUNT(*) AS n FROM forward_picks")["n"]
    assert n > 0
    assert jobs.record_forward()["new"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM forward_picks")["n"] == n
    assert db.one("SELECT COUNT(*) AS n FROM forward_days")["n"] == 4  # every screen marked, even with no picks
    assert jobs.tick()["status"] == "recorded"


def test_forward_picks_are_scored_after_their_hold(monkeypatch):
    data = fake_market()
    idx = data["SPY"].index
    _fake_feeds(monkeypatch, data, idx[650])
    jobs.record_forward()
    before = jobs.forward_stats("leaders", 10)
    assert before["matured"] == 0 and before["pending"] > 0 and before["stats"] is None and before["message"]
    _fake_feeds(monkeypatch, data, idx[-1])  # 49 trading days later
    jobs._score_cache.clear()
    after = jobs.forward_stats("leaders", 10)
    assert after["matured"] == before["pending"] and after["pending"] == 0
    p = db.one("SELECT * FROM forward_picks WHERE kind='leaders' AND rank=1")
    gross = data[p["symbol"]]["Close"].iloc[650 + 20] / p["entry_price"] - 1
    row = next(r for r in after["recent"] if r["symbol"] == p["symbol"])
    assert row["ret_pct"] == pytest.approx(round(st.net_return(gross, 0.001) * 100, 2), abs=0.006)
    assert after["stats"]["picks"] == after["matured"] and after["started"] == str(idx[650].date())


def test_forward_api_and_tick(monkeypatch):
    c = appmod.app.test_client()
    appmod._hits.clear()
    assert c.get("/api/forward").status_code == 401
    monkeypatch.setattr(jobs, "tick", lambda: {"status": "recorded", "pick_date": "2026-11-25"})
    assert c.post("/api/forward/tick", json={}).json["status"] == "recorded"  # public, no login
    c.post("/api/auth/signup", json={"email": "fwd@test.local", "password": "correct horse 1"})
    monkeypatch.setattr(jobs, "forward_stats", lambda k, bps: {"kind": k, "bps": bps})
    r = c.get("/api/forward").json
    assert [s["kind"] for s in r["screens"]] == list(st.HOLD) and r["screens"][0]["bps"] == 10
    assert c.post("/api/settings", json={"cost_bps_per_side": 250}).status_code == 400
    assert c.post("/api/settings", json={"cost_bps_per_side": 5}).json["cost_bps_per_side"] == 5
    assert c.get("/api/forward?kind=vcp").json == {"kind": "vcp", "bps": 5}
    monkeypatch.setattr(jobs, "sp500_backtest", lambda k, bps: {"status": "running", "kind": k})
    assert c.post("/api/backtest", json={"kind": "leaders"}).json["status"] == "running"
    assert c.post("/api/backtest", json={"kind": "value"}).json["supported"] is False
