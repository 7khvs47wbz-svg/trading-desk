"""Background jobs that are too slow for a web request.

1. The point-in-time S&P 500 backtest: about 600 tickers (everyone who was in the index at any
   time in the last 5 years), so it runs in a thread and the result is stored in the shared
   app_cache table, where it survives restarts and the free plan's sleep. Recomputed weekly.
2. The live forward test: once per trading day each price screen's top 10 among the current
   S&P 500 members is written down with that day's closing price, before anyone knows how it
   turns out. GET /api/forward scores the picks whose holding period has passed.

Only one heavy job runs at a time, so the 512 MB server never holds two big downloads at once.
"""
import os
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import db
import market
import strategies
import universe

BT_TTL = 7 * 86400          # recompute the S&P 500 backtest weekly
ERROR_RETRY = 300           # after a failed job, wait 5 minutes before retrying
KICK_EVERY = 3 * 3600       # opportunistic forward-test check at most every 3 hours

_heavy = threading.Lock()   # one big download/compute at a time
_state_lock = threading.Lock()
_state = {}                 # job tag -> {"status": "running"|"error", ...}
_threads = []               # kept so tests can wait for them
_fwd = {"last_kick": 0.0}
_fwd_lock = threading.Lock()
_score_cache = {}


def _now():
    return datetime.now(timezone.utc)


def _age_s(iso):
    try:
        return (_now() - datetime.fromisoformat(iso)).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


def _start(target, *args):
    th = threading.Thread(target=target, args=args, daemon=True)
    _threads.append(th)
    del _threads[:-20]
    th.start()
    return th


# ---------------------------------------------------------------- S&P 500 backtest

def bt_key(kind, bps):
    return f"bt:sp500:{kind}:{float(bps):g}"


def sp500_data():
    """Every ticker that was an S&P 500 member at any point in the last 5 years, plus SPY."""
    start = pd.Timestamp.today().normalize() - pd.Timedelta(days=1830)
    syms = universe.symbols_since(start)
    return syms, market.bulk_history(syms + ["SPY"], "5y")


def _run_sp500(bps):
    tag = f"sp500:{float(bps):g}"
    try:
        with _heavy:
            syms, data = sp500_data()
            if "SPY" not in data:
                raise RuntimeError("Couldn't load SPY for the benchmark.")
            members = universe.members(data["SPY"].index, syms)
            for kind in strategies.HOLD:
                res = strategies.backtest(kind, data, members=members, cost_per_side=float(bps) / 10_000)
                res.update(universe="sp500", universe_label="S&P 500 members on each date",
                           symbols=len(syms), symbols_with_prices=len(data) - 1,
                           computed_at=_now().isoformat(timespec="seconds"))
                db.cache_set(bt_key(kind, bps), res)
            # the result is stored; free the ~600-ticker download so the 512 MB server keeps headroom
            market._cache.pop(("bulk", tuple(sorted(syms + ["SPY"])), "5y"), None)
            del data, members
        with _state_lock:
            _state.pop(tag, None)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        with _state_lock:
            _state[tag] = {"status": "error", "at": time.time(),
                           "error": f"The S&P 500 backtest failed ({type(e).__name__}). It will retry in a few minutes."}


def sp500_backtest(kind, bps):
    """Cached result if there is one (refreshed in the background when over a week old);
    otherwise {"status": "running"} while the job computes all four screens."""
    if kind not in strategies.HOLD:
        raise ValueError(f"unknown screen {kind}")
    tag = f"sp500:{float(bps):g}"
    hit = db.cache_get(bt_key(kind, bps))
    fresh = bool(hit) and _age_s(hit["updated_at"]) < BT_TTL
    with _state_lock:
        st = _state.get(tag)
        if st and st["status"] == "error" and time.time() - st["at"] > ERROR_RETRY:
            _state.pop(tag)
            st = None
        if st and st["status"] == "error" and not hit:
            return {"status": "error", "kind": kind, "error": st["error"]}
        if not fresh and not st:
            st = _state[tag] = {"status": "running", "started": time.time()}
            _start(_run_sp500, bps)
    if hit:
        return {"status": "ready", "refreshing": not fresh, "updated_at": hit["updated_at"], **hit["value"]}
    return {"status": "running", "kind": kind, "elapsed_s": int(time.time() - st.get("started", time.time())),
            "message": "Downloading 5 years of prices for every stock that was in the S&P 500, including "
                       "ones that later left it. This takes a minute or two the first time each week."}


