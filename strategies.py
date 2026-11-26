"""Price-only screens as vectorised rules, shared by the live screener and the backtest
so both run exactly the same logic.

Every rule at row t only reads rows <= t (rolling windows and shifts look back, never
forward), so a backtest built on them has no look-ahead. Which rules made the cut was
decided in a 5-year study: rules were kept only if they beat an "own every stock in the
list" baseline in BOTH the older half and the newer half of the data.
"""
import math

import numpy as np
import pandas as pd

# holding period (trading days) each screen is designed and tested for
HOLD = {"vcp": 20, "momentum": 20, "leaders": 20, "pullback": 5}
TOP_N = 10
STEP = 5          # backtest re-runs the screen every 5 trading days
START = 260       # first backtest date needs ~1 year of history for the 200-day/12-month rules

NAMES = {
    "vcp": "Trend template + VCP",
    "momentum": "Momentum burst",
    "leaders": "Momentum leaders (12-1)",
    "pullback": "Uptrend pullback (RSI-2)",
}


def _frame(data, field, index):
    return pd.DataFrame({s: d[field] for s, d in data.items()}).reindex(index).ffill(limit=5)


def indicators(stocks, spy):
    """stocks: {symbol: OHLCV DataFrame}; spy: SPY OHLCV DataFrame (defines the trading calendar)."""
    idx = spy.index
    C, H, L, V = (_frame(stocks, f, idx) for f in ("Close", "High", "Low", "Volume"))
    s50, s150, s200 = (C.rolling(n).mean() for n in (50, 150, 200))
    ret = lambda n: C / C.shift(n) - 1  # noqa: E731
    rs_raw = 0.4 * ret(63) + 0.2 * ret(126) + 0.4 * ret(252)
    rs = (rs_raw.rank(axis=1, pct=True) * 98 + 1).round()
    hi252, lo252 = H.rolling(252, min_periods=200).max(), L.rolling(252, min_periods=200).min()

    # VCP: three successive 20-bar pullback depths, oldest first, should shrink
    hi20, lo20 = H.rolling(20).max(), L.rolling(20).min()
    depth = (hi20 - lo20) / hi20 * 100
    d0, d1, d2 = depth.shift(40), depth.shift(20), depth
    checks = ((C > s50) & (s50 > s150) & (s150 > s200)).astype(int) \
        + (s200 > s200.shift(21)).astype(int) + (C >= 0.75 * hi252).astype(int) \
        + (C >= 1.3 * lo252).astype(int) + (rs >= 70).astype(int)
    contracting = (d0 > d1) & (d1 > d2)
    vol_dry = V.rolling(10).mean() < V.rolling(50).mean()
    vcp_score = (checks * 15 + contracting * 15 + vol_dry * 10).where(s200.notna() & (checks >= 4))

    # momentum burst: +4% day on rising volume
    chg = C / C.shift(1) - 1
    burst = chg.where((chg >= 0.04) & (V > V.shift(1)) & (V > 100_000))

    # momentum leaders: 12-month return skipping the latest month
    mom12_1 = C.shift(21) / C.shift(252) - 1

    # uptrend pullback: 2-day RSI washed out while above the 200-day, market above its 200-day
    diff = C.diff()
    up = diff.clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    dn = (-diff.clip(upper=0)).ewm(alpha=0.5, adjust=False).mean()
    rsi2 = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    spy_c = spy["Close"]
    market_up = spy_c > spy_c.rolling(200).mean()
    pull = rsi2.where((C > s200) & (rsi2 < 10)).where(market_up, axis=0)

    prev = C.shift()
    tr = np.maximum(H - L, np.maximum((H - prev).abs(), (L - prev).abs()))
    return {"C": C, "H": H, "L": L, "V": V, "s50": s50, "s150": s150, "s200": s200, "rs": rs,
            "hi252": hi252, "lo252": lo252, "d0": d0, "d1": d1, "d2": d2, "contracting": contracting,
            "vol_dry": vol_dry, "checks": checks, "vcp_score": vcp_score, "burst": burst, "chg": chg,
            "mom12_1": mom12_1, "rsi2": rsi2, "pull": pull, "market_up": market_up,
            "atr": tr.rolling(14).mean(), "sma5": C.rolling(5).mean(), "spy": spy_c}


def pick(kind, ind, t, n=TOP_N):
    """Ordered list of the screen's top symbols at row t, using only data up to t."""
    if kind == "vcp":
        sc = ind["vcp_score"].iloc[t].dropna()
        if sc.empty:
            return []
        # strongest relative strength first; the VCP score breaks ties (ranking by score alone
        # did not hold up on the newer half of the 5-year test, ranking by RS did)
        df = pd.DataFrame({"s": sc, "rs": ind["rs"].iloc[t][sc.index].fillna(0)})
        return list(df.sort_values(["rs", "s"], ascending=False).index[:n])
    if kind == "momentum":
        return list(ind["burst"].iloc[t].dropna().sort_values(ascending=False).index[:n])
    if kind == "leaders":  # strongest 12-1 momentum among stocks still within 25% of their high
        m = ind["mom12_1"].iloc[t]
        ok = ind["C"].iloc[t] >= 0.75 * ind["hi252"].iloc[t]
        return list(m[ok].dropna().sort_values(ascending=False).index[:n])
    if kind == "pullback":
        return list(ind["pull"].iloc[t].dropna().sort_values().index[:n])
    raise ValueError(f"unknown screen {kind}")


