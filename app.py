"""Trading Desk: a decision-support and paper-trading app built on claude-trading-skills.

Paper trading only. No broker connection; nothing here places real orders.
Each signed-in user has a private paper account, portfolio, journal and settings.
"""
import csv
import io
import os
import re
import secrets
import time
import traceback
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, g, jsonify, request, send_from_directory, session
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import db
import engines
import market

ON_SERVER = bool(os.environ.get("RENDER") or os.environ.get("PRODUCTION"))
if ON_SERVER and not db.PG:
    raise SystemExit("DATABASE_URL (Postgres) must be set on the server; local SQLite would be wiped on each deploy.")

app = Flask(__name__, static_folder="static", static_url_path="")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or ("dev-only-" + secrets.token_hex(16) if not ON_SERVER else None),
    SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=ON_SERVER,
    PERMANENT_SESSION_LIFETIME=timedelta(days=30), MAX_CONTENT_LENGTH=1_000_000,
)
if not app.config["SECRET_KEY"]:
    raise SystemExit("SECRET_KEY must be set on the server.")
db.init()

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")
MAX_UNIVERSE = 150


class Unauthorized(Exception):
    pass


@app.errorhandler(Exception)
def on_error(e):
    if isinstance(e, HTTPException):
        return jsonify({"error": e.description}), e.code
    if isinstance(e, Unauthorized):
        return jsonify({"error": str(e) or "Please sign in.", "auth": True}), 401
    if isinstance(e, (ValueError, KeyError, engines.EngineError)):
        msg = f"Missing field: {e}" if isinstance(e, KeyError) else str(e)
        return jsonify({"error": msg}), 400
    traceback.print_exc()
    return jsonify({"error": "Something went wrong on the server. Please try again."}), 500


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
    if ON_SERVER:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resp


@app.before_request
def csrf_guard():
    """State-changing API calls must be same-origin JSON (blocks cross-site form posts)."""
    if request.method in ("POST", "PATCH", "DELETE", "PUT") and request.path.startswith("/api/"):
        if not (request.content_type or "").startswith("application/json"):
            return jsonify({"error": "Requests must be JSON."}), 415
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
            return jsonify({"error": "Cross-site request blocked."}), 403


# ------------------------------------------------------------- rate limiting

_hits = defaultdict(deque)


def rate_limit(bucket, limit, per_seconds):
    key = (bucket, request.remote_addr)
    q, now = _hits[key], time.time()
    while q and q[0] < now - per_seconds:
        q.popleft()
    if len(q) >= limit:
        raise ValueError("Too many attempts. Please wait a few minutes and try again.")
    q.append(now)


# ------------------------------------------------------------- auth

def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    u = db.one("SELECT id, email, token_version FROM users WHERE id=?", (uid,))
    if not u or u["token_version"] != session.get("tv"):
        session.clear()
        return None
    return u


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        u = current_user()
        if not u:
            raise Unauthorized("Please sign in.")
        g.uid, g.email = u["id"], u["email"]
        return fn(*a, **kw)
    return wrapper


def body():
    return request.get_json(silent=True) or {}


def _start_session(u):
    session.clear()
    session.permanent = True
    session["uid"], session["tv"] = u["id"], u["token_version"]


@app.post("/api/auth/signup")
def signup():
    rate_limit("signup", 5, 3600)
    b = body()
    email, pw = (b.get("email") or "").strip().lower(), b.get("password") or ""
    if not EMAIL_RE.match(email):
        raise ValueError("Enter a valid email address.")
    if len(pw) < 8 or len(pw) > 200:
        raise ValueError("Password must be at least 8 characters.")
    if db.one("SELECT id FROM users WHERE email=?", (email,)):
        raise ValueError("An account with that email already exists. Sign in instead.")
    uid = db.insert("INSERT INTO users (email, pw_hash, created_at) VALUES (?,?,?)",
                    (email, generate_password_hash(pw), db.now_iso()))
    _start_session({"id": uid, "token_version": 0})
    return {"email": email}


