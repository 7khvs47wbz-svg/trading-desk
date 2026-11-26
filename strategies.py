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


# which indicator groups each screen needs (the backtest computes only these to save memory)
NEEDS = {"vcp": {"vcp"}, "momentum": {"burst"}, "leaders": {"leaders"}, "pullback": {"pull"}}


def indicators(stocks, spy, member=None, kinds=None):
    """stocks: {symbol: OHLCV DataFrame}; spy: SPY OHLCV DataFrame (defines the trading calendar).

    member: optional bool frame (dates x symbols), True while the stock was in the index. When
    given, relative-strength ranks and every pick only consider members on that date.
    kinds: screens to prepare for (default all, plus the extra columns the live table shows).
    """
    idx = spy.index
    groups = set().union(*(NEEDS[k] for k in kinds)) if kinds else {"vcp", "burst", "leaders", "pull", "live"}
    C = _frame(stocks, "Close", idx).astype("float32")
    if member is not None:
        member = member.reindex(index=idx, columns=C.columns, fill_value=False)
    out = {"C": C, "member": member, "spy": spy["Close"]}
    H = _frame(stocks, "High", idx).astype("float32") if groups & {"vcp", "leaders", "live"} else None
    s200 = C.rolling(200).mean() if groups & {"vcp", "pull", "live"} else None
    if groups & {"vcp", "leaders", "live"}:
        out["hi252"] = H.rolling(252, min_periods=200).max()
    if groups & {"vcp", "live"}:
        ret = lambda n: C / C.shift(n) - 1  # noqa: E731
        rs_raw = 0.4 * ret(63) + 0.2 * ret(126) + 0.4 * ret(252)
        if member is not None:
            rs_raw = rs_raw.where(member)
        out["rs"] = (rs_raw.rank(axis=1, pct=True) * 98 + 1).round()
        del rs_raw
    if "vcp" in groups:
        L, V = _frame(stocks, "Low", idx).astype("float32"), _frame(stocks, "Volume", idx).astype("float32")
        s50, s150 = C.rolling(50).mean(), C.rolling(150).mean()
        lo252 = L.rolling(252, min_periods=200).min()
        # three successive 20-bar pullback depths, oldest first, should shrink
        hi20, lo20 = H.rolling(20).max(), L.rolling(20).min()
        depth = (hi20 - lo20) / hi20 * 100
        d0, d1, d2 = depth.shift(40), depth.shift(20), depth
        checks = ((C > s50) & (s50 > s150) & (s150 > s200)).astype("int8") \
            + (s200 > s200.shift(21)).astype("int8") + (C >= 0.75 * out["hi252"]).astype("int8") \
            + (C >= 1.3 * lo252).astype("int8") + (out["rs"] >= 70).astype("int8")
        contracting = (d0 > d1) & (d1 > d2)
        vol_dry = V.rolling(10).mean() < V.rolling(50).mean()
        out["vcp_score"] = (checks * 15 + contracting * 15 + vol_dry * 10).where(s200.notna() & (checks >= 4))
        if "live" in groups:
            out.update(s50=s50, s150=s150, lo252=lo252, d0=d0, d1=d1, d2=d2, contracting=contracting,
                       vol_dry=vol_dry, checks=checks)
        del L, s50, s150, lo252, hi20, lo20, depth, d0, d1, d2, checks, contracting
        out["V"] = V
    if "burst" in groups:
        V = out.get("V")
        if V is None:
            V = _frame(stocks, "Volume", idx).astype("float32")
        chg = C / C.shift(1) - 1
        out["burst"] = chg.where((chg >= 0.04) & (V > V.shift(1)) & (V > 100_000))
        if "live" in groups:
            out["chg"] = chg
    if groups & {"leaders", "live"}:
        out["mom12_1"] = C.shift(21) / C.shift(252) - 1
    if groups & {"pull", "live"}:
        diff = C.diff()
        up = diff.clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
        dn = (-diff.clip(upper=0)).ewm(alpha=0.5, adjust=False).mean()
        rsi2 = 100 - 100 / (1 + up / dn.replace(0, np.nan))
        spy_c = spy["Close"]
        market_up = spy_c > spy_c.rolling(200).mean()
        out.update(rsi2=rsi2, market_up=market_up,
                   pull=rsi2.where((C > s200) & (rsi2 < 10)).where(market_up, axis=0))
    if "live" in groups:
        L = _frame(stocks, "Low", idx).astype("float32")
        prev = C.shift()
        tr = np.maximum(H - L, np.maximum((H - prev).abs(), (L - prev).abs()))
        out.update(H=H, L=L, s200=s200, atr=tr.rolling(14).mean(), sma5=C.rolling(5).mean(),
                   C=_frame(stocks, "Close", idx))  # full precision for displayed prices
    return out


def _members_only(ind, t, x):
    m = ind.get("member")
    if m is None:
        return x
    return x[m.iloc[t].reindex(x.index, fill_value=False).to_numpy(dtype=bool)]


