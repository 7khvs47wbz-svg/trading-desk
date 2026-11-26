"""Point-in-time S&P 500 membership, so backtests use the stocks that were really in the
index on each date (including ones later removed), not just today's survivors."""
import csv
import io
import threading
import time
from pathlib import Path

import pandas as pd
import requests

LOCAL = Path(__file__).parent / "reference" / "sp500_ticker_start_end.csv"
REMOTE = "https://raw.githubusercontent.com/fja05680/sp500/master/sp500_ticker_start_end.csv"
_cache = {"t": 0.0, "rows": None}
_lock = threading.Lock()


# Ticker renames: Yahoo files the whole price history under the new symbol, so a stint under
# the old symbol is mapped to the new one. Only well-documented renames are listed; companies
# that were acquired or went private are NOT mapped (their missing prices are reported).
RENAMES = {
    "ABC": "COR", "ANTM": "ELV", "BLL": "BALL", "FLT": "CPAY", "PKI": "RVTY", "PEAK": "DOC",
    "RE": "EG", "WLTW": "WTW", "FBHS": "FBIN", "NLOK": "GEN", "GPS": "GAP", "DISCA": "WBD",
    "FB": "META", "FISV": "FI", "CDAY": "DAY", "HCP": "PEAK",
}


def _yahoo(t):
    t = t.strip().upper().replace(".", "-")
    seen = set()
    while t in RENAMES and t not in seen:  # follow chains like HCP -> PEAK -> DOC
        seen.add(t)
        t = RENAMES[t]
    return t


def _parse(text):
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        if r.get("ticker") and r.get("start_date"):
            rows.append((_yahoo(r["ticker"]), pd.Timestamp(r["start_date"]),
                         pd.Timestamp(r["end_date"]) if r.get("end_date") else None))
    return rows


def stints():
    """[(symbol, start, end_or_None)], refreshed from GitHub weekly, vendored copy as fallback."""
    with _lock:
        if _cache["rows"] is None or time.time() - _cache["t"] > 7 * 86400:
            rows = None
            try:
                r = requests.get(REMOTE, timeout=15)
                if r.ok:
                    rows = _parse(r.text)
                    if len(rows) < 1000:  # sanity check against a truncated download
                        rows = None
            except requests.RequestException:
                pass
            _cache["rows"] = rows or _parse(LOCAL.read_text())
            _cache["t"] = time.time()
        return _cache["rows"]


def current():
    return sorted({s for s, _, e in stints() if e is None})


def symbols_since(start):
    """Every ticker that was a member at any point from `start` until today."""
    start = pd.Timestamp(start)
    return sorted({s for s, b, e in stints() if e is None or e > start})


def members(index, symbols):
    """Boolean frame (dates x symbols): was the stock in the S&P 500 on that date?"""
    m = pd.DataFrame(False, index=index, columns=symbols)
    for s, b, e in stints():
        if s in m.columns:
            m.loc[(m.index >= b) & ((m.index < e) if e is not None else True), s] = True
    return m