@app.post("/api/auth/login")
def login():
    rate_limit("login", 20, 900)
    b = body()
    email, pw = (b.get("email") or "").strip().lower(), b.get("password") or ""
    u = db.one("SELECT * FROM users WHERE email=?", (email,))
    now = datetime.now(timezone.utc)
    if u and u["locked_until"] and datetime.fromisoformat(u["locked_until"]) > now:
        raise ValueError("Too many wrong passwords. This account is locked for 15 minutes.")
    if not u or not check_password_hash(u["pw_hash"], pw):
        if u:
            fails = (u["failed_logins"] or 0) + 1
            lock = (now + timedelta(minutes=15)).isoformat() if fails >= 8 else None
            db.execute("UPDATE users SET failed_logins=?, locked_until=? WHERE id=?",
                       (0 if lock else fails, lock, u["id"]))
        raise ValueError("Wrong email or password.")
    db.execute("UPDATE users SET failed_logins=0, locked_until=NULL WHERE id=?", (u["id"],))
    _start_session(u)
    return {"email": u["email"]}


@app.post("/api/auth/logout")
def logout():
    session.clear()
    return {"ok": True}


@app.get("/api/auth/me")
def me():
    u = current_user()
    return {"email": u["email"] if u else None}


@app.post("/api/auth/password")
@login_required
def change_password():
    rate_limit("pw", 10, 900)
    b = body()
    u = db.one("SELECT * FROM users WHERE id=?", (g.uid,))
    if not check_password_hash(u["pw_hash"], b.get("current") or ""):
        raise ValueError("Current password is wrong.")
    new = b.get("new") or ""
    if len(new) < 8:
        raise ValueError("New password must be at least 8 characters.")
    db.execute("UPDATE users SET pw_hash=?, token_version=token_version+1 WHERE id=?",
               (generate_password_hash(new), g.uid))
    _start_session(db.one("SELECT * FROM users WHERE id=?", (g.uid,)))  # other devices are signed out
    return {"ok": True}


@app.post("/api/auth/delete")
@login_required
def delete_account():
    u = db.one("SELECT * FROM users WHERE id=?", (g.uid,))
    if not check_password_hash(u["pw_hash"], body().get("password") or ""):
        raise ValueError("Password is wrong.")
    for t in ("paper_positions", "paper_orders", "journal", "holdings", "user_settings"):
        db.execute(f"DELETE FROM {t} WHERE user_id=?", (g.uid,))
    db.execute("DELETE FROM users WHERE id=?", (g.uid,))
    session.clear()
    return {"ok": True}


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/healthz")
def healthz():
    db.one("SELECT 1 AS ok")
    return {"ok": True}


# ------------------------------------------------------------- settings / data

def setting(key):
    return db.get_setting(g.uid, key)


def universe_from(text):
    syms = []
    for s in (text or "").upper().replace(",", " ").split():
        s = market.clean_symbol(s)
        if s not in syms:
            syms.append(s)
    if len(syms) > MAX_UNIVERSE:
        raise ValueError(f"Universe is limited to {MAX_UNIVERSE} tickers.")
    return syms


@app.get("/api/settings")
@login_required
def get_settings():
    s = db.all_settings(g.uid)
    s["fmp_enabled"] = bool(os.environ.get("FMP_API_KEY"))
    s["email"] = g.email
    s["max_deviation_pct"] = market.MAX_DEVIATION_PCT
    return s


@app.post("/api/settings")
@login_required
def save_settings():
    b = body()
    rules = {"risk_pct": (0.1, 5), "max_position_pct": (1, 100), "paper_starting_cash": (1000, 10_000_000),
             "portfolio_cash": (0, 1e10)}
    for k, v in b.items():
        if k == "universe":
            db.set_setting(g.uid, k, " ".join(universe_from(v)))
        elif k in rules:
            v = float(v)
            lo, hi = rules[k]
            if not lo <= v <= hi:
                raise ValueError(f"{k.replace('_', ' ')} must be between {lo:g} and {hi:g}.")
            db.set_setting(g.uid, k, v)
    return get_settings()


@app.get("/api/regime")
@login_required
def regime():
    return engines.market_regime(refresh=request.args.get("refresh") == "1")


@app.get("/api/quote/<symbol>")
@login_required
def quote(symbol):
    return market.quote(symbol)


@app.post("/api/screen")
@login_required
def screen():
    rate_limit("screen", 30, 600)
    b = body()
    universe = universe_from(b.get("universe") or setting("universe"))
    if b.get("kind") == "fmp_vcp":
        return {"results": engines.fmp_vcp(universe)}
    return {"results": market.run_screen(b["kind"], universe), "universe_size": len(universe)}


@app.post("/api/backtest")
@login_required
def backtest():
    rate_limit("backtest", 20, 600)
    b = body()
    return market.backtest(b["kind"], universe_from(b.get("universe") or setting("universe")))


# ------------------------------------------------------------- sizing & gate

