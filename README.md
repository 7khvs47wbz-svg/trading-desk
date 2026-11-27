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
- **Screens (backtest):** every 5 trading days each price screen picks its top 10 using only
  data known that day and holds them 20 trading days (5 for the pullback screen). The default
  universe is **the S&P 500 as it was on each date** (membership history from
  [fja05680/sp500](https://github.com/fja05680/sp500), MIT, in `reference/`), so stocks that later
  left the index, were acquired or collapsed are included, not just today's survivors. A stock
  whose prices stop during a hold is sold at its last price. Every pick pays a **trading cost on
  the buy and again on the sell** (default 0.10% each way, adjustable in Settings as
  `cost_bps_per_side`); SPY and the baseline are passive and pay nothing. Each screen is compared
  with SPY and with owning every index member that day (equal weight), and the data is split in
  half: a screen "holds up" only if it beats that baseline in both halves.
  With 0.10% costs on the point-in-time S&P 500 (Nov 2026 run): momentum leaders and trend
  template + VCP hold up (about +1.0% to +2.5% per 20-day hold over the baseline after costs),
  momentum burst holds up only barely (+0.45% older half, +0.10% newer half, and it trails SPY),
  and the uptrend pullback screen does not hold up. Costs take about 0.2% off every hold.
  You can still backtest your own ticker list, but that view only contains today's companies.
- **What's still missing:** prices cover about 96% of index member-days (99% today, 94% at
  worst). The gap is mostly companies acquired or delisted before the data was fetched (for
  example ATVI, XLNX, TWTR, SIVB, FRC, PXD), which Yahoo no longer serves; the app reports the
  coverage with every result. Closing it fully needs a paid survivorship-free data source.
  Fundamental screens (CANSLIM, value-dividend) are not backtested because the free data has no
  point-in-time fundamentals.
- **Live forward test:** after each US close the app records every price screen's top 10 among
  the current S&P 500 members with that day's closing price (`jobs.py`, triggered by a GitHub
  Actions cron and opportunistically by visits; it is idempotent). Picks are scored after their
  holding period with the same costs, against SPY and the equal-weight index. Because picks are
  written down before the outcome is known, this record has no hindsight or survivorship bias,
  but it only becomes meaningful after a few months (dozens of completed holds). Past results,
  backtested or live, don't guarantee future ones.

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
.venv/bin/python -m pytest -q # 65 tests, includes a live Yahoo-vs-Nasdaq check
```

## Deploy (Render, free)

1. On render.com: **New > Blueprint**, and give it this repo. Render reads `render.yaml`.
2. It creates the web service (with a generated `SECRET_KEY`). Set `DATABASE_URL` to a Postgres
   connection string, for example a free Neon database, which doesn't expire.
   `OLD_DATABASE_URL` (filled from the blueprint's Render Postgres) is only for moving data: on
   startup the app copies every row into an empty `DATABASE_URL` once, keeping ids.
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
- `universe.py`: point-in-time S&P 500 membership (with ticker renames)
- `jobs.py`: background S&P 500 backtest (cached in the database, weekly) and the live forward test
- `reference/`: the S&P 500 membership history file and its source
- `static/`: single-page UI
- `tests/`: accuracy and API tests