def _r(x, d=2):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), d)


def live(kind, stocks, spy):
    """Today's picks with the detail the screener table shows."""
    ind = indicators(stocks, spy)
    t = len(ind["C"]) - 1
    out = []
    for s in pick(kind, ind, t, n=25):
        g = lambda k: ind[k][s].iloc[t]  # noqa: E731
        price, atr = float(g("C")), _r(g("atr"))
        row = {"symbol": s, "price": _r(price), "rs": int(g("rs")) if not math.isnan(g("rs")) else None,
               "atr": atr, "hold_days": HOLD[kind]}
        if kind == "leaders":
            row.update(mom_12_1_pct=_r(g("mom12_1") * 100, 1),
                       from_high_pct=_r((price / g("hi252") - 1) * 100, 1),
                       above_200_pct=_r((price / g("s200") - 1) * 100, 1),
                       suggested_stop=_r(price - 2 * atr) if atr else None)
        elif kind == "pullback":
            row.update(rsi2=_r(g("rsi2"), 1), above_200_pct=_r((price / g("s200") - 1) * 100, 1),
                       suggested_stop=_r(price - 2.5 * atr) if atr else None,
                       suggested_target=_r(g("sma5")))
        out.append(row)
    note = None
    if kind == "pullback" and not bool(ind["market_up"].iloc[-1]):
        note = "SPY is below its 200-day average, so this screen stands aside (it only buys dips in a rising market)."
    return out, note


def backtest(kind, data):
    """Walk-forward basket test. data must include 'SPY'.

    Every STEP days the screen picks its top 10 using only data known that day; the basket is
    held HOLD[kind] days. It is compared with SPY and with an 'own everything' baseline (equal
    weight in every stock in the list), which removes the head start a list of today's big
    companies has. The period is split in half: the edge must hold up in the newer half too.
    """
    spy = data["SPY"]
    stocks = {s: d for s, d in data.items() if s != "SPY"}
    ind = indicators(stocks, spy)
    C, S = ind["C"], spy["Close"]
    hold, idx = HOLD[kind], C.index
    periods, picks = [], []
    for t in range(START, len(idx) - hold, STEP):
        c0, c1 = C.iloc[t], C.iloc[t + hold]
        avail = c0.notna() & c1.notna()
        base = float((c1[avail] / c0[avail] - 1).mean())
        spr = float(S.iloc[t + hold] / S.iloc[t] - 1)
        ps = [p for p in pick(kind, ind, t) if avail.get(p, False)]
        rets = (c1[ps] / c0[ps] - 1) if ps else pd.Series(dtype=float)
        periods.append({"t": t, "date": str(idx[t].date()), "n": len(ps),
                        "ret": float(rets.mean()) if ps else 0.0, "spy": spr, "base": base})
        for p in ps:
            picks.append({"date": str(idx[t].date()), "symbol": p, "ret": float(rets[p]),
                          "excess": float(rets[p]) - spr})
    if not periods:
        return {"kind": kind, "supported": True, "picks": 0}
    P = pd.DataFrame(periods)
    mid = START + (len(idx) - hold - START) // 2

    def stats(sub, pk):
        act = sub[sub.n > 0]
        if act.empty:
            return {"periods": 0}
        pk = pd.DataFrame(pk) if pk else pd.DataFrame(columns=["ret", "excess"])
        return {
            "from": act.date.iloc[0], "to": act.date.iloc[-1], "periods": int(len(act)), "picks": int(act.n.sum()),
            "avg_return_pct": round(act.ret.mean() * 100, 2),
            "avg_vs_spy_pct": round((act.ret - act.spy).mean() * 100, 2),
            "avg_vs_all_pct": round((act.ret - act.base).mean() * 100, 2),
            "basket_beat_spy_pct": round(((act.ret - act.spy) > 0).mean() * 100, 1),
            "pick_win_rate_pct": round(float((pk.ret > 0).mean() * 100), 1) if len(pk) else None,
            "worst_basket_pct": round(act.ret.min() * 100, 2), "best_basket_pct": round(act.ret.max() * 100, 2),
        }

    first, second = P[P.t < mid], P[P.t >= mid]
    split = idx[mid].date().isoformat()
    whole = stats(P, picks)
    older = stats(first, [p for p in picks if p["date"] < split])
    newer = stats(second, [p for p in picks if p["date"] >= split])

    # non-overlapping growth of $1 (cash earns nothing on dates the screen has no picks)
    curve, g_s, g_spy, g_all = [], 1.0, 1.0, 1.0
    for _, r in P[(P.t - START) % hold == 0].iterrows():
        g_s *= 1 + r.ret
        g_spy *= 1 + r.spy
        g_all *= 1 + r.base
        curve.append({"t": r.date, "screen": round(g_s, 4), "spy": round(g_spy, 4), "all": round(g_all, 4)})

    holds_up = bool(older.get("periods") and newer.get("periods")
                    and older["avg_vs_all_pct"] > 0 and newer["avg_vs_all_pct"] > 0)
    return {"kind": kind, "name": NAMES[kind], "supported": True, "picks": len(picks),
            "holding_days": hold, "step_days": STEP, "top_n": TOP_N, "split_date": split,
            "overall": whole, "older": older, "newer": newer, "holds_up": holds_up,
            "curve": curve, "recent": sorted(picks, key=lambda p: p["date"], reverse=True)[:15]}