# ---------------------------------------------------------------- forward test

def _last_completed_session(index):
    """The last trading day whose close is final (Yahoo includes today's bar while it forms)."""
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001  (no tz database: assume UTC-5)
        now_et = datetime.now(timezone(timedelta(hours=-5)))
    last = index[-1]
    if last.date() == now_et.date() and (now_et.hour, now_et.minute) < (16, 15):
        return index[-2]
    return last


def _missing_kinds(pick_date):
    done = {r["kind"] for r in db.rows("SELECT kind FROM forward_days WHERE pick_date=?", (pick_date,))}
    return [k for k in strategies.HOLD if k not in done]


def pending_date():
    """(pick_date, kinds still to record) using only the cheap SPY history."""
    d = _last_completed_session(market.history("SPY", "2y").index)
    pick_date = str(d.date())
    return pick_date, _missing_kinds(pick_date)


def record_forward():
    """Write down today's picks for every screen not yet recorded. Idempotent."""
    if not _fwd_lock.acquire(blocking=False):
        return {"status": "running"}
    try:
        with _heavy:
            spy_full = market.history("SPY", "2y")
            d = _last_completed_session(spy_full.index)
            pick_date = str(d.date())
            todo = _missing_kinds(pick_date)
            if not todo:
                return {"status": "recorded", "pick_date": pick_date, "new": 0}
            syms = universe.current()
            data = market.bulk_history(syms + ["SPY"], "2y")
            spy = data.get("SPY", spy_full).loc[:d]
            # only stocks with a bar on the pick date (no stale prices) and a year of history
            stocks = {s: v.loc[:d] for s, v in data.items()
                      if s != "SPY" and d in v.index and len(v.loc[:d]) > 260}
            ind = strategies.indicators(stocks, spy)
            t = len(ind["C"]) - 1
            spy_entry = float(spy["Close"].iloc[-1])
            now = _now().isoformat(timespec="seconds")
            for kind in todo:
                ps = strategies.pick(kind, ind, t)
                for rank, sym in enumerate(ps, 1):
                    db.execute("INSERT INTO forward_picks (kind, pick_date, symbol, rank, entry_price, spy_entry, "
                               "recorded_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT (kind, pick_date, symbol) DO NOTHING",
                               (kind, pick_date, sym, rank, float(stocks[sym]["Close"].iloc[-1]), spy_entry, now))
                note = None
                if not ps:
                    note = ("SPY was below its 200-day average, so the screen stood aside."
                            if kind == "pullback" and not bool(ind["market_up"].iloc[-1]) else "No stock passed the screen.")
                db.execute("INSERT INTO forward_days (kind, pick_date, n, members, note, recorded_at) "
                           "VALUES (?,?,?,?,?,?) ON CONFLICT (kind, pick_date) DO NOTHING",
                           (kind, pick_date, len(ps), len(stocks), note, now))
            _score_cache.clear()
            return {"status": "recorded", "pick_date": pick_date, "new": len(todo)}
    finally:
        _fwd_lock.release()


def _record_safely():
    try:
        record_forward()
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def tick():
    """Start recording in the background if today's picks are missing."""
    if _fwd_lock.locked():
        return {"status": "running"}
    pick_date, todo = pending_date()
    if not todo:
        return {"status": "recorded", "pick_date": pick_date}
    _fwd["last_kick"] = time.time()
    _start(_record_safely)
    return {"status": "started", "pick_date": pick_date, "screens": todo}


def kick_forward():
    """Called on signed-in requests: at most every few hours, record if anything is missing."""
    if os.environ.get("FORWARD_AUTOKICK", "1") == "0" or time.time() - _fwd["last_kick"] < KICK_EVERY:
        return
    _fwd["last_kick"] = time.time()

    def go():
        try:
            if pending_date()[1]:
                record_forward()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    _start(go)


