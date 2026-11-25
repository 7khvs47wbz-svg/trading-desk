"""Runs the claude-trading-skills scripts as calculation engines.

Each call shells out to the skill's own script, writes into data/runs/<name>/,
and returns the JSON artifact the skill produced.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db

_HERE = Path(__file__).parent
_default_skills = _HERE / "skills" if (_HERE / "skills").exists() else \
    Path.home() / "vendor" / "claude-trading-skills" / "skills"
SKILLS = Path(os.environ.get("TRADING_SKILLS_DIR", _default_skills)).resolve()
RUNS = _HERE / "data" / "runs"
PY = sys.executable
_regime_lock = threading.Lock()
_last_refresh = [0.0]


class EngineError(RuntimeError):
    pass


def _run(name, script, args, timeout=180, pattern="*.json", out=None):
    """Run a skill script. Without `out`, it writes to a private temp dir that is
    removed afterwards, so concurrent users never read each other's results."""
    private = out is None
    out = Path(tempfile.mkdtemp(prefix=name + "_", dir=_tmp_root())) if private else out
    out.mkdir(parents=True, exist_ok=True)
    try:
        before = set(out.glob(pattern))
        cmd = [PY, str(SKILLS / script), *map(str, args), "--output-dir", str(out)]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=SKILLS / script.split("/")[0])
        new = sorted(set(out.glob(pattern)) - before, key=lambda f: f.stat().st_mtime)
        if not new:
            raise EngineError(f"{name} failed: {(p.stderr or p.stdout)[-800:]}")
        return new[-1], json.loads(new[-1].read_text())
    finally:
        if private:
            shutil.rmtree(out, ignore_errors=True)


def _tmp_root():
    d = RUNS / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def latest(name, pattern="*.json"):
    files = sorted((RUNS / name).glob(pattern), key=lambda f: f.stat().st_mtime)
    return (files[-1], json.loads(files[-1].read_text())) if files else (None, None)


# ------------------------------------------------------------- market regime

def market_regime(refresh=False):
    """market-breadth-analyzer + uptrend-analyzer -> exposure-coach. Shared by all users."""
    import time
    with _regime_lock:
        _, ex = latest("exposure", "exposure_posture_*.json")
        fresh = ex and datetime.now(timezone.utc) - datetime.fromisoformat(ex["generated_at"]) < timedelta(hours=6)
        # a manual refresh re-runs at most every 15 minutes, however many people click it
        if fresh and (not refresh or time.time() - _last_refresh[0] < 900):
            return _bundle()
        _last_refresh[0] = time.time()
        return _refresh_regime()


def _refresh_regime():
    errors = []
    try:
        _run("breadth", "market-breadth-analyzer/scripts/market_breadth_analyzer.py", [],
             pattern="market_breadth_2*.json", out=RUNS / "breadth")
    except Exception as e:  # noqa: BLE001
        errors.append(str(e))
    try:
        _run("uptrend", "uptrend-analyzer/scripts/uptrend_analyzer.py", [],
             pattern="uptrend_analysis_*.json", out=RUNS / "uptrend")
    except Exception as e:  # noqa: BLE001
        errors.append(str(e))
    args = []
    b, _ = latest("breadth", "market_breadth_2*.json")
    u, _ = latest("uptrend", "uptrend_analysis_*.json")
    if b:
        args += ["--breadth", b]
    if u:
        args += ["--uptrend", u]
    if not args:
        raise EngineError("; ".join(errors))
    _run("exposure", "exposure-coach/scripts/calculate_exposure.py", args + ["--json-only"],
         pattern="exposure_posture_*.json", out=RUNS / "exposure")
    _prune()
    res = _bundle()
    res["errors"] = errors
    return res


def _prune(keep=20):
    for name in ("breadth", "uptrend", "exposure"):
        files = sorted((RUNS / name).glob("*_2*.json"), key=lambda f: f.stat().st_mtime)
        for f in files[:-keep]:
            f.unlink(missing_ok=True)


def _bundle():
    _, b = latest("breadth", "market_breadth_2*.json")
    _, u = latest("uptrend", "uptrend_analysis_*.json")
    _, e = latest("exposure", "exposure_posture_*.json")
    hist_file = RUNS / "breadth" / "market_breadth_history.json"
    hist = json.loads(hist_file.read_text()) if hist_file.exists() else []
    return {"breadth": b, "uptrend": u, "exposure": e, "breadth_history": hist}


# ------------------------------------------------------------- sizing

