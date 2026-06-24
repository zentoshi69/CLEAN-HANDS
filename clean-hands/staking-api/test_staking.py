#!/usr/bin/env python3
"""Run: python test_staking.py  — exits non-zero on failure."""
import os, tempfile, base58
os.environ.setdefault("TG_COMMUNITY_TOKEN", "123456:TEST_token")
os.environ.setdefault("DEFAULT_TOKEN_MINT", "CLEANmint1111111111111111111111111111111111")
os.environ["STAKE_DB"] = tempfile.mktemp(suffix=".db")
os.environ["STAKE_SERVER_SECRET"] = "test-secret"
os.environ["STAKE_ADMIN_TOKEN"] = "admin-test-secret"
# payout window + claim fee are exercised by their dedicated group; off here so
# the rest of the suite tests each mechanism in isolation
os.environ["STAKE_PAYOUT_SETUP_DAYS"] = "0"
os.environ["STAKE_CLAIM_FEE_USD"] = "0"

import economics as econ
import auth
from nacl.signing import SigningKey

# Under pytest, every test runs in one process and shares the in-memory
# rate-limit store. Without a reset, cumulative /api/nonce calls trip the
# 30/min limit and later tests get a 429 instead of a nonce. Clear the window
# before each test so the suite is deterministic regardless of order or clock.
try:
    import pytest as _pytest

    @_pytest.fixture(autouse=True)
    def _reset_rate_limits():
        try:
            import store
            store.get_store()._counts.clear()
        except Exception:
            pass
        yield
except ImportError:
    pass


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
    # wallet-balance booster: SOL mandatory, CLEAN optional, $50–$500 band
    assert econ.wallet_balance_boost(0, 300) == 0.0       # never CLEAN-only
    assert econ.wallet_balance_boost(49, 400) == 0.0      # SOL below $50 floor
    assert approx(econ.wallet_balance_boost(50, 0), 0.10)   # SOL-only at floor
    assert approx(econ.wallet_balance_boost(500, 0), 0.50)  # cap
    assert approx(econ.wallet_balance_boost(9999, 0), 0.50)  # clamped to cap
    assert approx(econ.wallet_balance_boost(275, 0), 0.30)  # linear midpoint
    assert approx(econ.wallet_balance_boost(50, 49), 0.10)  # CLEAN <$50 not counted
    assert approx(econ.wallet_balance_boost(100, 9999), 0.50)  # CLEAN clamps total to cap
    # composition: $500 SOL adds +0.50x to the multiplier
    assert approx(econ.effective_apr(0, 0, 0, 0, sol_usd=500).effective_apr, 0.40 * 1.50)
    assert econ.effective_apr(0, 0, 0, 0).wallet_boost == 0.0  # backward-compatible default
    # Escape booster: the game's actual Escape multiplier, not leaderboard score.
    assert econ.escape_boost(4.99) == 0.0
    assert approx(econ.escape_boost(5), 0.20)
    assert approx(econ.escape_boost(10), 0.33)
    assert approx(econ.escape_boost(20), 0.50)
    assert approx(econ.escape_boost(33), 1.00)
    assert approx(econ.escape_boost(999), 1.00)  # hard cap
    assert approx(econ.escape_score_from_state({"S": {"prestige": 12}}), 10.0)
    esc = econ.effective_apr(0, 0, 0, 0, escape_score=33)
    assert approx(esc.escape_boost, 1.0)
    assert approx(esc.effective_apr, 0.40 * 2.0)
    print("economics ✓")


def test_price_guards():
    """C-1 regression: a thin/manipulated $CLEAN pool must not feed the booster.
    SOL/USD comes from an independent deep pool and is clamped to a sane band."""
    import asyncio
    import market

    orig_bp, orig_ind = market.best_pair, market._independent_sol_usd
    try:
        async def run():
            # thin CLEAN pool (below the liquidity floor) + healthy independent SOL
            async def thin():
                return {"priceUsd": "0.00001", "priceNative": "1e-10",
                        "quoteToken": {"symbol": "SOL"}, "liquidity": {"usd": 500}}

            async def ind150():
                return 150.0

            market.best_pair, market._independent_sol_usd = thin, ind150
            market._last_prices.update(clean_usd=0, sol_usd=0, ts=0)
            pr = await market.refresh_prices()
            assert pr["clean_usd"] == 0.0      # thin CLEAN price rejected
            assert pr["sol_usd"] == 150.0      # independent SOL price used

            # liquid pool but absurd priceNative -> SOL/USD explodes -> band rejects
            async def garbage():
                return {"priceUsd": "0.0004", "priceNative": "1e-11",
                        "quoteToken": {"symbol": "SOL"}, "liquidity": {"usd": 50000}}

            async def ind0():
                return 0.0

            market.best_pair, market._independent_sol_usd = garbage, ind0
            market._last_prices.update(clean_usd=0, sol_usd=0, ts=0)
            pr = await market.refresh_prices()
            assert pr["sol_usd"] == 0.0        # implausible SOL/USD rejected

        asyncio.run(run())
    finally:
        market.best_pair, market._independent_sol_usd = orig_bp, orig_ind
        market._last_prices.update(clean_usd=0, sol_usd=0, ts=0)
    print("price guards ✓")


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
    import db as _db, app, auth as _auth, solana
    from fastapi.testclient import TestClient

    c = TestClient(app.app)
    sk = SigningKey.generate()
    wallet = base58.b58encode(bytes(sk.verify_key)).decode()
    nonce = c.get("/api/nonce", params={"wallet": wallet}).json()["nonce"]
    msg = _auth.login_message(wallet, nonce)
    sig = base58.b58encode(sk.sign(msg.encode()).signature).decode()
    token = c.post("/api/login", json={"wallet": wallet, "signature": sig, "nonce": nonce}).json()["token"]

    # grant rewards directly (5.0 tokens = 5_000_000 base @ 6 decimals) and
    # backdate the stake clock past the 90-day claim lock
    import time as _t

    with _db.db() as conn:
        conn.execute(
            "UPDATE stakers SET accrued=?, stake_start_ts=? WHERE wallet=?",
            (5_000_000, int(_t.time()) - 91 * 86400, wallet),
        )
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
    async def fake_verify_transfer(tx_sig, destination_wallet, amount_base, mint=None):
        return tx_sig == "TX123" and destination_wallet == wallet and amount_base == 5_000_000

    solana.verify_transfer = fake_verify_transfer
    paid = c.post("/api/admin/mark_paid", json={"admin_token": adm, "claim_id": cid, "tx_sig": "TX123"}).json()
    assert paid["status"] == "paid"
    # idempotent: a second mark of the same claim fails
    assert c.post(
        "/api/admin/mark_paid", json={"admin_token": adm, "claim_id": cid, "tx_sig": "TX123"}
    ).status_code == 409
    print("manual claims state machine ✓")


