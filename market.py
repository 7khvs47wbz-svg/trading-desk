"""Free delayed market data (Yahoo Finance via yfinance) and built-in screens.

The built-in screens follow the methodology of the repo's screener skills
(vcp-screener, stockbee-momentum-burst-screener, value-dividend-screener,
canslim-screener) but run on free data so no FMP key is needed.
"""
import math
import re
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pandas as pd
import requests
import yfinance as yf

import strategies

warnings.filterwarnings("ignore")

_cache = {}
_locks = {}
_locks_guard = threading.Lock()
TTL = 15 * 60
MAX_DEVIATION_PCT = 2.0  # largest allowed gap between the two price sources
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def clean_symbol(s):
    s = (s or "").strip().upper()
    if not SYMBOL_RE.match(s):
        raise ValueError(f"'{s[:12]}' is not a valid ticker symbol.")
    return s


def _cached(key, fn, ttl=TTL):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:  # one download per key even when many users ask at once
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = fn()
        _cache[key] = (time.time(), val)
        if len(_cache) > 4000:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:1000]:
                _cache.pop(k, None)
        return val


# ---------------------------------------------------------------- Nasdaq (second source)

_NQ = requests.Session()
_NQ.headers.update({"User-Agent": "Mozilla/5.0 (TradingDesk)", "Accept": "application/json"})


