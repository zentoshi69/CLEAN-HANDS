#!/usr/bin/env python3
"""Run: python test_staking.py  — exits non-zero on failure."""
import os, tempfile, base58
os.environ.setdefault("TG_COMMUNITY_TOKEN", "123456:TEST_token")
os.environ.setdefault("DEFAULT_TOKEN_MINT", "CLEANmint1111111111111111111111111111111111")
os.environ["STAKE_DB"] = tempfile.mktemp(suffix=".db")
os.environ["STAKE_SERVER_SECRET"] = "test-secret"
os.environ["STAKE_ADMIN_TOKEN"] = "admin-test-secret"

import economics as econ
import auth
from nacl.signing import SigningKey


def approx(a, b, eps=1e-9):
    return abs(a - b) < eps


def test_economics():
    # amount tier: highest matching wins
    assert econ.amount_boost(50_000) == 0.0
    assert econ.amount_boost(100_000) == 0.10
    assert econ.amount_boost(2_000_000) == 0.25
    assert econ.amount_boost(20_000_000) == 0.50
    # loyalty cap
    assert approx(econ.loyalty_boost(0), 0.0)
    assert approx(econ.loyalty_boost(31 * 86400), 0.05)
    assert approx(econ.loyalty_boost(3650 * 86400), econ.LOYALTY_CAP)
    # referral cap
    assert approx(econ.referral_boost(3), 0.06)
    assert approx(econ.referral_boost(1000), econ.REFERRAL_CAP)
    # burn bonus
    assert approx(econ.burn_bonus_apr(200_000), 0.10)
    assert approx(econ.burn_bonus_apr(10**12), econ.BURN_CAP_APR)
    # effective APR composition: 2M staked (+25%), 0 loyalty/ref, 200k burned (+0.10)
    e = econ.effective_apr(2_000_000, 0, 0, 200_000)
    assert approx(e.effective_apr, 0.40 * 1.25 + 0.10)  # 0.60
    # accrual: 1,000,000 staked @ 50% for one year = 500,000
    assert approx(econ.accrue(1_000_000, 0.50, econ.SECONDS_PER_YEAR), 500_000)
    # anti-gaming: earn only on what you still hold
    assert econ.effective_staked(1_000_000, 400_000) == 400_000
    print("economics ✓")


def test_auth_signature():
    sk = SigningKey.generate()
    wallet = base58.b58encode(bytes(sk.verify_key)).decode()
    msg = auth.login_message(wallet, "nonce123")
    sig = base58.b58encode(sk.sign(msg.encode()).signature).decode()
    assert auth.verify_wallet_signature(wallet, msg, sig) is True
    # tampered message fails
    assert auth.verify_wallet_signature(wallet, msg + "x", sig) is False
    # wrong wallet fails
    other = base58.b58encode(bytes(SigningKey.generate().verify_key)).decode()
    assert auth.verify_wallet_signature(other, msg, sig) is False
    print("wallet signature ✓")


def test_sessions_and_nonce():
    t = auth.create_session("WALLET", 42)
    s = auth.verify_session(t)
    assert s and s["w"] == "WALLET" and s["t"] == 42
    assert auth.verify_session(t + "x") is None
    n = auth.issue_nonce("WALLET")
    assert auth.consume_nonce("WALLET", n) is True
    assert auth.consume_nonce("WALLET", n) is False  # single use
    print("sessions + nonce ✓")


def test_api_flow():
    import solana
    async def fake_balance(wallet, mint=None):
        return 2_000_000.0
    async def fake_verify_burn(sig, wallet, mint=None):
        return 200_000.0 if sig == "BURNSIG" else 0.0
    solana.token_balance = fake_balance
    solana.verify_burn = fake_verify_burn

    import app  # imports after monkeypatch targets exist
    from fastapi.testclient import TestClient
    c = TestClient(app.app)

    sk = SigningKey.generate()
    wallet = base58.b58encode(bytes(sk.verify_key)).decode()

    # login
    nonce = c.get("/api/nonce", params={"wallet": wallet}).json()["nonce"]
    msg = auth.login_message(wallet, nonce)
    sig = base58.b58encode(sk.sign(msg.encode()).signature).decode()
    r = c.post("/api/login", json={"wallet": wallet, "signature": sig, "nonce": nonce}).json()
    token = r["token"]
    assert r["profile"]["balance"] == 2_000_000
    assert r["profile"]["staked"] == 0

    # stake -> snapshots the on-chain balance
    p = c.post("/api/stake", json={"token": token}).json()
    assert p["staked"] == 2_000_000
    assert p["apr"]["amount_boost"] == 0.25
    assert approx(p["apr"]["effective_apr"], 0.50)

    # burn -> credits burn bonus, idempotent
    b = c.post("/api/burn", json={"token": token, "signature": "BURNSIG"}).json()
    assert b["burned"] == 200_000
    assert approx(b["profile"]["apr"]["burn_bonus_apr"], 0.10)
    dup = c.post("/api/burn", json={"token": token, "signature": "BURNSIG"})
    assert dup.status_code == 409  # no double credit
    # and the bonus must NOT have inflated from the duplicate attempt
    prof = c.post("/api/profile", json={"token": token}).json()
    assert prof["total_burned"] == 200_000
    assert approx(prof["apr"]["burn_bonus_apr"], 0.10)

    # invalid burn tx rejected
    bad = c.post("/api/burn", json={"token": token, "signature": "NOPE"})
    assert bad.status_code == 400

    # leaderboard shows me on top
    lb = c.post("/api/leaderboard", json={"token": token}).json()["leaderboard"]
    assert lb[0]["me"] and lb[0]["staked"] == 2_000_000

    # bad session rejected
    assert c.post("/api/profile", json={"token": "garbage"}).status_code == 401

    # --- Phase 1 hardening ---
    # invalid wallet rejected at the edge
    assert c.get("/api/nonce", params={"wallet": "not-a-wallet"}).status_code == 400
    # ledger recorded the money events (stake + burn at least)
    import db as _db
    with _db.db() as conn:
        actions = [r["action"] for r in conn.execute("SELECT action FROM ledger WHERE wallet=?", (wallet,)).fetchall()]
    assert "stake" in actions and "burn" in actions
    # deep healthz reports db + config
    hz = c.get("/healthz").json()
    assert hz["ok"] is True and hz["db"] is True and "env" in hz
    print("api flow ✓")
    print("phase1 hardening ✓")