def test_escape_booster_accrues_into_claim():
    """A Telegram-bound Escape x33 save must increase the same APR used by claim."""
    import json as _json, time as _t
    import db as _db, app, auth as _auth, solana, market
    from fastapi.testclient import TestClient

    async def fake_balance(wallet, mint=None):
        return 1_000_000.0

    async def fake_sol_balance(wallet):
        return 0.0

    async def fake_prices():
        return {"sol_usd": 0.0, "clean_usd": 0.0}

    solana.token_balance = fake_balance
    solana.sol_balance = fake_sol_balance
    market.refresh_prices = fake_prices
    market.last_prices = lambda: {"sol_usd": 0.0, "clean_usd": 0.0}

    c = TestClient(app.app)
    sk = SigningKey.generate()
    wallet = base58.b58encode(bytes(sk.verify_key)).decode()
    tg_id = 42424242
    token = _auth.create_session(wallet, tg_id)
    now = int(_t.time())
    staked = _db.to_base(1_000_000)
    # prestige 43 => Escape multiplier 1 + 0.75*43 = x33.25, capped to +100%.
    game_state = _json.dumps({"S": {"prestige": 43}, "meta": {"lastSeen": now}})
    with _db.db() as conn:
        _db.upsert_staker(conn, wallet, tg_id=tg_id, username="escaper")
        conn.execute(
            "UPDATE stakers SET recorded_staked=?, cached_balance=?, accrued=0, "
            "stake_start_ts=?, last_accrual_ts=?, payout_wallet=?, payout_confirmed_ts=? "
            "WHERE wallet=?",
            (staked, staked, now - 91 * 86400, now - 86400, wallet, now, wallet),
        )
        _db.game_save(conn, f"tg:{tg_id}", "escaper", game_state, score=123)
        row = _db.get_staker(conn, wallet)
        _eff, _secs, _refs, apr = app._apr_for(conn, wallet, row)

    assert approx(apr.escape_score, 33.25)
    assert approx(apr.escape_boost, 1.0)
    # Amount tier (+25%) + 90d loyalty (+15%) + Escape cap (+100%).
    assert approx(apr.effective_apr, 0.40 * (1 + 0.25 + 0.15 + 1.0))

    r = c.post("/api/claim", json={"token": token})
    assert r.status_code == 200, r.text
    body = r.json()
    # One day at the boosted APR on 1M $CLEAN is ~2,630 $CLEAN. Without the
    # Escape cap it would be ~1,534, so this proves claim uses the booster.
    assert body["claimed"] > 2500, body
    assert body["profile"]["apr"]["escape_boost"] == 1.0
    print("escape booster accrues into claim ✓")


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
    # Tight nonce limit, then confirm the (N+1)th request is 429. Restore the
    # limit afterwards so this test can't poison later tests under pytest's
    # definition order (where this runs before the other nonce users).
    import importlib
    import ratelimit
    import app, store

    prev = os.environ.get("RL_NONCE")
    os.environ["RL_NONCE"] = "3"
    importlib.reload(ratelimit)  # pick up the new limit
    store.get_store()._counts.clear()  # reset counters from earlier tests (same IP)
    from fastapi.testclient import TestClient

    try:
        c = TestClient(app.app)
        w = base58.b58encode(bytes(SigningKey.generate().verify_key)).decode()
        codes = [c.get("/api/nonce", params={"wallet": w}).status_code for _ in range(5)]
        assert codes[:3] == [200, 200, 200], codes
        assert 429 in codes[3:], codes
        print("rate limit -> 429 ✓")
    finally:
        if prev is None:
            os.environ.pop("RL_NONCE", None)
        else:
            os.environ["RL_NONCE"] = prev
        importlib.reload(ratelimit)  # restore default limits for later tests
        store.get_store()._counts.clear()


def test_relay():
    """Wallet-callback relay: one-time write, single read, validated ids."""
    import app
    from fastapi.testclient import TestClient

    c = TestClient(app.app)
    rid = "5KQvfYV2zJxA3p9rW8mN4cTuD"  # well-formed base58 id
    # not ready yet
    assert c.get(f"/api/relay/{rid}").status_code == 404
    # bounce page stores the encrypted callback params
    p = {"phantom_encryption_public_key": "Pub", "data": "EncData", "nonce": "N0nce"}
    r = c.post(f"/api/relay/{rid}", json={"params": p})
    assert r.status_code == 200, r.text
    # reads PEEK (a relaunched webview must still find the payload) ...
    assert c.get(f"/api/relay/{rid}").json()["params"] == p
    assert c.get(f"/api/relay/{rid}").json()["params"] == p
    # ... until the webview acks after processing
    assert c.delete(f"/api/relay/{rid}").status_code == 200
    assert c.get(f"/api/relay/{rid}").status_code == 404
    # malformed ids and oversized payloads are rejected
    assert c.post("/api/relay/bad!id", json={"params": p}).status_code == 400
    assert (
        c.post(f"/api/relay/{rid}", json={"params": {str(i): "x" for i in range(9)}}).status_code
        == 400
    )
    # the bounce page itself is served
    assert c.get("/wallet-return").status_code == 200
    print("wallet relay handoff ✓")