def journal_stats(uid):
    t = db.rows("SELECT pnl FROM journal WHERE user_id=? AND pnl IS NOT NULL", (uid,))
    wins = [x["pnl"] for x in t if x["pnl"] > 0]
    losses = [-x["pnl"] for x in t if x["pnl"] <= 0]
    n = len(t)
    return {
        "trades": n,
        "win_rate": round(len(wins) / n, 3) if n else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "profit_factor": round(sum(wins) / sum(losses), 2) if losses and sum(losses) else None,
        "expectancy": round(sum(x["pnl"] for x in t) / n, 2) if n else None,
        "net_pnl": round(sum(x["pnl"] for x in t), 2),
    }


def paper_equity(uid):
    pos = db.rows("SELECT * FROM paper_positions WHERE user_id=? ORDER BY id", (uid,))
    q = market.quotes({p["symbol"] for p in pos})
    value = sum(p["shares"] * q.get(p["symbol"], {}).get("price", p["entry"]) for p in pos)
    return db.get_setting(uid, "paper_cash") + value, pos, q


def num(b, k, required=True, positive=True):
    v = b.get(k)
    if v in (None, ""):
        if required:
            raise ValueError(f"{k.replace('_', ' ').capitalize()} is required.")
        return None
    v = float(v)
    if positive and v <= 0:
        raise ValueError(f"{k.replace('_', ' ').capitalize()} must be greater than zero.")
    return v


@app.post("/api/size")
@login_required
def size():
    b = body()
    stats = journal_stats(g.uid)
    kelly = {}
    if b.get("use_kelly") and stats["win_rate"] and stats["avg_loss"] and 0 < stats["win_rate"] < 1:
        kelly = dict(win_rate=stats["win_rate"], avg_win=stats["avg_win"], avg_loss=stats["avg_loss"])
    entry, stop, atr = num(b, "entry"), num(b, "stop", False), num(b, "atr", False)
    if stop and stop >= entry:
        raise ValueError("Stop must be below the entry price.")
    if not stop and not atr:
        raise ValueError("Enter a stop price or an ATR.")
    return engines.position_size(
        account=num(b, "account"), entry=entry, stop=stop, atr=atr,
        risk_pct=float(b.get("risk_pct") or setting("risk_pct")),
        max_position_pct=b.get("max_position_pct") or setting("max_position_pct"),
        fractional=bool(b.get("fractional")), **kelly)


@app.get("/api/circuit")
@login_required
def circuit():
    equity, _, _ = paper_equity(g.uid)
    return engines.circuit_breaker(g.uid, equity)


def _final_shares(sz):
    for key in ("final_recommended_shares", "final_shares", "shares"):
        if key in sz:
            return sz[key]
    rec = sz.get("final_recommendation") or {}
    return rec.get("shares")


@app.post("/api/paper/preview")
@login_required
def paper_preview():
    """Size the trade with position-sizer and check it with the discipline gate."""
    b = body()
    sym = market.clean_symbol(b.get("symbol"))
    q = market.quote(sym)
    entry = q["price"]
    stop = num(b, "stop", False) or round(entry - 2 * q["atr"], 2)
    if stop >= entry:
        raise ValueError("Stop must be below the entry price for a long trade.")
    equity, _, _ = paper_equity(g.uid)
    sz = engines.position_size(account=equity, entry=entry, stop=stop,
                               risk_pct=float(b.get("risk_pct") or setting("risk_pct")),
                               max_position_pct=setting("max_position_pct"))
    shares = int(num(b, "shares", False) or _final_shares(sz) or 0)
    planned = round((entry - stop) * (_final_shares(sz) or 0), 2)
    actual = round((entry - stop) * shares, 2)
    gate = engines.pre_trade_gate(g.uid, {
        "symbol": sym, "order_intent": "ENTRY_READY",
        "entry_in_written_plan": bool(b.get("has_plan")),
        "stop_predefined": bool(b.get("stop")),
        "size_within_plan": actual <= planned * 1.05 + 0.01,
        "planned_risk_dollars": planned, "actual_risk_dollars": actual,
        "notes": (b.get("thesis") or "")[:2000],
    }, equity)
    return {"quote": q, "entry": entry, "stop": stop, "shares": shares, "sizer": sz,
            "gate": gate, "risk_dollars": actual, "cost": round(shares * entry, 2),
            "cash": setting("paper_cash")}


def _text(b, k, n=2000):
    v = b.get(k)
    return str(v)[:n] if v not in (None, "") else None