def _money(x):
    try:
        return float(str(x).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None


def _nasdaq(path, symbol, params=None):
    for cls in ("stocks", "etf"):
        r = _NQ.get(f"https://api.nasdaq.com/api/quote/{symbol}/{path}",
                    params={"assetclass": cls, **(params or {})}, timeout=10)
        d = (r.json() or {}).get("data") if r.ok else None
        if d:
            return d
    return None


def nasdaq_price(symbol):
    """Second-source price from Nasdaq. While the market is open: the live last sale.
    Otherwise: the official consolidated close from Nasdaq's daily history (its
    'closed at 4:00 PM' quote field is the last Nasdaq-venue trade, which can differ)."""
    def load():
        d = _nasdaq("info", symbol)
        status = (d or {}).get("marketStatus") or ""
        if d and status.lower() == "open":
            prim = d.get("primaryData") or {}
            return {"price": _money(prim.get("lastSalePrice")), "time": prim.get("lastTradeTimestamp"),
                    "status": status}
        h = _nasdaq("historical", symbol, {"fromdate": str(date.today() - timedelta(days=10)),
                                            "todate": str(date.today()), "limit": 5})
        rows = ((h or {}).get("tradesTable") or {}).get("rows") or []
        if not rows:
            return None
        return {"price": _money(rows[0]["close"]), "time": f"Official close {rows[0]['date']}",
                "status": status or "Closed"}
    try:
        return _cached(("nq", symbol), load, ttl=60)
    except Exception:  # noqa: BLE001
        return None


def nasdaq_history(symbol, days=730):
    def load():
        d = _nasdaq("historical", symbol, {"fromdate": str(date.today() - timedelta(days=days)),
                                            "todate": str(date.today()), "limit": 9999})
        rows = ((d or {}).get("tradesTable") or {}).get("rows") or []
        if not rows:
            raise ValueError(f"No price data for {symbol}")
        df = pd.DataFrame({
            "Open": [_money(r["open"]) for r in rows], "High": [_money(r["high"]) for r in rows],
            "Low": [_money(r["low"]) for r in rows], "Close": [_money(r["close"]) for r in rows],
            "Volume": [_money(r["volume"]) or 0 for r in rows]},
            index=pd.to_datetime([r["date"] for r in rows], format="%m/%d/%Y"))
        return df.sort_index().dropna(subset=["Close"])
    return _cached(("nqhist", symbol, days), load, ttl=TTL)


def verify_price(symbol, price):
    """Cross-check a Yahoo price against Nasdaq. ok=False when they differ by more than 2%."""
    nq = nasdaq_price(symbol)
    if not nq or not nq.get("price"):
        return {"ok": None, "second_source": None, "deviation_pct": None, "max_pct": MAX_DEVIATION_PCT,
                "message": "Second source (Nasdaq) had no price for this ticker, so it could not be cross-checked."}
    dev = abs(price / nq["price"] - 1) * 100
    ok = dev <= MAX_DEVIATION_PCT
    return {"ok": ok, "second_source": round(nq["price"], 2), "deviation_pct": round(dev, 2),
            "max_pct": MAX_DEVIATION_PCT, "source_time": nq.get("time"),
            "message": (f"Yahoo and Nasdaq agree within {dev:.2f}%." if ok else
                        f"Yahoo (${price:,.2f}) and Nasdaq (${nq['price']:,.2f}) differ by {dev:.2f}%, "
                        f"more than the {MAX_DEVIATION_PCT}% limit.")}


def _clean(x):
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return x
    return None if math.isnan(f) or math.isinf(f) else round(f, 4)


_DAYS = {"6mo": 190, "1y": 370, "2y": 735, "5y": 1830}


def _yahoo_or_nasdaq(symbol, period):
    try:
        df = yf.Ticker(symbol).history(period=period, auto_adjust=True)
        if not df.empty:
            df.index = df.index.tz_localize(None)
            return df.dropna(subset=["Close"])
    except Exception:  # noqa: BLE001  (Yahoo often rate-limits cloud servers)
        pass
    return nasdaq_history(symbol, _DAYS.get(period, 370))


def history(symbol, period="1y"):
    symbol = clean_symbol(symbol)

    def load():
        return _yahoo_or_nasdaq(symbol, period)

    return _cached(("hist", symbol, period), load)


def bulk_history(symbols, period="1y"):
    key = ("bulk", tuple(sorted(symbols)), period)

    def load():
        out = {}
        try:
            df = yf.download(symbols, period=period, group_by="ticker", auto_adjust=True,
                             threads=True, progress=False)
            for s in symbols:
                try:
                    d = (df[s] if len(symbols) > 1 else df).dropna(subset=["Close"])
                    if len(d) > 60:
                        d.index = d.index.tz_localize(None) if d.index.tz is not None else d.index
                        out[s] = d
                except KeyError:
                    pass
        except Exception:  # noqa: BLE001
            pass
        missing = [s for s in symbols if s not in out]

        def single(s):  # Yahoo drops tickers from bulk calls; retry singly, then Nasdaq
            try:
                return s, _yahoo_or_nasdaq(s, period)
            except Exception:  # noqa: BLE001
                return s, None
        with ThreadPoolExecutor(6) as ex:
            for s, d in ex.map(single, missing):
                if d is not None and len(d) > 60:
                    out[s] = d
        return out

    return _cached(key, load)


def info(symbol):
    symbol = clean_symbol(symbol)
    return _cached(("info", symbol), lambda: yf.Ticker(symbol).info or {}, ttl=6 * 3600)


def atr(df, n=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return float(tr.rolling(n).mean().iloc[-1])


def quote(symbol, verify=True):
    symbol = clean_symbol(symbol)
    df = history(symbol, "1y")
    last, prev = float(df["Close"].iloc[-1]), float(df["Close"].iloc[-2])
    return {
        "symbol": symbol,
        "price": round(last, 2),
        "price_check": verify_price(symbol, last) if verify else None,
        "change_pct": round((last / prev - 1) * 100, 2),
        "atr": round(atr(df), 2),
        "high_52w": round(float(df["High"].max()), 2),
        "low_52w": round(float(df["Low"].min()), 2),
        "sma50": round(float(df["Close"].rolling(50).mean().iloc[-1]), 2),
        "sma200": round(float(df["Close"].rolling(200).mean().iloc[-1]), 2) if len(df) >= 200 else None,
        "as_of": str(df.index[-1].date()),
        "chart": [{"t": str(i.date()), "c": round(float(v), 2)} for i, v in df["Close"].tail(180).items()],
    }


def quotes(symbols):
    if not symbols:
        return {}
    data = bulk_history(list(symbols), "6mo")
    out = {}
    for s in symbols:
        d = data.get(s)
        if d is not None:
            out[s] = {"price": round(float(d["Close"].iloc[-1]), 2),
                      "change_pct": round((float(d["Close"].iloc[-1]) / float(d["Close"].iloc[-2]) - 1) * 100, 2)}
    return out


# ---------------------------------------------------------------- screens

def _rs_scores(data):
    """Relative strength: weighted 3/6/12-month return, ranked 1-99 (IBD-style)."""
    raw = {}
    for s, d in data.items():
        c = d["Close"]
        def ret(n):
            return c.iloc[-1] / c.iloc[-min(n, len(c))] - 1
        raw[s] = 0.4 * ret(63) + 0.2 * ret(126) + 0.4 * ret(252)
    ranked = pd.Series(raw).rank(pct=True) * 98 + 1
    return ranked.round().astype(int).to_dict()


def screen_trend_vcp(data, rs):
    """Minervini trend template + volatility-contraction check (vcp-screener method)."""
    out = []
    for s, d in data.items():
        c, v = d["Close"], d["Volume"]
        if len(c) < 200:
            continue
        price = c.iloc[-1]
        s50, s150, s200 = (c.rolling(n).mean() for n in (50, 150, 200))
        hi, lo = d["High"].tail(252).max(), d["Low"].tail(252).min()
        checks = {
            "price>50>150>200": price > s50.iloc[-1] > s150.iloc[-1] > s200.iloc[-1],
            "200MA rising 1mo": s200.iloc[-1] > s200.iloc[-22],
            "within 25% of 52w high": price >= 0.75 * hi,
            ">30% above 52w low": price >= 1.3 * lo,
            "RS >= 70": rs.get(s, 0) >= 70,
        }
        # contraction: successive pullback depths over last ~60 bars shrink
        win = d.tail(60)
        depths = []
        for i in range(3):
            chunk = win.iloc[i * 20:(i + 1) * 20]
            depths.append((chunk["High"].max() - chunk["Low"].min()) / chunk["High"].max() * 100)
        contracting = depths[0] > depths[1] > depths[2]
        vol_dry = v.tail(10).mean() < v.tail(50).mean()
        pivot = float(win.tail(20)["High"].max())
        score = sum(checks.values()) * 15 + (15 if contracting else 0) + (10 if vol_dry else 0)
        if sum(checks.values()) >= 4:
            out.append({
                "symbol": s, "score": int(score), "price": round(float(price), 2),
                "rs": rs.get(s), "pivot": round(pivot, 2),
                "dist_to_pivot_pct": round((pivot / price - 1) * 100, 2),
                "contractions": [round(float(x), 1) for x in depths], "contracting": bool(contracting),
                "volume_dry_up": bool(vol_dry), "atr": round(atr(d), 2),
                "suggested_stop": round(float(win.tail(10)["Low"].min()), 2),
                "checks": {k: bool(v) for k, v in checks.items()},
            })
    # strongest relative strength first, score breaks ties (same order the backtest uses)
    return sorted(out, key=lambda r: (-(r["rs"] or 0), -r["score"]))


def screen_momentum_burst(data, rs):
    """Stockbee momentum burst: >=4% up day on higher volume than the prior day."""
    out = []
    for s, d in data.items():
        c, v = d["Close"], d["Volume"]
        for back in range(1, 4):  # last 3 sessions
            chg = c.iloc[-back] / c.iloc[-back - 1] - 1
            if chg >= 0.04 and v.iloc[-back] > v.iloc[-back - 1] and v.iloc[-back] > 100_000:
                out.append({"symbol": s, "days_ago": back - 1, "change_pct": round(chg * 100, 2),
                            "vol_ratio": round(float(v.iloc[-back] / v.iloc[-back - 1]), 2),
                            "price": round(float(c.iloc[-1]), 2), "rs": rs.get(s),
                            "suggested_stop": round(float(d["Low"].iloc[-back]), 2),
                            "atr": round(atr(d), 2)})
                break
    return sorted(out, key=lambda r: (r["days_ago"], -r["change_pct"]))


def _infos(symbols):
    with ThreadPoolExecutor(8) as ex:
        return dict(zip(symbols, ex.map(lambda s: _safe_info(s), symbols)))


def _safe_info(s):
    try:
        return info(s)
    except Exception:  # noqa: BLE001
        return {}


def screen_value_dividend(symbols):
    """value-dividend-screener: yield >= 2.5%, P/E <= 20, P/B <= 3, payout <= 80%."""
    out = []
    for s, i in _infos(symbols).items():
        y = i.get("dividendYield")
        y = (y / 100 if y and y > 1 else y) or 0
        pe, pb, payout = i.get("trailingPE"), i.get("priceToBook"), i.get("payoutRatio")
        if y >= 0.025 and pe and pe <= 20 and (pb is None or pb <= 3) and (payout is None or payout <= 0.8):
            score = min(40, y * 1000) + max(0, 20 - pe) * 1.5 + (10 if payout and payout < 0.6 else 0)
            out.append({"symbol": s, "name": i.get("shortName"), "sector": i.get("sector"),
                        "price": _clean(i.get("currentPrice")), "yield_pct": round(y * 100, 2),
                        "pe": _clean(pe), "pb": _clean(pb),
                        "payout_pct": round(payout * 100, 1) if payout else None,
                        "score": round(score, 1)})
    return sorted(out, key=lambda r: -r["score"])


def screen_canslim(symbols, data, rs):
    """canslim-screener (lite): C/A growth, N near highs, L leader RS, I institutions."""
    out = []
    for s, i in _infos(symbols).items():
        d = data.get(s)
        if d is None:
            continue
        eg, rg = i.get("earningsQuarterlyGrowth"), i.get("revenueGrowth")
        price, hi = float(d["Close"].iloc[-1]), float(d["High"].tail(252).max())
        inst = i.get("heldPercentInstitutions")
        parts = {
            "C: qtr EPS growth >= 25%": bool(eg and eg >= 0.25),
            "A: revenue growth >= 15%": bool(rg and rg >= 0.15),
            "N: within 15% of high": price >= 0.85 * hi,
            "L: RS >= 80": rs.get(s, 0) >= 80,
            "I: inst. 30-95%": bool(inst and 0.3 <= inst <= 0.95),
        }
        score = sum(parts.values())
        if score >= 3:
            out.append({"symbol": s, "name": i.get("shortName"), "score": score * 20,
                        "price": round(price, 2), "rs": rs.get(s),
                        "eps_growth_pct": round(eg * 100, 1) if eg else None,
                        "rev_growth_pct": round(rg * 100, 1) if rg else None,
                        "checks": parts})
    return sorted(out, key=lambda r: -r["score"])


PRICE_SCREENS = ("vcp", "momentum", "leaders", "pullback")


def run_screen(kind, symbols):
    """Returns {"results": [...], "note": str|None}."""
    symbols = [clean_symbol(x) for x in symbols]
    if kind in ("leaders", "pullback"):
        data = bulk_history(sorted(set(symbols) | {"SPY"}), "2y")
        if "SPY" not in data:
            raise ValueError("Couldn't load SPY, which these screens need.")
        stocks = {s: d for s, d in data.items() if s != "SPY" and s in symbols}
        res, note = strategies.live(kind, stocks, data["SPY"])
        return {"results": res, "note": note}
    data = bulk_history(symbols, "2y")
    rs = _rs_scores(data)
    if kind == "vcp":
        return {"results": screen_trend_vcp(data, rs), "note": None}
    if kind == "momentum":
        return {"results": screen_momentum_burst(data, rs), "note": None}
    if kind == "value":
        return {"results": screen_value_dividend(symbols), "note": None}
    if kind == "canslim":
        return {"results": screen_canslim(symbols, data, rs), "note": None}
    raise ValueError(f"unknown screen {kind}")


# ---------------------------------------------------------------- backtest

def backtest(kind, symbols):
    """5-year walk-forward basket test (see strategies.backtest)."""
    if kind not in strategies.HOLD:
        return {"kind": kind, "supported": False,
                "reason": "This screen uses today's fundamentals (earnings, dividends), and the free data "
                          "has no point-in-time history of them, so an honest backtest isn't possible."}
    symbols = sorted({clean_symbol(x) for x in symbols})
    if len(symbols) < 10:
        raise ValueError("Backtests need at least 10 tickers so the 'own everything' baseline means something.")

    def load():
        data = bulk_history(sorted(set(symbols) | {"SPY"}), "5y")
        if "SPY" not in data:
            raise ValueError("Couldn't load SPY for the benchmark.")
        return strategies.backtest(kind, data)
    return _cached(("bt", kind, tuple(symbols)), load, ttl=12 * 3600)


def holding_info(symbol):
    i = _safe_info(symbol)
    rate = i.get("dividendRate") or i.get("trailingAnnualDividendRate") or 0
    return {"name": i.get("shortName"), "sector": i.get("sector") or ("ETF" if i.get("quoteType") == "ETF" else "Other"),
            "dividend_rate": _clean(rate) or 0}


# ---------------------------------------------------------------- charts & watchlist

_CHART_BARS = {"3mo": 63, "6mo": 126, "1y": 252, "2y": 504}


def chart(symbol, period="1y"):
    """Daily bars plus 20/50/200-day averages (averages use the full history, then get sliced)."""
    symbol = clean_symbol(symbol)
    if period not in _CHART_BARS:
        raise ValueError("Period must be one of 3mo, 6mo, 1y, 2y.")
    df = history(symbol, "2y")
    c = df["Close"]
    ma = {n: c.rolling(n).mean() for n in (20, 50, 200)}
    d = df.tail(_CHART_BARS[period])
    r = lambda x: None if pd.isna(x) else round(float(x), 2)  # noqa: E731
    return {"symbol": symbol, "period": period, "bars": [
        {"t": str(i.date()), "o": r(row.Open), "h": r(row.High), "l": r(row.Low), "c": r(row.Close),
         "v": int(row.Volume or 0), "ma20": r(ma[20].loc[i]), "ma50": r(ma[50].loc[i]), "ma200": r(ma[200].loc[i])}
        for i, row in d.iterrows()]}


def watch_signals(symbol):
    """Per-stock signals that don't need a whole universe to rank against."""
    df = history(symbol, "2y")
    c, v, h = df["Close"], df["Volume"], df["High"]
    price, prev = float(c.iloc[-1]), float(c.iloc[-2])
    s50, s150, s200 = (c.rolling(n).mean() for n in (50, 150, 200))
    diff = c.diff()
    up = diff.clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    dn = (-diff.clip(upper=0)).ewm(alpha=0.5, adjust=False).mean()
    rsi2 = float((100 - 100 / (1 + up / dn.replace(0, float("nan")))).iloc[-1])
    hi = float(h.tail(252).max())
    has200 = len(c) >= 220
    flags = {
        "uptrend": bool(has200 and price > s50.iloc[-1] > s150.iloc[-1] > s200.iloc[-1] and s200.iloc[-1] > s200.iloc[-22]),
        "near_high": price >= 0.95 * hi,
        "pullback": bool(has200 and price > s200.iloc[-1] and rsi2 < 10),
        "burst": price / prev - 1 >= 0.04 and v.iloc[-1] > v.iloc[-2],
    }
    mom = float(c.iloc[-22] / c.iloc[-253] - 1) * 100 if len(c) > 253 else None
    return {"price": round(price, 2), "change_pct": round((price / prev - 1) * 100, 2),
            "from_high_pct": round((price / hi - 1) * 100, 1), "mom_12_1_pct": round(mom, 1) if mom is not None else None,
            "rsi2": None if math.isnan(rsi2) else round(rsi2, 1), "flags": flags, "as_of": str(df.index[-1].date()),
            "spark": [round(float(x), 2) for x in c.tail(60)]}