def test_tg_handshake():
    """Full server-side Telegram wallet handshake: start -> connect cb -> sign cb
    -> poll, simulating the wallet's crypto exactly as Phantom would."""
    import app, auth as _a
    from fastapi.testclient import TestClient
    import json as _json, time as _time, hmac as _hmac, hashlib as _hashlib, urllib.parse as _url
    import re as _re
    from nacl.public import PrivateKey, PublicKey, Box
    from nacl.utils import random as _rnd

    c = TestClient(app.app)
    TOKEN = os.environ["TG_COMMUNITY_TOKEN"]

    def init_data(uid):
        pairs = {"user": _json.dumps({"id": uid, "username": "u" + str(uid)}), "auth_date": str(int(_time.time()))}
        dcs = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = _hmac.new(b"WebAppData", TOKEN.encode(), _hashlib.sha256).digest()
        pairs["hash"] = _hmac.new(secret, dcs.encode(), _hashlib.sha256).hexdigest()
        return _url.urlencode(pairs)

    wsk = SigningKey.generate()
    wallet = base58.b58encode(bytes(wsk.verify_key)).decode()
    idata = init_data(424242)

    # 1) start -> ephemeral server pubkey
    r = c.post("/api/tg/start", json={"initData": idata, "wallet": "phantom"})
    assert r.status_code == 200, r.text
    sid, dapp_pub = r.json()["sid"], r.json()["dapp_pub"]
    # bad initData is refused
    assert c.post("/api/tg/start", json={"initData": "garbage", "wallet": "phantom"}).status_code == 401

    # 2) wallet side: shared box, encrypt the connect reply, hit the connect cb
    wkx = PrivateKey.generate()
    shared = Box(wkx, PublicKey(base58.b58decode(dapp_pub)))
    n1 = _rnd(24)
    ct1 = shared.encrypt(_json.dumps({"public_key": wallet, "session": "wsess"}).encode(), n1).ciphertext
    r2 = c.get(
        f"/api/tg/connect/{sid}",
        params={
            "phantom_encryption_public_key": base58.b58encode(bytes(wkx.public_key)).decode(),
            "data": base58.b58encode(ct1).decode(),
            "nonce": base58.b58encode(n1).decode(),
        },
    )
    assert r2.status_code == 200 and "signMessage" in r2.text
    # primary button must use the custom scheme (opens the installed app
    # unconditionally); the https UL stays as the fallback link
    assert "phantom://ul/v1/signMessage?" in r2.text
    ul = _re.search(r"(https://phantom\.app/ul/v1/signMessage\?[^\"']+)", r2.text).group(1)
    q = _url.parse_qs(_url.urlparse(ul).query)
    info = _json.loads(shared.decrypt(base58.b58decode(q["payload"][0]), base58.b58decode(q["nonce"][0])))
    msg_bytes = base58.b58decode(info["message"])  # exact bytes the server wants signed

    # 3) wallet signs the message, encrypts the signature, hits the sign cb
    sig = base58.b58encode(wsk.sign(msg_bytes).signature).decode()
    n2 = _rnd(24)
    ct2 = shared.encrypt(_json.dumps({"signature": sig}).encode(), n2).ciphertext
    r3 = c.get(
        f"/api/tg/sign/{sid}",
        params={"data": base58.b58encode(ct2).decode(), "nonce": base58.b58encode(n2).decode()},
    )
    assert r3.status_code == 200 and "Signed" in r3.text

    # 4) the webview polls and gets a working session + profile
    j = c.post("/api/tg/poll", json={"initData": idata, "sid": sid}).json()
    assert j["status"] == "done" and j["token"] and j["profile"]["wallet"] == wallet
    assert c.post("/api/profile", json={"token": j["token"]}).status_code == 200
    # recovery after a localStorage wipe: poll with NO sid resolves via tglast
    assert c.post("/api/tg/poll", json={"initData": idata}).json()["status"] == "done"
    # another Telegram user can never read this handshake
    assert c.post("/api/tg/poll", json={"initData": init_data(999999), "sid": sid}).json()["status"] == "pending"

    # a forged signature (wrong key over the right message) is rejected
    r = c.post("/api/tg/start", json={"initData": init_data(515151), "wallet": "phantom"})
    sid2, dapp2 = r.json()["sid"], r.json()["dapp_pub"]
    w2 = SigningKey.generate()
    wallet2 = base58.b58encode(bytes(w2.verify_key)).decode()
    sh2 = Box(PrivateKey.generate(), PublicKey(base58.b58decode(dapp2)))
    # need sh2's wallet-side pubkey to match what we encrypt with — rebuild cleanly
    wkx2 = PrivateKey.generate()
    sh2 = Box(wkx2, PublicKey(base58.b58decode(dapp2)))
    nn = _rnd(24)
    cc = sh2.encrypt(_json.dumps({"public_key": wallet2, "session": "x"}).encode(), nn).ciphertext
    rc = c.get(
        f"/api/tg/connect/{sid2}",
        params={
            "phantom_encryption_public_key": base58.b58encode(bytes(wkx2.public_key)).decode(),
            "data": base58.b58encode(cc).decode(),
            "nonce": base58.b58encode(nn).decode(),
        },
    )
    ul2 = _re.search(r"(https://phantom\.app/ul/v1/signMessage\?[^\"']+)", rc.text).group(1)
    q2 = _url.parse_qs(_url.urlparse(ul2).query)
    info2 = _json.loads(sh2.decrypt(base58.b58decode(q2["payload"][0]), base58.b58decode(q2["nonce"][0])))
    bad_sig = base58.b58encode(SigningKey.generate().sign(base58.b58decode(info2["message"])).signature).decode()
    n3 = _rnd(24)
    cc3 = sh2.encrypt(_json.dumps({"signature": bad_sig}).encode(), n3).ciphertext
    rs = c.get(f"/api/tg/sign/{sid2}", params={"data": base58.b58encode(cc3).decode(), "nonce": base58.b58encode(n3).decode()})
    assert "Signed" not in rs.text
    assert c.post("/api/tg/poll", json={"initData": init_data(515151), "sid": sid2}).json()["status"] == "error"
    print("tg server handshake ✓")