def test_tg_collision():
    import db as _db, app
    from fastapi.testclient import TestClient
    import json as _json, time as _time, hmac as _hmac, hashlib as _hashlib, urllib.parse as _url

    c = TestClient(app.app)
    TOKEN = os.environ["TG_COMMUNITY_TOKEN"]

    def init_data(uid):
        user = _json.dumps({"id": uid, "username": "u" + str(uid)})
        pairs = {"user": user, "auth_date": str(int(_time.time()))}
        dcs = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = _hmac.new(b"WebAppData", TOKEN.encode(), _hashlib.sha256).digest()
        pairs["hash"] = _hmac.new(secret, dcs.encode(), _hashlib.sha256).hexdigest()
        return _url.urlencode(pairs)

    def login(sk, initdata):
        wallet = base58.b58encode(bytes(sk.verify_key)).decode()
        nonce = c.get("/api/nonce", params={"wallet": wallet}).json()["nonce"]
        msg = auth.login_message(wallet, nonce)
        sig = base58.b58encode(sk.sign(msg.encode()).signature).decode()
        return c.post("/api/login", json={"wallet": wallet, "signature": sig, "nonce": nonce, "initData": initdata})

    # same TG id, two different wallets -> second is 409, not 500
    a, b = SigningKey.generate(), SigningKey.generate()
    assert login(a, init_data(7777)).status_code == 200
    assert login(b, init_data(7777)).status_code == 409
    print("tg-id collision -> 409 ✓")


