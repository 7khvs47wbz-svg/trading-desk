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
- **Screens:** price-based screens (trend template + VCP, momentum burst) have a walk-forward
  backtest: the screen is re-run on past dates using only the data known then, and each pick's
  next-20-day return is compared with SPY. Fundamental screens (CANSLIM, value-dividend) are not
  backtested because the free data has no point-in-time fundamentals. No screen is promised to
  be right; the backtest shows how often it has been.

## Tabs

| Tab | What it does | Skills used |
| --- | --- | --- |
| Cockpit | Market posture, exposure ceiling, breadth/uptrend scores, your circuit breaker | market-breadth-analyzer, uptrend-analyzer, exposure-coach; drawdown-circuit-breaker rules |
| Screener | Trend-template/VCP, momentum burst, CANSLIM-lite, value-dividend; backtest per screen | vcp-screener, stockbee-momentum-burst-screener, canslim-screener, value-dividend-screener |
| Paper Trading | Fake-money buys sized by position-sizer and checked by the discipline gate (NO_GO needs an explicit, journaled override) | position-sizer, pre-trade-discipline-gate |
| Portfolio | Manual or CSV holdings, live value, sector mix, dividend income, concentration warnings | portfolio-manager (CSV path), exposure-coach |
| Journal | Auto-logged paper trades plus manual entries, stats, weekly digest, P&L curve | trader-memory-core, signal-postmortem, weekly-performance-digest |
| Sizer & Settings | Standalone sizer, risk defaults, screener universe, account (password, delete) | position-sizer |

## Run locally

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
./run.sh                      # http://127.0.0.1:5057 (uses data/trading.db)
.venv/bin/python -m pytest -q # 43 tests, includes a live Yahoo-vs-Nasdaq check
```

## Deploy (Render + Neon, free)

1. Create a free Postgres database at neon.tech and copy its connection string.
2. On render.com: **New > Blueprint**, pick this repo. Render reads `render.yaml`.
3. Paste the Neon connection string into `DATABASE_URL` when asked. `SECRET_KEY` is generated.
4. Deploys run on every push to `main`.

The free plan sleeps after 15 idle minutes; the first visit afterwards takes ~30-60 seconds.

## Files

- `app.py`: Flask API, accounts, sessions, security headers
- `db.py`: Postgres (DATABASE_URL) or SQLite storage, all rows scoped by user
- `engines.py`: runs the vendored skill scripts in `skills/` and reads their JSON
- `market.py`: Yahoo + Nasdaq prices, the 2% cross-check, screens, backtests
- `static/`: single-page UI
- `tests/`: accuracy and API tests