def test_ref_codes():
    """Short shareable referral codes: stable per wallet, resolvable at login."""
    import app, db as _db
    from fastapi.testclient import TestClient

    c = TestClient(app.app)

    def login(sk, ref=None):
        wallet = base58.b58encode(bytes(sk.verify_key)).decode()
        nonce = c.get("/api/nonce", params={"wallet": wallet}).json()["nonce"]
        msg = auth.login_message(wallet, nonce)
        sig = base58.b58encode(sk.sign(msg.encode()).signature).decode()
        return c.post("/api/login", json={"wallet": wallet, "signature": sig, "nonce": nonce, "ref": ref})

    a = SigningKey.generate()
    ra = login(a)
    assert ra.status_code == 200, ra.text
    code = ra.json()["profile"]["ref_code"]
    assert code and len(code) == 6 and all(ch in _db._REF_ALPHABET for ch in code), code
    # stable across logins
    assert login(a).json()["profile"]["ref_code"] == code
    # a friend joins with the code (messy casing + spaces still resolves)
    b = SigningKey.generate()
    rb = login(b, ref=" " + code.lower() + " ")
    assert rb.status_code == 200, rb.text
    wa = base58.b58encode(bytes(a.verify_key)).decode()
    wb = base58.b58encode(bytes(b.verify_key)).decode()
    with _db.db() as conn:
        assert _db.get_staker(conn, wb)["referred_by"] == wa
    # /api/referrals exposes the code
    tok = ra.json()["token"]
    j = c.post("/api/referrals", json={"token": tok}).json()
    assert j["ref_code"] == code
    assert j["link"].endswith(f"/g/{code}")
    assert "startapp=" not in j["link"]
    # self-referral with your own code is ignored
    s = SigningKey.generate()
    rs = login(s)
    own = rs.json()["profile"]["ref_code"]
    ws = base58.b58encode(bytes(s.verify_key)).decode()
    with _db.db() as conn:
        assert _db.get_staker(conn, ws)["referred_by"] is None
        assert _db.wallet_by_ref_code(conn, own) == ws

    # /g/<code> landing: branded OG page for a real code, 404 for junk
    g = c.get(f"/g/{code.lower()}")  # case-insensitive
    assert g.status_code == 200 and code in g.text and "og:image" in g.text
    assert c.get("/g/NOPE99").status_code == 404
    assert c.get("/g/<script>").status_code in (404, 422)

    # season campaign appears in /api/stats when configured
    import time as _t

    os.environ["STAKE_TOTAL_SUPPLY"] = "1000000000"
    os.environ["SEASON_END_TS"] = str(int(_t.time()) + 60 * 86400)
    try:
        se = c.get("/api/stats").json().get("season")
        assert se and se["goal_pct"] == 5.0 and se["goal_tokens"] == 50_000_000.0
        assert 1 <= se["days_left"] <= 60 and 0 <= se["progress_pct"] <= 100
    finally:
        os.environ.pop("SEASON_END_TS", None)
    assert "season" not in c.get("/api/stats").json()
    print("ref codes + glove links + season ✓")


def test_claim_lock_and_forfeit():
    """90-day claim vesting + unstake forfeits pending rewards (ledgered)."""
    import app, db as _db, auth as _auth
    import time as _t
    from fastapi.testclient import TestClient

    c = TestClient(app.app)
    sk = SigningKey.generate()
    wallet = base58.b58encode(bytes(sk.verify_key)).decode()
    nonce = c.get("/api/nonce", params={"wallet": wallet}).json()["nonce"]
    sig = base58.b58encode(sk.sign(_auth.login_message(wallet, nonce).encode()).signature).decode()
    token = c.post("/api/login", json={"wallet": wallet, "signature": sig, "nonce": nonce}).json()["token"]

    now = int(_t.time())
    with _db.db() as conn:
        conn.execute(
            "UPDATE stakers SET accrued=?, recorded_staked=?, cached_balance=?, "
            "balance_ts=?, stake_start_ts=? WHERE wallet=?",
            (3_000_000, 10_000_000, 10_000_000, now, now - 10 * 86400, wallet),
        )
        conn.commit()

    # locked at day 10 -> 400 with the unlock message; profile says so too
    r = c.post("/api/claim", json={"token": token})
    assert r.status_code == 400 and "unlock" in r.json()["detail"], r.text
    p = c.post("/api/profile", json={"token": token}).json()
    assert p["claim_locked"] is True and 79 <= p["claim_unlock_in_days"] <= 80, p

    # unstake -> pending forfeited to 0, forfeit ledgered, clock reset
    p2 = c.post("/api/unstake", json={"token": token}).json()
    assert p2["pending_rewards"] == 0.0 and p2["staked"] == 0.0, p2
    with _db.db() as conn:
        f = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM ledger WHERE wallet=? AND action='forfeit'",
            (wallet,),
        ).fetchone()["s"]
        assert f >= 3_000_000, f

    # past the lock -> claim succeeds
    with _db.db() as conn:
        conn.execute(
            "UPDATE stakers SET accrued=?, stake_start_ts=? WHERE wallet=?",
            (2_000_000, now - 91 * 86400, wallet),
        )
        conn.commit()
    r = c.post("/api/claim", json={"token": token})
    assert r.status_code == 200 and r.json()["claimed"] == 2.0, r.text
    # economics exposes the rule for the UI
    assert c.get("/api/economics").json()["claim_lock_days"] == 90
    print("claim lock + unstake forfeit ✓")