def forward_stats(kind, bps):
    """Score the recorded picks: those past their holding period count; the rest are pending."""
    if kind not in strategies.HOLD:
        raise ValueError(f"unknown screen {kind}")
    days = db.rows("SELECT pick_date, n, note FROM forward_days WHERE kind=? ORDER BY pick_date", (kind,))
    picks = db.rows("SELECT pick_date, symbol, rank, entry_price, spy_entry FROM forward_picks WHERE kind=? "
                    "ORDER BY pick_date, rank", (kind,))
    hold = strategies.HOLD[kind]
    out = {"kind": kind, "name": strategies.NAMES[kind], "holding_days": hold, "top_n": strategies.TOP_N,
           "cost_per_side_pct": round(float(bps) / 100, 3), "universe_label": "S&P 500 members on the day",
           "started": days[0]["pick_date"] if days else None, "days_recorded": len(days),
           "picks_recorded": len(picks), "matured": 0, "pending": 0, "stats": None, "recent": [],
           "last_note": days[-1]["note"] if days else None}
    if not picks:
        out["message"] = ("Recording starts with the next market close." if not days else
                          "No picks yet (the screen hasn't found anything on the days recorded so far).")
        return out
    key = (kind, float(bps), len(picks), str(pd.Timestamp.today().date()))
    if key in _score_cache:
        return _score_cache[key]

    first = pd.Timestamp(picks[0]["pick_date"])
    period = "2y" if first > pd.Timestamp.today() - pd.Timedelta(days=600) else "5y"
    members_now = universe.current()
    data = market.bulk_history(members_now + ["SPY"], period)
    leavers = sorted({p["symbol"] for p in picks} - set(data))
    if leavers:
        data = {**data, **market.bulk_history(leavers, period)}
    spy = data["SPY"]["Close"]
    idx = spy.index
    C = pd.DataFrame({s: d["Close"] for s, d in data.items() if s != "SPY"}).reindex(idx)
    Cx = C.ffill()
    cost = float(bps) / 10_000
    base_cache = {}

    def baseline(i0):
        """Equal-weight return of every stock in the index on that date, over the same hold."""
        if i0 not in base_cache:
            day = idx[i0]
            mem = universe.members(pd.DatetimeIndex([day]), list(C.columns)).iloc[0]
            c0, c1 = C.iloc[i0][mem.to_numpy()], Cx.iloc[i0 + hold][mem.to_numpy()]
            ok = c0.notna() & c1.notna()
            base_cache[i0] = float((c1[ok] / c0[ok] - 1).mean())
        return base_cache[i0]

    rows, matured = [], []
    for p in picks:
        i0 = int(idx.searchsorted(pd.Timestamp(p["pick_date"])))
        if i0 >= len(idx):
            continue
        done = i0 + hold < len(idx)
        i1 = i0 + hold if done else len(idx) - 1
        px = Cx[p["symbol"]].iloc[i1] if p["symbol"] in Cx else np.nan
        if pd.isna(px) or not p["entry_price"]:
            continue
        gross = float(px / p["entry_price"] - 1)
        net = strategies.net_return(gross, cost) if done else gross
        spr = float(spy.iloc[i1] / p["spy_entry"] - 1) if p["spy_entry"] else None
        row = {"date": p["pick_date"], "symbol": p["symbol"], "rank": p["rank"], "entry": round(p["entry_price"], 2),
               "status": "done" if done else "pending", "ret_pct": round(net * 100, 2),
               "vs_spy_pct": round((net - spr) * 100, 2) if spr is not None else None,
               "exit_date": str(idx[i0 + hold].date()) if done else None}
        if done:
            b = baseline(i0)
            row["vs_all_pct"] = round((net - b) * 100, 2)
            matured.append({"net": net, "gross": gross, "spy": spr, "base": b, "date": p["pick_date"]})
        rows.append(row)
    out["matured"] = len(matured)
    out["pending"] = len(rows) - len(matured)
    out["recent"] = sorted(rows, key=lambda r: (r["date"], -r["rank"]), reverse=True)[:30]
    if matured:
        M = pd.DataFrame(matured)
        out["stats"] = {
            "picks": len(M), "days": int(M.date.nunique()), "from": M.date.min(), "to": M.date.max(),
            "avg_return_pct": round(M.net.mean() * 100, 2), "avg_return_gross_pct": round(M.gross.mean() * 100, 2),
            "avg_vs_spy_pct": round((M.net - M.spy).mean() * 100, 2),
            "avg_vs_all_pct": round((M.net - M.base).mean() * 100, 2),
            "win_rate_pct": round(float((M.net > 0).mean() * 100), 1),
            "beat_spy_pct": round(float(((M.net - M.spy) > 0).mean() * 100), 1),
        }
    else:
        first_due = pd.bdate_range(first, periods=hold + 1)[-1]  # weekdays; holidays can add a day or two
        out["message"] = (f"The first picks finish their {hold}-day hold about {hold} trading days after "
                          f"{picks[0]['pick_date']}. Results appear here then.")
        out["first_due_after"] = str(first_due.date())
    _score_cache[key] = out
    return out
