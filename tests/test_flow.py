"""End-to-end API tests: accounts, isolation between users, and paper-trade cash maths."""
import pytest

import app as appmod
import market


@pytest.fixture()
def client():
    appmod.app.config["TESTING"] = True
    appmod._hits.clear()
    return appmod.app.test_client()


def signup(c, email, pw="correct horse 1"):
    return c.post("/api/auth/signup", json={"email": email, "password": pw})


def test_requires_login(client):
    assert client.get("/api/paper").status_code == 401


def test_signup_login_logout(client):
    assert signup(client, "a@test.local").status_code == 200
    assert client.get("/api/auth/me").json["email"] == "a@test.local"
    assert signup(client, "a@test.local").status_code == 400  # duplicate
    client.post("/api/auth/logout", json={})
    assert client.get("/api/paper").status_code == 401
    assert client.post("/api/auth/login", json={"email": "a@test.local", "password": "nope"}).status_code == 400
    assert client.post("/api/auth/login", json={"email": "A@test.local", "password": "correct horse 1"}).status_code == 200


def test_short_password_and_bad_email(client):
    assert signup(client, "b@test.local", "short").status_code == 400
    assert signup(client, "not-an-email").status_code == 400


def test_csrf_needs_json(client):
    r = client.post("/api/auth/login", data={"email": "x", "password": "y"})
    assert r.status_code == 415


def test_paper_trade_cash_and_pnl_exact(client, monkeypatch):
    prices = {"AAPL": 200.00}
    monkeypatch.setattr(market, "quote", lambda s, verify=True: {
        "symbol": s, "price": prices[s], "atr": 4.0,
        "price_check": {"ok": True, "message": "ok"}})
    monkeypatch.setattr(market, "quotes", lambda syms: {s: {"price": prices[s], "change_pct": 0} for s in syms})
    signup(client, "trader@test.local")
    r = client.post("/api/paper/buy", json={"symbol": "AAPL", "shares": 37, "stop": 190,
                                             "gate_decision": "NO_GO", "override": True})
    assert r.status_code == 200, r.json
    assert r.json["cash"] == pytest.approx(100000 - 37 * 200.00, abs=0.01)
    pid = r.json["positions"][0]["id"]
    prices["AAPL"] = 215.55
    r = client.post(f"/api/paper/sell/{pid}", json={})
    assert r.json["cash"] == pytest.approx(100000 + 37 * 15.55, abs=0.01)
    j = client.get("/api/journal").json["entries"][0]
    assert j["pnl"] == pytest.approx(37 * 15.55, abs=0.01)
    assert j["r_multiple"] == pytest.approx(15.55 / 10, abs=0.01)
    assert j["overridden"] == 1


def test_trade_blocked_when_sources_disagree(client, monkeypatch):
    monkeypatch.setattr(market, "quote", lambda s, verify=True: {
        "symbol": s, "price": 100.0, "atr": 2.0,
        "price_check": {"ok": False, "message": "Yahoo and Nasdaq differ by 3.10%"}})
    signup(client, "blocked@test.local")
    r = client.post("/api/paper/buy", json={"symbol": "AAPL", "shares": 1, "gate_decision": "GO"})
    assert r.status_code == 400 and "blocked" in r.json["error"]


def test_users_cannot_see_each_others_data(client, monkeypatch):
    monkeypatch.setattr(market, "quotes", lambda syms: {})
    monkeypatch.setattr(market, "holding_info", lambda s: {"name": s, "sector": "Tech", "dividend_rate": 0})
    signup(client, "alice@test.local")
    client.post("/api/holdings", json={"symbol": "MSFT", "shares": 5, "cost_basis": 300})
    hid = client.get("/api/holdings").json["holdings"][0]["id"]
    client.post("/api/auth/logout", json={})
    signup(client, "bob@test.local")
    assert client.get("/api/holdings").json["holdings"] == []
    client.delete(f"/api/holdings/{hid}")  # Bob tries to delete Alice's row
    client.post("/api/auth/logout", json={})
    client.post("/api/auth/login", json={"email": "alice@test.local", "password": "correct horse 1"})
    assert len(client.get("/api/holdings").json["holdings"]) == 1


def test_invalid_symbol_rejected(client):
    signup(client, "sym@test.local")
    r = client.post("/api/holdings", json={"symbol": "X');alert(1)//", "shares": 1, "cost_basis": 1})
    assert r.status_code == 400


def test_delete_account(client):
    signup(client, "gone@test.local")
    assert client.post("/api/auth/delete", json={"password": "wrong"}).status_code == 400
    assert client.post("/api/auth/delete", json={"password": "correct horse 1"}).status_code == 200
    assert client.post("/api/auth/login", json={"email": "gone@test.local", "password": "correct horse 1"}).status_code == 400