def test_integer_migration():
    """Legacy REAL token rows convert to integer base units, idempotently."""
    import sqlite3, tempfile, db as _db

    path = tempfile.mktemp(suffix=".db")
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE stakers (wallet TEXT PRIMARY KEY, recorded_staked REAL, cached_balance REAL,
            accrued REAL, claimed_total REAL, total_burned REAL);
        CREATE TABLE burns (signature TEXT PRIMARY KEY, wallet TEXT, amount REAL, ts INTEGER);
        CREATE TABLE ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, wallet TEXT,
            action TEXT, amount REAL, detail TEXT);
        """
    )
    con.execute("INSERT INTO stakers VALUES ('W', 1.5, 2.0, 0.25, 0, 0.5)")
    con.execute("INSERT INTO burns VALUES ('s', 'W', 0.5, 0)")
    con.execute("INSERT INTO ledger (ts,wallet,action,amount) VALUES (0,'W','burn',0.5)")
    con.commit()

    _db._migrate(con)
    r = con.execute("SELECT * FROM stakers WHERE wallet='W'").fetchone()
    assert r["recorded_staked"] == int(1.5 * _db.BASE) == 1_500_000
    assert r["accrued"] == int(0.25 * _db.BASE) == 250_000
    assert r["total_burned"] == int(0.5 * _db.BASE) == 500_000
    assert con.execute("PRAGMA user_version").fetchone()[0] == _db.SCHEMA_VERSION
    # idempotent: a second run must NOT multiply again
    _db._migrate(con)
    assert con.execute("SELECT recorded_staked FROM stakers WHERE wallet='W'").fetchone()[0] == 1_500_000
    con.close()
    os.remove(path)
    print("integer migration ✓")


def test_claims_manual():
    """Claim creates an idempotent 'requested' claim; admin marks it paid once."""
    import db as _db, app, auth as _auth
    from fastapi.testclient import TestClient

    c = TestClient(app.app)
    sk = SigningKey.generate()
    wallet = base58.b58encode(bytes(sk.verify_key)).decode()
    nonce = c.get("/api/nonce", params={"wallet": wallet}).json()["nonce"]
    msg = _auth.login_message(wallet, nonce)
    sig = base58.b58encode(sk.sign(msg.encode()).signature).decode()
    token = c.post("/api/login", json={"wallet": wallet, "signature": sig, "nonce": nonce}).json()["token"]

    # grant rewards directly (5.0 tokens = 5_000_000 base @ 6 decimals)
    with _db.db() as conn:
        conn.execute("UPDATE stakers SET accrued=? WHERE wallet=?", (5_000_000, wallet))
        conn.commit()

    r = c.post("/api/claim", json={"token": token}).json()
    assert r["status"] == "requested" and r["claimed"] == 5.0, r
    # nothing left to claim -> 400 (no double count)
    assert c.post("/api/claim", json={"token": token}).status_code == 400

    # admin auth enforced
    assert c.post("/api/admin/pending", json={"admin_token": "wrong"}).status_code == 403
    adm = os.environ["STAKE_ADMIN_TOKEN"]
    pend = c.post("/api/admin/pending", json={"admin_token": adm}).json()["pending"]
    mine = [p for p in pend if p["wallet"] == wallet]
    assert len(mine) == 1 and mine[0]["amount"] == 5.0
    cid = mine[0]["claim_id"]
    paid = c.post("/api/admin/mark_paid", json={"admin_token": adm, "claim_id": cid, "tx_sig": "TX123"}).json()
    assert paid["status"] == "paid"
    # idempotent: a second mark of the same claim fails
    assert c.post(
        "/api/admin/mark_paid", json={"admin_token": adm, "claim_id": cid, "tx_sig": "TX123"}
    ).status_code == 409
    print("manual claims state machine ✓")


def test_pg_translation():
    """Postgres dialect shim: ? -> %s and INSERT OR IGNORE -> ON CONFLICT DO NOTHING.
    Params stay bound (no string interpolation), so no injection is introduced."""
    import db as _db

    assert _db._translate("SELECT * FROM x WHERE a=? AND b=?") == "SELECT * FROM x WHERE a=%s AND b=%s"
    assert (
        _db._translate("INSERT OR IGNORE INTO burns (signature, wallet) VALUES (?,?)")
        == "INSERT INTO burns (signature, wallet) VALUES (%s,%s) ON CONFLICT DO NOTHING"
    )
    print("pg sql translation ✓")


def test_robustness():
    """Malformed/oversized input is rejected cleanly (no 500s); headers present."""
    import auth as _a, app
    from fastapi.testclient import TestClient

    c = TestClient(app.app)
    # oversized wallet rejected by the length cap before any decode
    assert c.get("/api/nonce", params={"wallet": "z" * 100}).status_code == 400
    # body too large -> 413 (not a crash)
    assert c.post("/api/profile", json={"token": "x" * 20000}).status_code == 413
    # malformed session token -> 401, never 500
    assert c.post("/api/profile", json={"token": "a.b"}).status_code == 401
    # junk initData is rejected by the verifier without raising
    assert _a.verify_init_data("garbage") is None
    assert _a.verify_init_data("") is None
    # hardening headers are attached
    h = c.get("/healthz").headers
    assert h.get("x-content-type-options") == "nosniff"
    print("robustness + headers ✓")


def test_reconcile():
    """Whole-DB invariants hold after all prior activity; injected drift is caught."""
    import db as _db, reconcile as _rec

    with _db.db() as conn:
        report = _rec.reconcile(conn)
    assert report["ok"], report["issues"]  # everything done so far is consistent

    # inject drift: bump a wallet's claimed_total out of band
    with _db.db() as conn:
        w = conn.execute("SELECT wallet FROM stakers LIMIT 1").fetchone()["wallet"]
        conn.execute("UPDATE stakers SET claimed_total = claimed_total + 1 WHERE wallet=?", (w,))
        conn.commit()
        bad = _rec.reconcile(conn)
    assert not bad["ok"] and any(i["wallet"] == w for i in bad["issues"]), bad
    print("reconciliation ✓")


def test_rate_limit():
    # Tight nonce limit, then confirm the (N+1)th request is 429.
    os.environ["RL_NONCE"] = "3"
    import importlib
    import ratelimit
    importlib.reload(ratelimit)  # pick up the new limit
    import app, store
    store.get_store()._counts.clear()  # reset counters from earlier tests (same IP)
    from fastapi.testclient import TestClient

    c = TestClient(app.app)
    w = base58.b58encode(bytes(SigningKey.generate().verify_key)).decode()
    codes = [c.get("/api/nonce", params={"wallet": w}).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200], codes
    assert 429 in codes[3:], codes
    print("rate limit -> 429 ✓")


if __name__ == "__main__":
    test_economics()
    test_auth_signature()
    test_sessions_and_nonce()
    test_api_flow()
    test_tg_collision()
    test_integer_migration()
    test_pg_translation()
    test_claims_manual()
    test_robustness()
    test_reconcile()
    test_rate_limit()
    print("\nALL STAKING TESTS PASSED")