def pick(kind, ind, t, n=TOP_N):
    """Ordered list of the screen's top symbols at row t, using only data up to t."""
    if kind == "vcp":
        sc = _members_only(ind, t, ind["vcp_score"].iloc[t].dropna())
        if sc.empty:
            return []
        # strongest relative strength first; the VCP score breaks ties (ranking by score alone
        # did not hold up on the newer half of the 5-year test, ranking by RS did)
        df = pd.DataFrame({"s": sc, "rs": ind["rs"].iloc[t][sc.index].fillna(0)})
        return list(df.sort_values(["rs", "s"], ascending=False).index[:n])
    if kind == "momentum":
        return list(_members_only(ind, t, ind["burst"].iloc[t].dropna()).sort_values(ascending=False).index[:n])
    if kind == "leaders":  # strongest 12-1 momentum among stocks still within 25% of their high
        m = ind["mom12_1"].iloc[t]
        ok = ind["C"].iloc[t] >= 0.75 * ind["hi252"].iloc[t]
        return list(_members_only(ind, t, m[ok].dropna()).sort_values(ascending=False).index[:n])
    if kind == "pullback":
        return list(_members_only(ind, t, ind["pull"].iloc[t].dropna()).sort_values().index[:n])
    raise ValueError(f"unknown screen {kind}")


def _r(x, d=2):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), d)


def live(kind, stocks, spy):
    """Today's picks with the detail the screener table shows."""
    ind = indicators(stocks, spy)
    t = len(ind["C"]) - 1
    out = []
    for s in pick(kind, ind, t, n=25):
        g = lambda k: float(ind[k][s].iloc[t])  # noqa: E731
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


def net_return(r, cost_per_side):
    """Return after paying `cost_per_side` (a fraction) on the buy and again on the sell."""
    return (1 + r) * (1 - cost_per_side) / (1 + cost_per_side) - 1


def backtest(kind, data, members=None, cost_per_side=0.0):
    """Walk-forward basket test. data must include 'SPY'.

    Every STEP days the screen picks its top 10 using only data known that day; the basket is
    held HOLD[kind] days. It is compared with SPY and with an 'own everything' baseline (equal
    weight in every stock available that day), which removes the head start a list of today's
    big companies has. The period is split in half: the edge must hold up in the newer half too.

    members: optional bool frame (dates x symbols) of index membership. When given, only stocks
    in the index on the signal date can be picked or count in the baseline, including ones that
    later left it. A stock whose prices stop during the hold (acquired, delisted) is sold at its
    last price. cost_per_side: trading cost charged on entry and exit of every pick (e.g. 0.001).
    SPY and the baseline are passive and carry no cost, which is the conservative choice.
    """
    spy = data["SPY"]
    stocks = {s: d for s, d in data.items() if s != "SPY"}
    ind = indicators(stocks, spy, member=members, kinds=[kind])
    S, M = spy["Close"], ind["member"]
    C = _frame(stocks, "Close", spy.index)  # float64 for returns (signals use float32 to save memory)
    Cx = C.ffill()  # exit price: last known price if trading stopped during the hold
    hold, idx = HOLD[kind], C.index
    periods, picks, coverage = [], [], []
    for t in range(START, len(idx) - hold, STEP):
        c0, c1 = C.iloc[t], Cx.iloc[t + hold]
        avail = c0.notna() & c1.notna()
        if M is not None:
            mt = M.iloc[t].to_numpy(dtype=bool)
            # share of that day's index members we have a price for (members with no data at all
            # are in `members` but not in `data`, so count against the full membership)
            n_members = int(members.loc[idx[t]].sum()) if idx[t] in members.index else int(mt.sum())
            coverage.append(float(c0.notna().to_numpy()[mt].sum() / max(n_members, 1)))
            avail = avail & mt
        base = float((c1[avail] / c0[avail] - 1).mean())
        spr = float(S.iloc[t + hold] / S.iloc[t] - 1)
        ps = [p for p in pick(kind, ind, t) if bool(avail.get(p, False))]
        gross = (c1[ps] / c0[ps] - 1).astype(float) if ps else pd.Series(dtype=float)
        net = net_return(gross, cost_per_side)
        periods.append({"t": t, "date": str(idx[t].date()), "n": len(ps),
                        "ret": float(net.mean()) if ps else 0.0, "gross": float(gross.mean()) if ps else 0.0,
                        "spy": spr, "base": base})
        for p in ps:
            picks.append({"date": str(idx[t].date()), "symbol": p, "ret": float(net[p]),
                          "ret_gross": float(gross[p]), "excess": float(net[p]) - spr})
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
            "avg_return_gross_pct": round(act.gross.mean() * 100, 2),
            "cost_drag_pct": round((act.gross - act.ret).mean() * 100, 2),
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
    cov = None
    if coverage:
        cov = {"mean_pct": round(float(np.mean(coverage)) * 100, 1), "min_pct": round(float(np.min(coverage)) * 100, 1),
               "latest_pct": round(coverage[-1] * 100, 1)}
    return {"kind": kind, "name": NAMES[kind], "supported": True, "picks": len(picks),
            "holding_days": hold, "step_days": STEP, "top_n": TOP_N, "split_date": split,
            "cost_per_side_pct": round(cost_per_side * 100, 3), "point_in_time": M is not None,
            "coverage": cov, "overall": whole, "older": older, "newer": newer, "holds_up": holds_up,
            "curve": curve, "recent": sorted(picks, key=lambda p: p["date"], reverse=True)[:15]}
