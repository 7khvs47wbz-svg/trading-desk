"""Password reset by emailed code: works, signs out other devices, resists guessing and enumeration."""
from datetime import datetime, timedelta, timezone

import pytest

import app as appmod
import db
import mailer

PW = "correct horse 1"


@pytest.fixture()
def sent(monkeypatch):
    appmod._hits.clear()
    box = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, body: box.append((to, subject, body)))
    return box


def client():
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def code_of(box):
    return box[-1][1].rsplit(" ", 1)[-1]


def test_reset_flow_signs_in_and_signs_out_other_devices(sent):
    a, other = client(), client()
    a.post("/api/auth/signup", json={"email": "r1@test.local", "password": PW})
    other.post("/api/auth/login", json={"email": "r1@test.local", "password": PW})
    assert other.get("/api/auth/me").json["email"] == "r1@test.local"
    a.post("/api/auth/logout", json={})
    r = a.post("/api/auth/forgot", json={"email": "R1@test.local"})
    assert r.status_code == 200 and len(sent) == 1 and sent[0][0] == "r1@test.local"
    code = code_of(sent)
    assert len(code) == 6 and code in sent[0][2]
    assert a.post("/api/auth/reset", json={"email": "r1@test.local", "code": code, "password": "short"}).status_code == 400
    r = a.post("/api/auth/reset", json={"email": "r1@test.local", "code": code, "password": "brand new pass"})
    assert r.status_code == 200 and a.get("/api/auth/me").json["email"] == "r1@test.local"
    assert other.get("/api/paper").status_code == 401  # other devices are signed out
    assert client().post("/api/auth/login", json={"email": "r1@test.local", "password": PW}).status_code == 400
    assert client().post("/api/auth/login", json={"email": "r1@test.local", "password": "brand new pass"}).status_code == 200
    # the code can't be used twice
    assert a.post("/api/auth/reset", json={"email": "r1@test.local", "code": code, "password": "another pass 9"}).status_code == 400


def test_unknown_email_gets_the_same_answer_and_no_email(sent):
    c = client()
    known = c.post("/api/auth/signup", json={"email": "r2@test.local", "password": PW})
    c.post("/api/auth/logout", json={})
    a = c.post("/api/auth/forgot", json={"email": "r2@test.local"}).json
    b = c.post("/api/auth/forgot", json={"email": "nobody@test.local"}).json
    assert known.status_code == 200 and a == b and len(sent) == 1


def test_wrong_codes_run_out_and_codes_expire(sent):
    c = client()
    c.post("/api/auth/signup", json={"email": "r3@test.local", "password": PW})
    c.post("/api/auth/forgot", json={"email": "r3@test.local"})
    code = code_of(sent)
    wrong = "000000" if code != "000000" else "111111"
    msgs = [c.post("/api/auth/reset", json={"email": "r3@test.local", "code": wrong, "password": "new pass 123"}).json["error"]
            for _ in range(5)]
    assert "4 tries left" in msgs[0] and "1 try left" in msgs[3] and "expired" in msgs[4]
    # after 5 misses even the right code is refused
    assert c.post("/api/auth/reset", json={"email": "r3@test.local", "code": code, "password": "new pass 123"}).status_code == 400
    uid = db.one("SELECT id FROM users WHERE email='r3@test.local'")["id"]
    db.execute("UPDATE password_resets SET created_at=? WHERE user_id=?",
               ((datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(), uid))
    c.post("/api/auth/forgot", json={"email": "r3@test.local"})
    code2 = code_of(sent)
    db.execute("UPDATE password_resets SET expires_at=? WHERE user_id=?",
               ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), uid))
    assert c.post("/api/auth/reset", json={"email": "r3@test.local", "code": code2, "password": "new pass 123"}).status_code == 400


def test_one_email_a_minute(sent):
    c = client()
    c.post("/api/auth/signup", json={"email": "r4@test.local", "password": PW})
    for _ in range(3):
        c.post("/api/auth/forgot", json={"email": "r4@test.local"})
    assert len(sent) == 1


def test_mailer_refuses_on_server_without_settings(monkeypatch):
    monkeypatch.delenv("EMAIL_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("RENDER", "1")
    with pytest.raises(ValueError, match="isn't switched on"):
        mailer.send("x@test.local", "s", "b")


def test_unconfigured_server_refuses_every_address_alike(monkeypatch):
    monkeypatch.delenv("EMAIL_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("RENDER", "1")
    appmod._hits.clear()
    c = client()
    c.post("/api/auth/signup", json={"email": "r5@test.local", "password": PW})
    a = c.post("/api/auth/forgot", json={"email": "r5@test.local"})
    b = c.post("/api/auth/forgot", json={"email": "nobody5@test.local"})
    assert a.status_code == b.status_code == 400 and a.json == b.json