@app.post("/api/paper/buy")
@login_required
def paper_buy():
    b = body()
    sym, shares = market.clean_symbol(b.get("symbol")), num(b, "shares")
    q = market.quote(sym)
    price = q["price"]
    if q["price_check"]["ok"] is False:
        raise ValueError("Paper trade blocked: " + q["price_check"]["message"])
    cash = setting("paper_cash")
    if shares * price > cash + 1e-9:
        raise ValueError(f"Not enough paper cash (${cash:,.2f}) for {shares:g} x ${price}.")
    decision = b.get("gate_decision", "UNCHECKED")
    if decision != "GO" and not b.get("override"):
        raise ValueError(f"Gate decision is {decision}. Tick 'override' to place the paper trade anyway.")
    stop, target = num(b, "stop", False), num(b, "target", False)
    db.set_setting(g.uid, "paper_cash", round(cash - shares * price, 2))
    db.insert("INSERT INTO paper_positions (user_id, symbol, shares, entry, stop, target, setup, thesis, "
              "opened_at, gate_decision, overridden) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              (g.uid, sym, shares, price, stop, target, _text(b, "setup", 100), _text(b, "thesis"),
               db.now_iso(), str(decision)[:20], int(decision != "GO")))
    db.insert("INSERT INTO paper_orders (user_id, symbol, side, shares, price, at, note) VALUES (?,?,?,?,?,?,?)",
              (g.uid, sym, "BUY", shares, price, db.now_iso(), _text(b, "setup", 100)))
    return paper_state()


def close_position(uid, pos, price, reason):
    """Close a paper position at `price` and log it to the journal. Returns the P&L."""
    pnl = round((price - pos["entry"]) * pos["shares"], 2)
    risk = (pos["entry"] - pos["stop"]) * pos["shares"] if pos["stop"] and pos["stop"] < pos["entry"] else None
    db.set_setting(uid, "paper_cash", round(db.get_setting(uid, "paper_cash") + price * pos["shares"], 2))
    db.execute("DELETE FROM paper_positions WHERE id=? AND user_id=?", (pos["id"], uid))
    db.insert("INSERT INTO paper_orders (user_id, symbol, side, shares, price, at, note) VALUES (?,?,?,?,?,?,?)",
              (uid, pos["symbol"], "SELL", pos["shares"], price, db.now_iso(), reason))
    db.insert("INSERT INTO journal (user_id, source, symbol, shares, entry, exit, stop, pnl, r_multiple, setup, "
              "thesis, opened_at, closed_at, exit_reason, gate_decision, overridden) "
              "VALUES (?,'paper',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (uid, pos["symbol"], pos["shares"], pos["entry"], price, pos["stop"], pnl,
               round(pnl / risk, 2) if risk else None, pos["setup"], pos["thesis"], pos["opened_at"],
               db.now_iso(), reason, pos["gate_decision"], pos["overridden"]))
    return pnl


@app.post("/api/paper/sell/<int:pid>")
@login_required
def paper_sell(pid):
    pos = db.one("SELECT * FROM paper_positions WHERE id=? AND user_id=?", (pid, g.uid))
    if not pos:
        raise ValueError("Position not found.")
    q = market.quote(pos["symbol"])
    if q["price_check"]["ok"] is False:
        raise ValueError("Close blocked: " + q["price_check"]["message"] + " Try again shortly.")
    close_position(g.uid, pos, q["price"], _text(body(), "reason", 100) or "manual exit")
    return paper_state()


@app.post("/api/paper/check-stops")
@login_required
def check_stops():
    closed = []
    for p in db.rows("SELECT * FROM paper_positions WHERE user_id=?", (g.uid,)):
        price = market.quote(p["symbol"], verify=False)["price"]
        if p["stop"] and price <= p["stop"]:
            close_position(g.uid, p, price, "stop hit")
            closed.append(p["symbol"])
        elif p["target"] and price >= p["target"]:
            close_position(g.uid, p, price, "target hit")
            closed.append(p["symbol"])
    return {**paper_state(), "closed": closed}


@app.post("/api/paper/reset")
@login_required
def paper_reset():
    b = body()
    start = num(b, "cash", False) or setting("paper_starting_cash")
    if not 1000 <= start <= 10_000_000:
        raise ValueError("Starting cash must be between $1,000 and $10,000,000.")
    db.execute("DELETE FROM paper_positions WHERE user_id=?", (g.uid,))
    db.execute("DELETE FROM paper_orders WHERE user_id=?", (g.uid,))
    db.set_setting(g.uid, "paper_cash", start)
    db.set_setting(g.uid, "paper_starting_cash", start)
    return paper_state()