def test_payout_and_fee():
    """Payout-wallet setup window (opens pre-unlock) + $5 claim fee in $CLEAN."""
    import app, db as _db, auth as _auth, market as _mkt
    import time as _t
    from fastapi.testclient import TestClient

    c = TestClient(app.app)
    sk = SigningKey.generate()
    wallet = base58.b58encode(bytes(sk.verify_key)).decode()
    nonce = c.get("/api/nonce", params={"wallet": wallet}).json()["nonce"]
    sig = base58.b58encode(sk.sign(_auth.login_message(wallet, nonce).encode()).signature).decode()
    token = c.post("/api/login", json={"wallet": wallet, "signature": sig, "nonce": nonce}).json()["token"]

    os.environ["STAKE_PAYOUT_SETUP_DAYS"] = "3"
    os.environ["STAKE_CLAIM_FEE_USD"] = "5"
    real_best_pair = _mkt.best_pair

    async def fake_pair(mint=None):
        return {"baseToken": {"symbol": "CLEAN"}, "priceUsd": "0.05"}  # fee = 100 $CLEAN

    _mkt.best_pair = fake_pair
    now = int(_t.time())
    try:
        # day 30 of 90: window closed -> payout setup refused, claim locked
        with _db.db() as conn:
            conn.execute(
                "UPDATE stakers SET accrued=?, stake_start_ts=? WHERE wallet=?",
                (150_000_000, now - 30 * 86400, wallet),
            )
            conn.commit()
        def _payout(address=None):
            body = {"token": token}
            if address is not None:
                body["address"] = address
            nr = c.post("/api/payout/nonce", json=body)
            if nr.status_code != 200:
                return nr
            nonce_body = nr.json()
            s = base58.b58encode(sk.sign(nonce_body["message"].encode()).signature).decode()
            return c.post(
                "/api/payout",
                json={
                    "token": token,
                    "address": nonce_body.get("address"),
                    "nonce": nonce_body["nonce"],
                    "signature": s,
                },
            )

        # a stolen session token alone (no fresh signature) cannot set the payout
        assert c.post("/api/payout", json={"token": token, "address": "x"}).status_code == 401
        r = _payout()
        assert r.status_code == 400 and "opens" in r.json()["detail"], r.text
        p = c.post("/api/profile", json={"token": token}).json()
        assert p["payout_setup_open"] is False and p["claim_fee_usd"] == 5.0

        # day 88: window open -> confirm a CUSTOM payout address
        other = base58.b58encode(bytes(SigningKey.generate().verify_key)).decode()
        with _db.db() as conn:
            conn.execute("UPDATE stakers SET stake_start_ts=? WHERE wallet=?", (now - 88 * 86400, wallet))
            conn.commit()
        p = c.post("/api/profile", json={"token": token}).json()
        assert p["payout_setup_open"] is True and p["payout_confirmed"] is False
        assert _payout("junk").status_code == 400  # invalid address (sig valid, window open)
        p = _payout(other).json()
        assert p["payout_confirmed"] is True and p["payout_wallet"] == other

        # day 91: claim -> $5 fee at $0.05 = 100 $CLEAN deducted; net 50 paid
        with _db.db() as conn:
            conn.execute("UPDATE stakers SET stake_start_ts=? WHERE wallet=?", (now - 91 * 86400, wallet))
            conn.commit()
        r = c.post("/api/claim", json={"token": token}).json()
        assert r["claimed"] == 50.0 and r["fee"] == 100.0 and r["fee_usd"] == 5.0, r
        with _db.db() as conn:
            fee = conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS s FROM ledger WHERE wallet=? AND action='fee'",
                (wallet,),
            ).fetchone()["s"]
            assert fee == 100_000_000, fee
        # pending below the fee -> refused with the fee amount in the message
        with _db.db() as conn:
            conn.execute("UPDATE stakers SET accrued=? WHERE wallet=?", (50_000_000, wallet))
            conn.commit()
        r = c.post("/api/claim", json={"token": token})
        assert r.status_code == 400 and "fee" in r.json()["detail"], r.text

        # a fresh unlocked wallet WITHOUT payout confirmation cannot claim
        sk2 = SigningKey.generate()
        w2 = base58.b58encode(bytes(sk2.verify_key)).decode()
        n2 = c.get("/api/nonce", params={"wallet": w2}).json()["nonce"]
        s2 = base58.b58encode(sk2.sign(_auth.login_message(w2, n2).encode()).signature).decode()
        t2 = c.post("/api/login", json={"wallet": w2, "signature": s2, "nonce": n2}).json()["token"]
        with _db.db() as conn:
            conn.execute(
                "UPDATE stakers SET accrued=?, stake_start_ts=? WHERE wallet=?",
                (200_000_000, now - 91 * 86400, w2),
            )
            conn.commit()
        r = c.post("/api/claim", json={"token": t2})
        assert r.status_code == 400 and "payout" in r.json()["detail"], r.text
        # economics exposes both knobs
        e = c.get("/api/economics").json()
        assert e["claim_fee_usd"] == 5.0 and e["payout_setup_days"] == 3
    finally:
        _mkt.best_pair = real_best_pair
        os.environ["STAKE_PAYOUT_SETUP_DAYS"] = "0"
        os.environ["STAKE_CLAIM_FEE_USD"] = "0"
    print("payout setup window + $5 claim fee ✓")


