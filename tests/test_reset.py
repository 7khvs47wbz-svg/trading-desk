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


# ------------------------------------------------------------- recovery keys (no email needed)

import re


def test_signup_gives_a_recovery_key_that_resets_the_password():
    appmod._hits.clear()
    a, other = client(), client()
    r = a.post("/api/auth/signup", json={"email": "k1@test.local", "password": PW}).json
    key = r["recovery_key"]
    assert re.fullmatch(r"[A-Z2-9]{5}(-[A-Z2-9]{5}){3}", key)
    assert a.get("/api/auth/me").json["has_recovery_key"] is True
    other.post("/api/auth/login", json={"email": "k1@test.local", "password": PW})
    a.post("/api/auth/logout", json={})
    bad = a.post("/api/auth/recover", json={"email": "k1@test.local", "recovery_key": "AAAAA-AAAAA-AAAAA-AAAAA", "password": "new pass 123"})
    assert bad.status_code == 400 and "don't match" in bad.json["error"]
    sloppy = key.lower().replace("-", " ")  # typed without dashes, lower case
    r2 = a.post("/api/auth/recover", json={"email": "K1@test.local", "recovery_key": sloppy, "password": "new pass 123"})
    assert r2.status_code == 200 and r2.json["recovery_key"] != key
    assert a.get("/api/auth/me").json["email"] == "k1@test.local"
    assert other.get("/api/paper").status_code == 401  # other devices signed out
    assert client().post("/api/auth/login", json={"email": "k1@test.local", "password": "new pass 123"}).status_code == 200
    # the used key is gone; the new one works
    again = client().post("/api/auth/recover", json={"email": "k1@test.local", "recovery_key": key, "password": "x" * 9})
    assert again.status_code == 400
    assert client().post("/api/auth/recover", json={"email": "k1@test.local", "recovery_key": r2.json["recovery_key"],
                                                    "password": "third pass 1"}).status_code == 200


def test_settings_can_make_a_new_key_with_the_password():
    appmod._hits.clear()
    c = client()
    old = c.post("/api/auth/signup", json={"email": "k2@test.local", "password": PW}).json["recovery_key"]
    assert c.post("/api/auth/recovery-key", json={"password": "wrong"}).status_code == 400
    new = c.post("/api/auth/recovery-key", json={"password": PW}).json["recovery_key"]
    assert new != old
    c.post("/api/auth/logout", json={})
    assert c.post("/api/auth/recover", json={"email": "k2@test.local", "recovery_key": old, "password": "new pass 123"}).status_code == 400
    assert c.post("/api/auth/recover", json={"email": "k2@test.local", "recovery_key": new, "password": "new pass 123"}).status_code == 200


def test_unknown_email_and_options():
    appmod._hits.clear()
    c = client()
    r = c.post("/api/auth/recover", json={"email": "ghost@test.local", "recovery_key": "AAAAA-AAAAA-AAAAA-AAAAA", "password": "new pass 123"})
    assert r.status_code == 400 and "don't match" in r.json["error"]  # same answer as a wrong key
    o = c.get("/api/auth/options").json
    assert o["recovery_key_reset"] is True and "email_reset" in o
    assert c.get("/api/auth/me").json == {"email": None, "has_recovery_key": False}