@app.get("/api/paper")
@login_required
def paper_state():
    equity, pos, q = paper_equity(g.uid)
    for p in pos:
        price = q.get(p["symbol"], {}).get("price", p["entry"])
        p.update(price=price, value=round(price * p["shares"], 2),
                 pnl=round((price - p["entry"]) * p["shares"], 2),
                 pnl_pct=round((price / p["entry"] - 1) * 100, 2))
    start = setting("paper_starting_cash")
    return {"cash": setting("paper_cash"), "equity": round(equity, 2), "start": start,
            "return_pct": round((equity / start - 1) * 100, 2), "positions": pos,
            "orders": db.rows("SELECT * FROM paper_orders WHERE user_id=? ORDER BY id DESC LIMIT 50", (g.uid,))}


# ------------------------------------------------------------- portfolio

def portfolio_summary(hs, cash, exposure):
    """Pure portfolio maths (tested in tests/test_calcs.py). Each holding needs
    symbol, shares, cost_basis, price, sector, dividend_rate."""
    total = 0.0
    for h in hs:
        price = h.get("price")
        h["value"] = round(price * h["shares"], 2) if price else 0
        h["gain"] = round(h["value"] - h["cost_basis"] * h["shares"], 2) if price else None
        h["annual_income"] = round((h.get("dividend_rate") or 0) * h["shares"], 2)
        h["yield_pct"] = round(h["dividend_rate"] / price * 100, 2) if price and h.get("dividend_rate") else 0
        total += h["value"]
    sectors = defaultdict(float)
    for h in hs:
        h["weight_pct"] = round(h["value"] / total * 100, 2) if total else 0
        sectors[h.get("sector") or "Other"] += h["value"]
    equity_pct = total / (total + cash) * 100 if total + cash else 0
    warnings = [f"{h['symbol']} is {h['weight_pct']}% of equities (over 20%)" for h in hs if h["weight_pct"] > 20]
    if exposure and equity_pct > exposure["exposure_ceiling_pct"] + 0.5:
        warnings.append(f"Equity allocation {equity_pct:.0f}% is above the Exposure Coach ceiling of "
                        f"{exposure['exposure_ceiling_pct']}%. This is a guide for new risk, not a sell order.")
    return {"holdings": hs, "total_value": round(total, 2), "cash": cash,
            "equity_pct": round(equity_pct, 1),
            "annual_income": round(sum(h["annual_income"] for h in hs), 2),
            "cost": round(sum(h["cost_basis"] * h["shares"] for h in hs), 2),
            "sectors": {k: round(v, 2) for k, v in sorted(sectors.items(), key=lambda x: -x[1])},
            "exposure_ceiling": exposure and exposure["exposure_ceiling_pct"], "warnings": warnings}


@app.get("/api/holdings")
@login_required
def holdings():
    hs = db.rows("SELECT * FROM holdings WHERE user_id=? ORDER BY symbol", (g.uid,))
    q = market.quotes({h["symbol"] for h in hs})
    for h in hs:
        h.update(market.holding_info(h["symbol"]), price=q.get(h["symbol"], {}).get("price"),
                 change_pct=q.get(h["symbol"], {}).get("change_pct"))
    _, exposure = engines.latest("exposure", "exposure_posture_*.json")
    return portfolio_summary(hs, float(setting("portfolio_cash") or 0), exposure)


def _upsert_holding(sym, shares, cost, account=None):
    sym = market.clean_symbol(sym)
    if shares <= 0 or cost < 0:
        raise ValueError(f"{sym}: shares must be above zero and cost can't be negative.")
    db.execute("INSERT INTO holdings (user_id, symbol, shares, cost_basis, account) VALUES (?,?,?,?,?) "
               "ON CONFLICT (user_id, symbol) DO UPDATE SET shares=excluded.shares, cost_basis=excluded.cost_basis",
               (g.uid, sym, shares, cost, account))


@app.post("/api/holdings")
@login_required
def add_holding():
    b = body()
    if db.one("SELECT COUNT(*) AS n FROM holdings WHERE user_id=?", (g.uid,))["n"] >= 300:
        raise ValueError("Holdings are limited to 300 per account.")
    _upsert_holding(b.get("symbol"), num(b, "shares"), num(b, "cost_basis", positive=False), _text(b, "account", 60))
    return holdings()