def test_readyz_ops_flags_and_effective_rank():
    """Readiness endpoint, operator pause flags, ghost-stake exclusion, and
    finalized-transfer settlement are all 10k-launch gates."""
    import app, db as _db, solana
    import time as _t
    from fastapi.testclient import TestClient

    c = TestClient(app.app)
    adm = os.environ["STAKE_ADMIN_TOKEN"]

    async def fake_balance(wallet, mint=None):
        return 1_000.0

    solana.token_balance = fake_balance

    rz = c.get("/readyz")
    assert rz.status_code == 200, rz.text
    body = rz.json()
    assert body["ok"] is True and body["db"] is True and body["store"]["ok"] is True

    # Operator kill switch fails closed for staking and makes readyz non-ready.
    assert c.post(
        "/api/admin/set_flag",
        json={"admin_token": adm, "key": "halt_staking", "value": True},
    ).json()["ok"] is True
    assert c.post(
        "/api/admin/flags",
        json={"admin_token": adm},
    ).json()["flags"]

    sk = SigningKey.generate()
    wallet = base58.b58encode(bytes(sk.verify_key)).decode()
    n = c.get("/api/nonce", params={"wallet": wallet}).json()["nonce"]
    sig = base58.b58encode(sk.sign(auth.login_message(wallet, n).encode()).signature).decode()
    token = c.post("/api/login", json={"wallet": wallet, "signature": sig, "nonce": n}).json()["token"]
    halted = c.post("/api/stake", json={"token": token})
    assert halted.status_code == 503 and "paused" in halted.json()["detail"]
    c.post("/api/admin/set_flag", json={"admin_token": adm, "key": "halt_staking", "value": False})

    # Ghost stake: recorded_staked alone must not rank/count if cached balance is zero.
    with _db.db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO stakers "
            "(wallet, recorded_staked, cached_balance, balance_ts, stake_start_ts, last_accrual_ts, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("Ghost111111111111111111111111111111111111", 9_999_000_000_000, 0, int(_t.time()), 1, 1, 1),
        )
        conn.commit()
    lb = c.post("/api/leaderboard", json={"token": token}).json()["leaderboard"]
    assert all(r["name"] != "Ghos…1111" for r in lb)
    stats = c.get("/api/stats").json()
    assert stats["total_staked"] < 9_999_000

    # Admin settlement refuses an unverified tx, then accepts a verified transfer once.
    with _db.db() as conn:
        _db.create_claim(
            conn,
            wallet,
            7_000_000,
            gross_amount_base=7_000_000,
            destination=wallet,
            status="requested",
        )
        cid = conn.execute("SELECT MAX(id) AS id FROM claims WHERE wallet=?", (wallet,)).fetchone()["id"]

    async def no_transfer(*args, **kwargs):
        return False

    solana.verify_transfer = no_transfer
    bad = c.post("/api/admin/mark_paid", json={"admin_token": adm, "claim_id": cid, "tx_sig": "BADTX"})
    assert bad.status_code == 400

    async def yes_transfer(tx_sig, destination_wallet, amount_base, mint=None):
        return tx_sig == "GOODTX" and destination_wallet == wallet and amount_base == 7_000_000

    solana.verify_transfer = yes_transfer
    ok = c.post("/api/admin/mark_paid", json={"admin_token": adm, "claim_id": cid, "tx_sig": "GOODTX"})
    assert ok.status_code == 200 and ok.json()["status"] == "paid", ok.text
    print("readyz + ops flags + effective rank + settlement verify ✓")


def test_partial_stake():
    """Stake only a percent of the bag: floor math, bounds, re-stake updates."""
    import app, db as _db, solana
    from fastapi.testclient import TestClient

    async def fake_balance(wallet, mint=None):
        return 2_000_000.0

    solana.token_balance = fake_balance

    c = TestClient(app.app)
    sk = SigningKey.generate()
    wallet = base58.b58encode(bytes(sk.verify_key)).decode()
    nonce = c.get("/api/nonce", params={"wallet": wallet}).json()["nonce"]
    sig = base58.b58encode(sk.sign(auth.login_message(wallet, nonce).encode()).signature).decode()
    token = c.post("/api/login", json={"wallet": wallet, "signature": sig, "nonce": nonce}).json()["token"]

    # Pin the balance locally so the test does not depend on any previous
    # suite-wide monkeypatch state.
    half = c.post("/api/stake", json={"token": token, "percent": 50}).json()
    assert half["staked"] == 1_000_000, half
    # bounds rejected
    assert c.post("/api/stake", json={"token": token, "percent": 0}).status_code == 400
    assert c.post("/api/stake", json={"token": token, "percent": 101}).status_code == 400
    # default = everything (back-compat: old clients send no percent)
    full = c.post("/api/stake", json={"token": token}).json()
    assert full["staked"] == 2_000_000, full
    # ledger recorded the partial amount, not the full bag
    with _db.db() as conn:
        amts = [r["amount"] for r in conn.execute(
            "SELECT amount FROM ledger WHERE wallet=? AND action='stake' ORDER BY id", (wallet,)
        ).fetchall()]
    assert amts[0] == 1_000_000 * _db.BASE and amts[1] == 2_000_000 * _db.BASE, amts
    print("partial stake ✓")


def test_sliding_sessions_and_headers():
    """48h sessions silently re-mint after 6h of age; security headers on every response."""
    import time as _t
    import app, auth as _auth
    from fastapi.testclient import TestClient

    c = TestClient(app.app)
    # young token -> no refresh offered
    t_young = _auth.create_session("WALLETX", None)
    assert _auth.maybe_refresh(_auth.verify_session(t_young)) is None
    # aged token (simulate by back-dating exp) -> refresh minted and verifies
    payload = _auth.verify_session(t_young)
    payload["exp"] = int(_t.time()) + _auth.SESSION_TTL - _auth.SESSION_REFRESH_AFTER - 1
    fresh = _auth.maybe_refresh(payload)
    assert fresh and _auth.verify_session(fresh)["w"] == "WALLETX"
    # headers present on a plain API response
    r = c.get("/api/economics")
    assert "Content-Security-Policy" in r.headers and "script-src" in r.headers["Content-Security-Policy"]
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    print("sliding sessions + security headers \u2713")


