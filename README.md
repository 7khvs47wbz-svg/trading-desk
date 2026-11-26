# Trading Desk

A multi-user decision-support and **paper-trading** web app built on
[claude-trading-skills](https://github.com/tradermonty/claude-trading-skills) (MIT).
It never places real orders. Not financial advice.

Each person signs up with an email and password and gets a private paper account
($100,000 of fake money), portfolio tracker, trade journal and settings.

## Accuracy

- **Prices (2% limit):** every quote comes from Yahoo Finance and is cross-checked against
  Nasdaq (live last sale while the market is open, official close otherwise). If the two
  sources differ by more than 2%, the paper ticket shows a warning and buys/closes are blocked.
  If Yahoo is unreachable (common on cloud servers), Nasdaq's daily history is used instead.
- **Calculations:** position size, P&L, R-multiples, portfolio value/weights/income and
  circuit-breaker thresholds are tested against hand calculations (`tests/test_calcs.py`).
- **Screens:** the four price screens were kept only after a 5-year walk-forward test: every 5
  trading days each screen picks its top 10 using only data known that day, holds them 20 days
  (5 for the pullback screen), and is compared with SPY **and** with owning every stock in the
  list. That second baseline matters because a list of today's large companies beats SPY on its
  own (survivorship bias). The data is split in half and a screen must beat the baseline in both
  halves. Momentum leaders and trend template + VCP passed in both the 6-year research window and
  the app's own 5-year window (roughly +0.8% to +1.8% per 20-day hold over the baseline, before
  costs). Momentum burst and uptrend pullback passed in the 6-year window but each failed one half
  of the 5-year window, so the app labels them "no consistent edge": their edge depends on the
  period tested. Three other candidate rules failed outright and were dropped. Each screen's own
  backtest is one click away in the app. Fundamental screens (CANSLIM, value-dividend) are not
  backtested because the free data has no point-in-time fundamentals. Past results don't
  guarantee future ones.

## Tabs

| Tab | What it does | Skills used |
| --- | --- | --- |
| Cockpit | Market posture, exposure ceiling, breadth/uptrend scores, your circuit breaker | market-breadth-analyzer, uptrend-analyzer, exposure-coach; drawdown-circuit-breaker rules |
| Screener | Momentum leaders (12-1), trend-template/VCP, momentum burst, uptrend pullback (RSI-2), CANSLIM-lite, value-dividend; 5-year backtest per price screen with an unseen-half check | vcp-screener, stockbee-momentum-burst-screener, canslim-screener, value-dividend-screener |
| Watchlist | Starred stocks with price, sparkline and live signals (uptrend, near high, pullback, burst) | |
| Charts | Click any ticker: price with 20/50/200-day averages, volume, and a position's entry/stop/target | |
| Paper Trading | Fake-money buys sized by position-sizer and checked by the discipline gate (NO_GO needs an explicit, journaled override) | position-sizer, pre-trade-discipline-gate |
| Portfolio | Manual or CSV holdings, live value, sector mix, dividend income, concentration warnings | portfolio-manager (CSV path), exposure-coach |
| Journal | Auto-logged paper trades plus manual entries, stats, weekly digest, P&L curve, and a trade review (by setup, gate/plan discipline, R distribution, plain-English insights) | trader-memory-core, signal-postmortem, weekly-performance-digest |
| Sizer & Settings | Standalone sizer, risk defaults, screener universe, account (password, delete) | position-sizer |

## Run locally

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
./run.sh                      # http://127.0.0.1:5057 (uses data/trading.db)
.venv/bin/python -m pytest -q # 55 tests, includes a live Yahoo-vs-Nasdaq check
```

## Deploy (Render, free)

1. On render.com: **New > Blueprint**, and give it this repo. Render reads `render.yaml`.
2. It creates the web service and a Postgres database and links them (`DATABASE_URL` is filled
   in automatically; `SECRET_KEY` is generated). Nothing to paste.
3. Deploys run on every push to `main`.

The free web service sleeps after 15 idle minutes (the first visit then takes ~30-60 seconds), and
Render's free Postgres is deleted after 30 days unless upgraded. The app also accepts any other
Postgres (for example Neon) through `DATABASE_URL`.

## Files

- `app.py`: Flask API, accounts, sessions, security headers
- `db.py`: Postgres (DATABASE_URL) or SQLite storage, all rows scoped by user
- `engines.py`: runs the vendored skill scripts in `skills/` and reads their JSON
- `market.py`: Yahoo + Nasdaq prices, the 2% cross-check, screens, charts, watchlist signals
- `strategies.py`: the price-screen rules as vectorised code, shared by the live screener and the 5-year backtest
- `static/`: single-page UI
- `tests/`: accuracy and API tests
