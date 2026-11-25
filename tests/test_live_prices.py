"""Live data check: Yahoo prices must agree with Nasdaq within 2%."""
import pytest

import market

TICKERS = ["AAPL", "MSFT", "NVDA", "AMZN", "JPM", "KO", "XOM", "COST", "SPY", "QQQ"]


@pytest.mark.parametrize("sym", TICKERS)
def test_yahoo_matches_nasdaq_within_2pct(sym):
    q = market.quote(sym)
    pc = q["price_check"]
    assert pc["second_source"], f"Nasdaq had no price for {sym}"
    assert pc["deviation_pct"] <= 2.0, pc["message"]


def test_bad_price_is_flagged(monkeypatch):
    monkeypatch.setattr(market, "nasdaq_price", lambda s: {"price": 100.0, "time": "t"})
    assert market.verify_price("XYZ", 102.5)["ok"] is False
    assert market.verify_price("XYZ", 101.9)["ok"] is True