def test_csp_permits_the_webapps_inline_handlers():
    """Regression for the 2026-06-12 dead-buttons incident: index.html wires its
    buttons with inline onclick="App.*" attributes, and attribute handlers are
    governed by CSP script-src. If anyone re-tightens script-src (drops
    'unsafe-inline') while ANY inline handler still exists in the served HTML,
    every button in the Mini App silently dies. This test fails loudly instead.
    The day index.html is fully converted to addEventListener wiring, flip the
    assertion: inline == 0 should then REQUIRE 'unsafe-inline' to be gone."""
    import os as _os
    import re as _re
    import app
    from fastapi.testclient import TestClient

    c = TestClient(app.app)
    html = open(_os.path.join(_os.path.dirname(_os.path.abspath(app.__file__)), "webapp", "index.html")).read()
    inline = len(_re.findall(r'on(?:click|change|input|submit|keyup|keydown)\s*=\s*"', html))
    csp = c.get("/").headers["Content-Security-Policy"]
    script_src = _re.search(r"script-src ([^;]*)", csp).group(1)
    if inline:
        assert "'unsafe-inline'" in script_src, (
            f"index.html has {inline} inline event handlers but script-src lacks "
            "'unsafe-inline' \u2014 every Mini App button would be dead in production"
        )
    else:
        assert "'unsafe-inline'" not in script_src, (
            "no inline handlers remain \u2014 tighten the CSP back (drop 'unsafe-inline')"
        )
    print(f"CSP vs inline handlers ({inline}) \u2713")


def test_portfolio_multi_wallet():
    """Link wallet B (and C) under A's session with REAL signatures; the
    dashboard must aggregate live balances and enforce ownership + exclusivity."""
    import solana

    async def fake_balance(wallet, mint=None):
        return 1_000.0  # every wallet holds 1k $CLEAN on-chain

    solana.token_balance = fake_balance
    import app
    from fastapi.testclient import TestClient

    c = TestClient(app.app)

    def login(sk):
        w = base58.b58encode(bytes(sk.verify_key)).decode()
        n = c.get("/api/nonce", params={"wallet": w}).json()["nonce"]
        sig = base58.b58encode(sk.sign(auth.login_message(w, n).encode()).signature).decode()
        r = c.post("/api/login", json={"wallet": w, "signature": sig, "nonce": n})
        assert r.status_code == 200, r.text
        return w, r.json()["token"]

    def signed_nonce(sk):
        w = base58.b58encode(bytes(sk.verify_key)).decode()
        n = c.get("/api/nonce", params={"wallet": w}).json()["nonce"]
        sig = base58.b58encode(sk.sign(auth.login_message(w, n).encode()).signature).decode()
        return w, n, sig

    ska, skb, skc = SigningKey.generate(), SigningKey.generate(), SigningKey.generate()
    wa, tok_a = login(ska)

    # solo portfolio: just A
    p = c.post("/api/portfolio", json={"token": tok_a}).json()
    assert p["count"] == 1 and p["wallets"][0]["anchor"]

    # link B with a real signature
    wb, nb, sigb = signed_nonce(skb)
    p = c.post("/api/link", json={"token": tok_a, "wallet": wb, "signature": sigb, "nonce": nb})
    assert p.status_code == 200, p.text
    p = p.json()
    assert p["count"] == 2 and p["totals"]["balance"] == 2000.0

    # forged signature for C is rejected
    wc_, nc, _ = signed_nonce(skc)
    bad = base58.b58encode(ska.sign(auth.login_message(wc_, nc).encode()).signature).decode()
    assert c.post("/api/link", json={"token": tok_a, "wallet": wc_, "signature": bad, "nonce": nc}).status_code == 401

    # another portfolio CAN reclaim B — but only with B's own fresh signature
    # (proof of CURRENT control re-homes the wallet instead of dead-ending)
    wd, tok_d = login(SigningKey.generate())
    wb2, nb2, sigb2 = signed_nonce(skb)
    r = c.post("/api/link", json={"token": tok_d, "wallet": wb2, "signature": sigb2, "nonce": nb2})
    assert r.status_code == 200 and r.json()["count"] == 2
    # ...and B left A's portfolio in the process
    assert c.post("/api/portfolio", json={"token": tok_a}).json()["count"] == 1
    # reclaim it back for A so the rest of the test continues unchanged
    wb3, nb3, sigb3 = signed_nonce(skb)
    assert c.post("/api/link", json={"token": tok_a, "wallet": wb3, "signature": sigb3, "nonce": nb3}).status_code == 200

    # a linked wallet's own session sees the same cluster
    _, tok_b = login(skb)
    p = c.post("/api/portfolio", json={"token": tok_b}).json()
    assert p["count"] == 2 and any(x["me"] and x["wallet"] == wb for x in p["wallets"])

    # unlink B; anchor can't be unlinked
    assert c.post("/api/unlink", json={"token": tok_a, "wallet": wb}).json()["count"] == 1
    assert c.post("/api/unlink", json={"token": tok_a, "wallet": wa}).status_code == 400
    print("multi-wallet portfolio + reclaim ✓")