def position_size(account, entry, stop=None, atr=None, risk_pct=1.0, max_position_pct=None,
                  win_rate=None, avg_win=None, avg_loss=None, fractional=False):
    args = ["--account-size", account, "--entry", entry, "--risk-pct", risk_pct]
    if stop:
        args += ["--stop", stop]
    elif atr:
        args += ["--atr", atr]
    if max_position_pct:
        args += ["--max-position-pct", max_position_pct]
    if win_rate and avg_win and avg_loss:
        args += ["--win-rate", win_rate, "--avg-win", avg_win, "--avg-loss", avg_loss]
    if fractional:
        args += ["--fractional"]
    _, res = _run("sizer", "position-sizer/scripts/position_sizer.py", args, timeout=30)
    return res


# ------------------------------------------------------------- circuit breaker

def circuit_breaker(uid, account_size, max_daily=2.0, weekly=5.0, monthly=8.0, streak_n=2, cooldown_h=24,
                    write_to=None):
    """drawdown-circuit-breaker rules applied to the paper-trade journal.

    Same thresholds as the skill: daily 2%, weekly 5%, monthly 8% of account,
    and a cooldown after N consecutive losing trades.
    """
    trades = db.rows("SELECT pnl, closed_at FROM journal WHERE user_id=? AND closed_at IS NOT NULL "
                     "AND pnl IS NOT NULL ORDER BY closed_at", (uid,))
    now = datetime.now(timezone.utc)
    today = now.date()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    def total(since):
        return sum(t["pnl"] for t in trades if datetime.fromisoformat(t["closed_at"]).date() >= since)

    rules = []
    d, w, m = total(today), total(week_start), total(month_start)
    if d <= -account_size * max_daily / 100:
        rules.append({"rule": "max_daily_loss", "value": round(d, 2), "decision": "HALTED"})
    if w <= -account_size * weekly / 100:
        rules.append({"rule": "weekly_drawdown", "value": round(w, 2), "decision": "HALTED"})
    if m <= -account_size * monthly / 100:
        rules.append({"rule": "monthly_drawdown", "value": round(m, 2), "decision": "HALTED"})
    streak = 0
    for t in reversed(trades):
        if t["pnl"] < 0:
            streak += 1
        else:
            break
    if streak >= streak_n:
        last = datetime.fromisoformat(trades[-1]["closed_at"])
        until = last + timedelta(hours=cooldown_h)
        if now < until:
            rules.append({"rule": "losing_streak", "value": streak, "decision": "COOLDOWN",
                          "active_until": until.isoformat()})
    rec = "HALTED" if any(r["decision"] == "HALTED" for r in rules) else (
        "COOLDOWN" if rules else "TRADING_ALLOWED")
    res = {"recommendation": rec, "triggered_rules": rules, "pnl_today": round(d, 2),
           "pnl_week": round(w, 2), "pnl_month": round(m, 2), "losing_streak": streak,
           "limits": {"daily": max_daily, "weekly": weekly, "monthly": monthly, "streak_n": streak_n},
           "generated_at": now.isoformat()}
    if write_to:
        Path(write_to).write_text(json.dumps(res, indent=2))
    return res


# ------------------------------------------------------------- discipline gate

def pre_trade_gate(uid, candidate, account_size):
    """pre-trade-discipline-gate with exposure-coach + this user's circuit breaker."""
    work = Path(tempfile.mkdtemp(prefix="gate_", dir=_tmp_root()))
    try:
        answers = work / "answers.json"
        answers.write_text(json.dumps({"candidates": [candidate]}))
        cb = work / "circuit_breaker_decision.json"
        circuit_breaker(uid, account_size, write_to=cb)
        regime = work / "regime.json"
        _, ex = latest("exposure", "exposure_posture_*.json")
        (work / "state").mkdir()
        args = ["--answers-file", answers, "--state-dir", work / "state", "--journal-dir", work / "journal",
                "--circuit-breaker-decision", cb, "--json-only"]
        if ex:
            regime.write_text(json.dumps(ex))
            args += ["--market-regime-decision", regime]
        _, res = _run("gate", "pre-trade-discipline-gate/scripts/check_pre_trade_discipline.py", args,
                      timeout=30, pattern="pre_trade_discipline_decision_*.json", out=work / "out")
        return res
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------- FMP screener

def fmp_vcp(universe):
    key = os.environ.get("FMP_API_KEY")
    if not key:
        raise EngineError("Set FMP_API_KEY to use the repo's full VCP screener.")
    args = ["--top", 25]
    if universe:
        args += ["--universe", *universe]
    _, res = _run("vcp_fmp", "vcp-screener/scripts/screen_vcp.py", args, timeout=300)
    return res
