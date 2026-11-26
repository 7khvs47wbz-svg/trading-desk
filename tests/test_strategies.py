"""Screen rules: no look-ahead, hand-checkable backtest maths, and the honest baseline."""
import numpy as np
import pandas as pd
import pytest

import strategies as st


def fake_market(n_sym=30, n_days=700, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2021-01-04", periods=n_days)
    data = {}
    for k in range(n_sym + 1):
        drift = 0.0004 + 0.0002 * (k % 5)
        c = 50 * np.exp(np.cumsum(rng.normal(drift, 0.02, n_days)))
        hi, lo = c * (1 + rng.uniform(0, 0.02, n_days)), c * (1 - rng.uniform(0, 0.02, n_days))
        vol = rng.integers(200_000, 2_000_000, n_days).astype(float)
        name = "SPY" if k == n_sym else f"S{k:02d}"
        data[name] = pd.DataFrame({"Open": c, "High": hi, "Low": lo, "Close": c, "Volume": vol}, index=idx)
    return data


@pytest.mark.parametrize("kind", list(st.HOLD))
def test_picks_ignore_the_future(kind):
    """Scrambling every price after day t must not change what the screen picks on day t."""
    data = fake_market()
    spy = data["SPY"]
    stocks = {s: d for s, d in data.items() if s != "SPY"}
    t = 500
    before = [st.pick(kind, st.indicators(stocks, spy), tt) for tt in (t - 5, t)]
    rng = np.random.default_rng(9)
    scrambled = {s: d.copy() for s, d in stocks.items()}
    for d in scrambled.values():
        d.iloc[t + 1:, :4] *= rng.uniform(0.5, 1.5, (len(d) - t - 1, 1))
    spy2 = spy.copy()
    spy2.iloc[t + 1:, :4] *= 0.7
    after = [st.pick(kind, st.indicators(scrambled, spy2), tt) for tt in (t - 5, t)]
    assert before == after


def test_backtest_returns_and_baseline_are_exact():
    data = fake_market()
    bt = st.backtest("leaders", data)
    assert bt["picks"] > 0 and bt["holding_days"] == 20
    C = pd.DataFrame({s: d["Close"] for s, d in data.items() if s != "SPY"})
    spy = data["SPY"]["Close"]
    p = bt["recent"][3]
    t0 = C.index.get_loc(pd.Timestamp(p["date"]))
    ret = C[p["symbol"]].iloc[t0 + 20] / C[p["symbol"]].iloc[t0] - 1
    assert p["ret"] == pytest.approx(ret, rel=1e-12)
    assert p["excess"] == pytest.approx(ret - (spy.iloc[t0 + 20] / spy.iloc[t0] - 1), rel=1e-12)
    # the 'own everything' curve compounds the equal-weight return of every stock
    first = bt["curve"][0]
    t = C.index.get_loc(pd.Timestamp(first["t"]))
    base = (C.iloc[t + 20] / C.iloc[t] - 1).mean()
    assert first["all"] == pytest.approx(1 + base, abs=1e-4)
    assert set(bt) >= {"older", "newer", "holds_up", "split_date"}
    assert bt["older"]["to"] < bt["split_date"] <= bt["newer"]["from"]


def test_leaders_rank_by_12_1_momentum_and_skip_broken_stocks():
    data = fake_market()
    spy = data["SPY"]
    stocks = {s: d for s, d in data.items() if s != "SPY"}
    ind = st.indicators(stocks, spy)
    t = len(ind["C"]) - 1
    picks = st.pick("leaders", ind, t)
    m = ind["mom12_1"].iloc[t][picks]
    assert list(m) == sorted(m, reverse=True)
    assert all(ind["C"].iloc[t][s] >= 0.75 * ind["hi252"].iloc[t][s] for s in picks)


def test_pullback_stands_aside_when_market_is_below_200_day():
    data = fake_market()
    spy = data["SPY"].copy()
    spy.iloc[-60:, :4] *= np.linspace(1, 0.5, 60)[:, None]  # crash SPY under its 200-day
    stocks = {s: d for s, d in data.items() if s != "SPY"}
    rows, note = st.live("pullback", stocks, spy)
    assert rows == [] and "stands aside" in note