@app.delete("/api/holdings/<int:hid>")
@login_required
def del_holding(hid):
    db.execute("DELETE FROM holdings WHERE id=? AND user_id=?", (hid, g.uid))
    return holdings()


@app.post("/api/holdings/import")
@login_required
def import_holdings():
    """CSV with columns symbol, shares, cost_basis (header names are case-insensitive)."""
    reader = csv.DictReader(io.StringIO((body().get("csv") or "").strip()))
    n, skipped = 0, []
    for row in reader:
        if n >= 300:
            break
        r = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        sym = r.get("symbol") or r.get("ticker")
        qty = r.get("shares") or r.get("quantity") or r.get("qty")
        cost = r.get("cost_basis") or r.get("avg_cost") or r.get("cost") or r.get("price") or "0"
        try:
            _upsert_holding(sym, float(qty.replace(",", "")), float(cost.replace(",", "").replace("$", "")))
            n += 1
        except (ValueError, AttributeError):
            skipped.append(sym or "?")
    return {**holdings(), "imported": n, "skipped": skipped[:20]}


# ------------------------------------------------------------- journal

@app.get("/api/journal")
@login_required
def journal():
    entries = db.rows("SELECT * FROM journal WHERE user_id=? ORDER BY COALESCE(closed_at, opened_at) DESC", (g.uid,))
    weekly = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    curve, running = [], 0.0
    for e in sorted(entries, key=lambda x: x["closed_at"] or ""):
        if e["pnl"] is None or not e["closed_at"]:
            continue
        dt = datetime.fromisoformat(e["closed_at"])
        wk = f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]:02d}"
        weekly[wk]["trades"] += 1
        weekly[wk]["wins"] += e["pnl"] > 0
        weekly[wk]["pnl"] = round(weekly[wk]["pnl"] + e["pnl"], 2)
        running += e["pnl"]
        curve.append({"t": e["closed_at"][:10], "pnl": round(running, 2)})
    followed = [e for e in entries if e["followed_plan"] is not None]
    stats = journal_stats(g.uid)
    stats["plan_adherence_pct"] = (round(sum(e["followed_plan"] for e in followed) / len(followed) * 100)
                                   if followed else None)
    stats["overrides"] = sum(1 for e in entries if e["overridden"])
    return {"entries": entries, "stats": stats, "curve": curve,
            "weekly": [{"week": k, **v} for k, v in sorted(weekly.items(), reverse=True)]}


def trade_math(entry, exitp, shares, stop=None):
    """P&L and R-multiple for a long trade (tested in tests/test_calcs.py)."""
    pnl = round((exitp - entry) * shares, 2)
    r = round(pnl / ((entry - stop) * shares), 2) if stop and stop < entry else None
    return pnl, r


def _iso(v):
    if not v:
        return db.now_iso()
    return datetime.fromisoformat(str(v)[:25]).isoformat()


@app.post("/api/journal")
@login_required
def add_journal():
    b = body()
    entry, exitp, shares = num(b, "entry"), num(b, "exit"), num(b, "shares")
    stop = num(b, "stop", False)
    pnl, r = trade_math(entry, exitp, shares, stop)
    fp = b.get("followed_plan")
    db.insert("INSERT INTO journal (user_id, source, symbol, shares, entry, exit, stop, pnl, r_multiple, setup, "
              "thesis, lessons, followed_plan, opened_at, closed_at, exit_reason) "
              "VALUES (?,'manual',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (g.uid, market.clean_symbol(b.get("symbol")), shares, entry, exitp, stop, pnl, r,
               _text(b, "setup", 100), _text(b, "thesis"), _text(b, "lessons"),
               None if fp in (None, "") else int(fp), _iso(b.get("opened_at")), _iso(b.get("closed_at")),
               _text(b, "exit_reason", 100)))
    return journal()


@app.patch("/api/journal/<int:jid>")
@login_required
def update_journal(jid):
    b = body()
    for field in ("lessons", "setup", "thesis", "followed_plan"):
        if field in b:
            v = b[field]
            v = (None if v in (None, "") else int(v)) if field == "followed_plan" else _text(b, field)
            db.execute(f"UPDATE journal SET {field}=? WHERE id=? AND user_id=?", (v, jid, g.uid))
    return journal()


@app.delete("/api/journal/<int:jid>")
@login_required
def delete_journal(jid):
    db.execute("DELETE FROM journal WHERE id=? AND user_id=?", (jid, g.uid))
    return journal()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5057)), debug=False)