def test_accrue_concurrent_no_double_credit():
    """REGRESSION: concurrent accrual must credit a time window exactly ONCE.
    Pre-fix, every money path opened its own connection and ran a read-modify-
    write (accrued = accrued + reward) keyed only on wallet, so N concurrent
    accruals each added the reward for the SAME [last_ts, now] window — minting
    rewards. The fix conditions the UPDATE on `last_accrual_ts == prev` (CAS), so
    only the first writer credits the interval. Here we hammer app._accrue from
    8 threads and assert the result is one interval, not ~8x."""
    import threading, time as _t, db as _db, app

    def seed(w):
        start = int(_t.time()) - 3600  # 1h of unsettled accrual
        with _db.db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO stakers (wallet, recorded_staked, cached_balance, "
                "accrued, stake_start_ts, last_accrual_ts, created_at) VALUES (?,?,?,?,?,?,?)",
                (w, 10_000_000_000, 10_000_000_000, 0, start, start, 0),
            )
            conn.commit()

    # single-thread baseline: what ONE accrual credits for ~1h
    seed("BaseW1111111111111111111111111111111111111")
    with _db.db() as conn:
        app._accrue(conn, "BaseW1111111111111111111111111111111111111")
        base = conn.execute(
            "SELECT accrued FROM stakers WHERE wallet='BaseW1111111111111111111111111111111111111'"
        ).fetchone()["accrued"]
    assert base > 0, "baseline accrual should be positive"

    # concurrent: 8 threads release together against an identical fresh staker
    seed("RaceW1111111111111111111111111111111111111")
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        with _db.db() as conn:
            app._accrue(conn, "RaceW1111111111111111111111111111111111111")

    ths = [threading.Thread(target=worker) for _ in range(8)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    with _db.db() as conn:
        race = conn.execute(
            "SELECT accrued FROM stakers WHERE wallet='RaceW1111111111111111111111111111111111111'"
        ).fetchone()["accrued"]

    # Credited once (allowing only sub-second timing drift), never multiplied.
    assert race < 2 * base, f"double-credit regression: race={race} base={base}"
    print("concurrent accrual: no double-credit ✓")


def test_game_cloud_save_and_leaderboard():
    """Game cloud-save is keyed by verified Telegram identity, ranks by a
    monotonic lifetime-laundered score, caps the blob, and the track/ref pings
    no longer 404. Additive — never touches the staking tables."""
    import app
    from fastapi.testclient import TestClient
    import json as _json, time as _time, hmac as _hmac, hashlib as _hashlib, urllib.parse as _url

    c = TestClient(app.app)
    TOKEN = os.environ["TG_COMMUNITY_TOKEN"]

    def init_data(uid, username=None):
        user = _json.dumps({"id": uid, "username": username or ("u" + str(uid))})
        pairs = {"user": user, "auth_date": str(int(_time.time()))}
        dcs = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = _hmac.new(b"WebAppData", TOKEN.encode(), _hashlib.sha256).digest()
        pairs["hash"] = _hmac.new(secret, dcs.encode(), _hashlib.sha256).hexdigest()
        return _url.urlencode(pairs)

    # forged / empty initData is rejected on every authed game route
    assert c.post("/api/game/save", json={"initData": "garbage", "state": "{}", "score": 1}).status_code == 401
    assert c.post("/api/game/load", json={"initData": ""}).status_code == 401

    # save + load round-trips the opaque blob and the score
    idA = init_data(1001, "alice")
    blob = _json.dumps({"S": {"total": 5000}, "meta": {"lastSeen": 123}})
    r = c.post("/api/game/save", json={"initData": idA, "state": blob, "score": 5000})
    assert r.status_code == 200 and r.json().get("ok") is True
    r = c.post("/api/game/load", json={"initData": idA}).json()
    assert r["state"] == blob and r["score"] == 5000

    # score is monotonic (a stale client can't lower a rank) but the newest
    # state blob still persists
    blob2 = _json.dumps({"S": {"total": 10}, "meta": {"lastSeen": 999}})
    c.post("/api/game/save", json={"initData": idA, "state": blob2, "score": 10})
    r = c.post("/api/game/load", json={"initData": idA}).json()
    assert r["score"] == 5000 and r["state"] == blob2

    # Escape multiplier is separate from leaderboard score and cannot be lowered
    # by a stale save, because it now controls real staking rewards.
    blob3 = _json.dumps({"S": {"prestige": 12}, "meta": {"lastSeen": 1000}})
    r = c.post("/api/game/save", json={"initData": idA, "state": blob3, "score": 20}).json()
    assert r["escape_score"] == 10.0 and r["escape_boost"] == 0.33
    c.post("/api/game/save", json={"initData": idA, "state": blob2, "score": 30})
    kept = _json.loads(c.post("/api/game/load", json={"initData": idA}).json()["state"])
    assert kept["S"]["prestige"] == 12

    # oversized blob is rejected
    big = "x" * (app.GAME_STATE_MAX + 1)
    assert c.post("/api/game/save", json={"initData": idA, "state": big, "score": 1}).status_code == 413

    # leaderboard ranks by score desc, honours limit, is public (no auth needed)
    c.post("/api/game/save", json={"initData": init_data(1002, "bob"), "state": "{}", "score": 9000})
    c.post("/api/game/save", json={"initData": init_data(1003, "carol"), "state": "{}", "score": 1})
    top = c.get("/api/game/leaderboard", params={"limit": 2}).json()["top"]
    assert len(top) == 2
    assert top[0]["name"] == "bob" and top[0]["score"] == 9000
    assert top[1]["name"] == "alice" and top[1]["score"] == 5000

    # analytics + referral pings are accepted now (no more silent 404s)
    assert c.post("/api/track", json={"cid": "c1", "ev": []}).status_code == 204
    assert c.get("/api/ref", params={"action": "refer", "ref": "abc123", "nid": "def456"}).status_code == 204

    print("game cloud-save + leaderboard + track/ref ✓")


if __name__ == "__main__":
    test_economics()
    test_price_guards()
    test_auth_signature()
    test_sessions_and_nonce()
    test_api_flow()
    test_partial_stake()
    test_sliding_sessions_and_headers()
    test_csp_permits_the_webapps_inline_handlers()
    test_tg_collision()
    test_integer_migration()
    test_pg_translation()
    test_claims_manual()
    test_escape_booster_accrues_into_claim()
    test_robustness()
    test_reconcile()
    test_ref_codes()
    test_claim_lock_and_forfeit()
    test_payout_and_fee()
    test_readyz_ops_flags_and_effective_rank()
    test_portfolio_multi_wallet()
    test_accrue_concurrent_no_double_credit()
    test_relay()
    test_tg_handshake()
    test_game_cloud_save_and_leaderboard()
    test_rate_limit()  # exhausts the nonce bucket — keep it last
    print("\nALL STAKING TESTS PASSED")
