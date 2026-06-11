#!/usr/bin/env python3
"""
CLEAN soft-staking API — the SINGLE SOURCE OF TRUTH.

Your website and the Telegram Mini App are both clients of this one backend, so
they always show identical numbers (staked amount, boosters, burn yield, rewards).

Auth: Solana wallet signature (same login as the site). In the Mini App we also
verify Telegram initData to bind telegram_id <-> wallet.

    pip install -r requirements.txt
    export TG_COMMUNITY_TOKEN=...        # for Mini App initData verification
    export DEFAULT_TOKEN_MINT=...        # the $CLEAN mint
    export SOLANA_RPC_URL=...            # a real RPC (Helius/Triton) in prod
    export STAKE_SERVER_SECRET=$(openssl rand -hex 32)
    python app.py                        # :8090

See README.md for the economics and the full flow.
"""

from __future__ import annotations

import os
import re
import json
import time
import hmac

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel

import db
import auth
import config
import economics as econ
import market
import ratelimit
import solana
import store

CONFIG_STATUS = config.validate_config()  # fail-fast on misconfig (prod)
BALANCE_TTL = int(os.environ.get("STAKE_BALANCE_TTL", "300"))  # re-check chain every 5 min
# Explicit allow-list so your website's browser can call this API cross-origin.
# Comma-separated origins, e.g. "https://clean.fun,https://app.clean.fun".
# The Telegram Mini App webview sends requests from the app's own HTTPS origin.
_CORS = [o.strip() for o in os.environ.get("STAKE_CORS_ORIGINS", "").split(",") if o.strip()]
MAX_BODY = int(os.environ.get("STAKE_MAX_BODY", "16384"))  # bytes; reject larger payloads
# In prod, don't expose the interactive docs / OpenAPI schema (reduce surface).
_docs = dict(docs_url=None, redoc_url=None, openapi_url=None) if config.is_prod() else {}
app = FastAPI(title="CLEAN soft-staking API", **_docs)


@app.middleware("http")
async def _guard(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > MAX_BODY:
                return JSONResponse({"detail": "payload too large"}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "bad content-length"}, status_code=400)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return resp


if _CORS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_CORS,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
        allow_credentials=False,  # we use bearer-style tokens in the body, not cookies
    )
db.init_db()


# --------------------------------------------------------------------------- #
#  HELPERS                                                                     #
# --------------------------------------------------------------------------- #
def _require(token: str) -> dict:
    sess = auth.verify_session(token)
    if not sess:
        raise HTTPException(401, "invalid or expired session")
    return sess


async def _refresh_balance(conn, row, force: bool = False) -> int:
    """Returns the wallet's $CLEAN balance in integer base units (cached)."""
    now = int(time.time())
    if not force and (now - row["balance_ts"]) < BALANCE_TTL:
        return row["cached_balance"]
    try:
        bal_base = db.to_base(await solana.token_balance(row["wallet"]))
    except Exception:  # noqa: BLE001 — RPC hiccup: keep last known balance
        return row["cached_balance"]
    conn.execute(
        "UPDATE stakers SET cached_balance=?, balance_ts=? WHERE wallet=?",
        (bal_base, now, row["wallet"]),
    )
    conn.commit()
    return bal_base


def _apr_for(conn, wallet: str, row):
    """Effective APR object for a staker. Economics works in human-token space
    (coarse tiers); amounts in `row` are integer base units, so convert."""
    now = int(time.time())
    eff_base = econ.effective_staked(row["recorded_staked"], row["cached_balance"])
    secs = now - row["stake_start_ts"] if (eff_base > 0 and row["stake_start_ts"]) else 0
    refs = db.active_referrals(conn, wallet)
    apr = econ.effective_apr(db.to_ui(eff_base), secs, refs, db.to_ui(row["total_burned"]))
    return eff_base, secs, refs, apr


def _accrue(conn, wallet: str) -> None:
    """Bring a staker's rewards up to `now`. All arithmetic in integer base units
    (floored), so there is no float drift over many small accruals."""
    row = db.get_staker(conn, wallet)
    now = int(time.time())
    eff_base, _secs, _refs, apr = _apr_for(conn, wallet, row)
    dt = now - row["last_accrual_ts"]
    reward_base = int(econ.accrue(eff_base, apr.effective_apr, dt))  # floor
    conn.execute(
        "UPDATE stakers SET accrued = accrued + ?, last_accrual_ts=? WHERE wallet=?",
        (reward_base, now, wallet),
    )
    conn.commit()


def _profile(conn, wallet: str) -> dict:
    row = db.get_staker(conn, wallet)
    eff_base, secs, refs, apr = _apr_for(conn, wallet, row)
    rank = conn.execute(
        "SELECT COUNT(*)+1 AS r FROM stakers WHERE recorded_staked > ?",
        (row["recorded_staked"],),
    ).fetchone()["r"]
    return {
        "wallet": row["wallet"],
        "tg_id": row["tg_id"],
        "username": row["username"],
        # amounts converted from base units to human tokens at the boundary
        "balance": db.to_ui(row["cached_balance"]),
        "staked": db.to_ui(row["recorded_staked"]),
        "staked_effective": db.to_ui(eff_base),
        "pending_rewards": db.to_ui(row["accrued"]),
        "claimed_total": db.to_ui(row["claimed_total"]),
        "total_burned": db.to_ui(row["total_burned"]),
        "active_referrals": refs,
        "days_staked": round(secs / 86400, 2),
        "rank": rank,
        "apr": apr.to_dict(),
    }


# --------------------------------------------------------------------------- #
#  MODELS                                                                      #
# --------------------------------------------------------------------------- #
class LoginBody(BaseModel):
    wallet: str
    signature: str
    nonce: str
    initData: str | None = None
    ref: str | None = None  # referrer wallet (from a referral link)


class Tok(BaseModel):
    token: str


class BurnBody(BaseModel):
    token: str
    signature: str


# --------------------------------------------------------------------------- #
#  AUTH                                                                        #
# --------------------------------------------------------------------------- #
@app.get("/api/nonce")
def api_nonce(wallet: str, request: Request):
    ratelimit.hit(request, "nonce")
    if not auth.is_valid_wallet(wallet):
        raise HTTPException(400, "invalid Solana wallet address")
    nonce = auth.issue_nonce(wallet)
    return {"nonce": nonce, "message": auth.login_message(wallet, nonce)}


@app.post("/api/login")
async def api_login(body: LoginBody, request: Request):
    ratelimit.hit(request, "login", extra_key=body.wallet)
    if not auth.is_valid_wallet(body.wallet):
        raise HTTPException(400, "invalid Solana wallet address")
    if not auth.consume_nonce(body.wallet, body.nonce):
        raise HTTPException(401, "bad or expired nonce — request a new one")
    msg = auth.login_message(body.wallet, body.nonce)
    if not auth.verify_wallet_signature(body.wallet, msg, body.signature):
        raise HTTPException(401, "bad wallet signature")

    tg_id = username = None
    if body.initData:
        tg = auth.verify_init_data(body.initData)
        if not tg:
            raise HTTPException(401, "bad Telegram initData")
        try:
            tg_id = int(tg["id"])
        except (KeyError, ValueError, TypeError):
            raise HTTPException(401, "bad Telegram user")
        username = tg.get("username") or tg.get("first_name")

    with db.db() as conn:
        # One Telegram account links to one wallet — reject a hijack of someone
        # else's TG identity (and avoid the UNIQUE-constraint 500).
        if tg_id is not None:
            other = db.get_staker_by_tg(conn, tg_id)
            if other and other["wallet"] != body.wallet:
                raise HTTPException(409, "this Telegram account is already linked to another wallet")
        existed = db.get_staker(conn, body.wallet) is not None
        ref = None
        if not existed and body.ref and body.ref != body.wallet and auth.is_valid_wallet(body.ref):
            if db.get_staker(conn, body.ref):
                ref = body.ref
        db.upsert_staker(conn, body.wallet, tg_id=tg_id, username=username, referred_by=ref)
        row = db.get_staker(conn, body.wallet)
        await _refresh_balance(conn, row)
        _accrue(conn, body.wallet)
        token = auth.create_session(body.wallet, tg_id)
        return {"token": token, "profile": _profile(conn, body.wallet)}


# --------------------------------------------------------------------------- #
#  STAKING                                                                     #
# --------------------------------------------------------------------------- #
@app.post("/api/stake")
async def api_stake(body: Tok, request: Request):
    wallet = _require(body.token)["w"]
    ratelimit.hit(request, "write", extra_key=wallet)
    now = int(time.time())
    with db.db() as conn:
        row = db.get_staker(conn, wallet)
        if not row:
            raise HTTPException(404, "unknown wallet")
        _accrue(conn, wallet)  # settle rewards on the prior amount first
        bal = await _refresh_balance(conn, db.get_staker(conn, wallet))
        if bal <= 0:
            raise HTTPException(400, "no $CLEAN in wallet to stake")
        start = row["stake_start_ts"] or now  # keep loyalty clock if already staking
        conn.execute(
            "UPDATE stakers SET recorded_staked=?, stake_start_ts=?, last_accrual_ts=? WHERE wallet=?",
            (bal, start, now, wallet),
        )
        conn.commit()
        db.record(conn, wallet, "stake", bal)
        return _profile(conn, wallet)


@app.post("/api/unstake")
async def api_unstake(body: Tok, request: Request):
    wallet = _require(body.token)["w"]
    ratelimit.hit(request, "write", extra_key=wallet)
    with db.db() as conn:
        row = db.get_staker(conn, wallet)
        if not row:
            raise HTTPException(404, "unknown wallet")
        prev = row["recorded_staked"]
        _accrue(conn, wallet)  # keep what you earned
        conn.execute(
            "UPDATE stakers SET recorded_staked=0, stake_start_ts=0 WHERE wallet=?", (wallet,)
        )
        conn.commit()
        db.record(conn, wallet, "unstake", prev)
        return _profile(conn, wallet)


@app.post("/api/claim")
async def api_claim(body: Tok, request: Request):
    wallet = _require(body.token)["w"]
    ratelimit.hit(request, "write", extra_key=wallet)
    with db.db() as conn:
        row = db.get_staker(conn, wallet)
        if not row:
            raise HTTPException(404, "unknown wallet")
        # Settle at the user's REAL current holdings, not a stale cache, so you
        # can't briefly over-accrue by selling right before claiming.
        await _refresh_balance(conn, row, force=True)
        _accrue(conn, wallet)
        row = db.get_staker(conn, wallet)
        amount = row["accrued"]
        if amount <= 0:
            raise HTTPException(400, "nothing to claim")
        # Atomic compare-and-swap: only one request can flip THIS exact accrued
        # amount to 0, so a claim can never be double-counted or double-paid even
        # under concurrent submits.
        cur = conn.execute(
            "UPDATE stakers SET accrued=0, claimed_total = claimed_total + ? "
            "WHERE wallet=? AND accrued=?",
            (amount, wallet, amount),
        )
        if cur.rowcount != 1:
            raise HTTPException(409, "claim already in progress")
        # Manual payout (PAYOUT_MODE=manual): record a 'requested' claim. An
        # operator/cron pays it from the treasury and marks it paid with the tx.
        # No funds move here and NO private key lives on the server.
        db.create_claim(conn, wallet, amount, status="requested")
        db.record(conn, wallet, "claim", amount)
        return {
            "claimed": db.to_ui(amount),
            "status": "requested",
            "profile": _profile(conn, wallet),
        }


@app.post("/api/burn")
async def api_burn(body: BurnBody, request: Request):
    wallet = _require(body.token)["w"]
    ratelimit.hit(request, "burn", extra_key=wallet)
    with db.db() as conn:
        if not db.get_staker(conn, wallet):
            raise HTTPException(404, "unknown wallet")
        if db.burn_seen(conn, body.signature):
            raise HTTPException(409, "burn already credited")
    burned = await solana.verify_burn(body.signature, wallet)
    if burned <= 0:
        raise HTTPException(400, "no $CLEAN burn by this wallet found in that transaction")
    burned_base = db.to_base(burned)
    now = int(time.time())
    with db.db() as conn:
        if not db.get_staker(conn, wallet):
            raise HTTPException(404, "unknown wallet")
        # Atomic idempotency: the `burns` PRIMARY KEY (signature) lets exactly one
        # request insert this signature. We ONLY credit the bonus when our own
        # INSERT actually created the row (rowcount == 1); concurrent duplicates
        # get rowcount 0 and are rejected — no double credit.
        cur = conn.execute(
            "INSERT OR IGNORE INTO burns (signature, wallet, amount, ts) VALUES (?,?,?,?)",
            (body.signature, wallet, burned_base, now),
        )
        if cur.rowcount == 0:
            raise HTTPException(409, "burn already credited")
        _accrue(conn, wallet)
        conn.execute(
            "UPDATE stakers SET total_burned = total_burned + ? WHERE wallet=?",
            (burned_base, wallet),
        )
        conn.commit()
        db.record(conn, wallet, "burn", burned_base, body.signature)
        return {"burned": round(burned, 6), "profile": _profile(conn, wallet)}


# --------------------------------------------------------------------------- #
#  READS                                                                       #
# --------------------------------------------------------------------------- #
@app.post("/api/profile")
async def api_profile(body: Tok):
    wallet = _require(body.token)["w"]
    with db.db() as conn:
        row = db.get_staker(conn, wallet)
        if not row:
            raise HTTPException(404, "unknown wallet")
        await _refresh_balance(conn, row)
        _accrue(conn, wallet)
        return _profile(conn, wallet)


@app.post("/api/leaderboard")
def api_leaderboard(body: Tok):
    wallet = _require(body.token)["w"]
    with db.db() as conn:
        rows = conn.execute(
            "SELECT wallet, username, recorded_staked, total_burned FROM stakers "
            "ORDER BY recorded_staked DESC LIMIT 50"
        ).fetchall()
        board = [
            {
                "rank": i + 1,
                "name": r["username"] or (r["wallet"][:4] + "…" + r["wallet"][-4:]),
                "staked": db.to_ui(r["recorded_staked"]),
                "burned": db.to_ui(r["total_burned"]),
                "me": r["wallet"] == wallet,
            }
            for i, r in enumerate(rows)
        ]
        return {"leaderboard": board}


@app.post("/api/referrals")
def api_referrals(body: Tok):
    wallet = _require(body.token)["w"]
    with db.db() as conn:
        return {
            "ref_code": wallet,
            "active_referrals": db.active_referrals(conn, wallet),
            "reward": "each active referral adds to your APR (see /api/profile apr.referral_boost)",
        }


# --------------------------------------------------------------------------- #
#  ADMIN — manual payout workflow (treasury/cron). Gate with STAKE_ADMIN_TOKEN. #
# --------------------------------------------------------------------------- #
ADMIN_TOKEN = os.environ.get("STAKE_ADMIN_TOKEN", "")


class AdminTok(BaseModel):
    admin_token: str


class AdminMark(BaseModel):
    admin_token: str
    claim_id: int
    tx_sig: str


def _require_admin(tok: str) -> None:
    if not ADMIN_TOKEN or not hmac.compare_digest(tok or "", ADMIN_TOKEN):
        raise HTTPException(403, "admin only")


@app.post("/api/admin/pending")
def api_admin_pending(body: AdminTok):
    _require_admin(body.admin_token)
    with db.db() as conn:
        rows = db.list_pending_claims(conn)
        return {
            "pending": [
                {
                    "claim_id": r["id"],
                    "wallet": r["wallet"],
                    "amount": db.to_ui(r["amount"]),
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        }


@app.post("/api/admin/mark_paid")
def api_admin_mark_paid(body: AdminMark):
    _require_admin(body.admin_token)
    with db.db() as conn:
        n = db.mark_claim_paid(conn, body.claim_id, body.tx_sig)
        if n != 1:
            raise HTTPException(409, "claim not found or already paid")
        return {"ok": True, "claim_id": body.claim_id, "status": "paid"}


@app.get("/api/economics")
def api_economics():
    """Public, non-secret: lets the site/app render the rules consistently."""
    return JSONResponse(
        {
            "base_apr": econ.BASE_APR,
            "amount_tiers": econ.AMOUNT_TIERS,
            "loyalty_per_30d": econ.LOYALTY_PER_30D,
            "loyalty_cap": econ.LOYALTY_CAP,
            "referral_per": econ.REFERRAL_PER,
            "referral_cap": econ.REFERRAL_CAP,
            "burn_unit": econ.BURN_UNIT,
            "burn_apr_per_unit": econ.BURN_APR_PER_UNIT,
            "burn_cap_apr": econ.BURN_CAP_APR,
            "botUsername": os.environ.get("MINIAPP_BOT_USERNAME", "").lstrip("@"),
            "appShortName": os.environ.get("MINIAPP_SHORT_NAME", "app"),
            # Browser-safe RPC for the in-app swap widget. NEVER expose the paid
            # SOLANA_RPC_URL here; operators set a separate public-ish endpoint.
            "swapRpc": os.environ.get("MINIAPP_SWAP_RPC", ""),
            "decimals": db.DECIMALS,
            "mint": market.MINT,
            # In-app burn (signs a real burn tx in the wallet). OFF by default —
            # burn is irreversible; enable only after on-device QA.
            "inAppBurn": os.environ.get("MINIAPP_INAPP_BURN", "").strip() in ("1", "true", "yes"),
        }
    )


@app.get("/api/price")
async def api_price():
    """Live $CLEAN market data (cached) so the app can show price + USD values."""
    s = market.summary(await market.best_pair())
    if not s:
        return JSONResponse({"price_usd": 0, "available": False})
    s["available"] = True
    s["mint"] = market.MINT
    return JSONResponse(s, headers={"Cache-Control": "no-store"})


@app.get("/api/stats")
def api_stats():
    """Public, aggregate-only protocol stats for the app's 'Supply washed' panel."""
    with db.db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(total_burned),0) AS burned,"
            " SUM(CASE WHEN total_burned > 0 THEN 1 ELSE 0 END) AS burners,"
            " SUM(CASE WHEN recorded_staked > 0 THEN 1 ELSE 0 END) AS stakers,"
            " COALESCE(SUM(recorded_staked),0) AS staked"
            " FROM stakers"
        ).fetchone()
    out = {
        "total_burned": db.to_ui(row["burned"] or 0),
        "burners": int(row["burners"] or 0),
        "stakers": int(row["stakers"] or 0),
        "total_staked": db.to_ui(row["staked"] or 0),
    }
    supply = float(os.environ.get("STAKE_TOTAL_SUPPLY", "0") or 0)
    if supply > 0:
        out["burned_pct"] = round(out["total_burned"] / supply * 100.0, 2)
    return out


# --------------------------------------------------------------------------- #
#  WALLET CALLBACK RELAY                                                        #
#  Inside Telegram the wallet's deeplink callback cannot reach the Mini App's   #
#  webview — it lands on /wallet-return in the external browser. That page      #
#  posts the still-ENCRYPTED payload here under a one-time id; the webview      #
#  polls the id and decrypts locally (the x25519 key never leaves the webview). #
# --------------------------------------------------------------------------- #
_RID_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{8,40}$")  # base58, unguessable
_RELAY_TTL = 180  # seconds; single read via getdel


class RelayBody(BaseModel):
    params: dict


@app.post("/api/relay/{rid}")
def api_relay_put(rid: str, body: RelayBody, request: Request):
    ratelimit.hit(request, "relay")
    if not _RID_RE.match(rid):
        raise HTTPException(400, "bad relay id")
    if not isinstance(body.params, dict) or len(body.params) > 8:
        raise HTTPException(400, "bad relay payload")
    params = {str(k)[:64]: str(v)[:4096] for k, v in body.params.items()}
    store.get_store().setex("relay:" + rid, _RELAY_TTL, json.dumps(params))
    return {"ok": True}


@app.get("/api/relay/{rid}")
def api_relay_get(rid: str, request: Request):
    # Peek WITHOUT consuming: Telegram may relaunch the Mini App webview while
    # it is mid-poll, and the relaunched instance must still find the payload.
    # The webview acks with DELETE after processing; the TTL is the backstop.
    ratelimit.hit(request, "relay")
    if not _RID_RE.match(rid):
        raise HTTPException(400, "bad relay id")
    v = store.get_store().get("relay:" + rid)
    if v is None:
        raise HTTPException(404, "not ready")
    return {"params": json.loads(v)}


@app.delete("/api/relay/{rid}")
def api_relay_ack(rid: str, request: Request):
    ratelimit.hit(request, "relay")
    if not _RID_RE.match(rid):
        raise HTTPException(400, "bad relay id")
    store.get_store().delete("relay:" + rid)
    return {"ok": True}


@app.get("/healthz")
def healthz():
    try:
        with db.db() as conn:
            conn.execute("SELECT 1").fetchone()
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    status = {"ok": db_ok, "db": db_ok, "store": store.backend_name(), **CONFIG_STATUS}
    return JSONResponse(status, status_code=200 if db_ok else 503)


# --------------------------------------------------------------------------- #
#  STATIC FRONTEND (the Mini App, served same-origin → no CORS for the app)    #
# --------------------------------------------------------------------------- #
_WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")


@app.get("/")
def index():
    return FileResponse(os.path.join(_WEB, "index.html"))


@app.get("/app.js")
def app_js():
    return FileResponse(os.path.join(_WEB, "app.js"), media_type="application/javascript")


@app.get("/wallet.js")
def wallet_js():
    return FileResponse(os.path.join(_WEB, "wallet.js"), media_type="application/javascript")


@app.get("/nacl.min.js")
def nacl_js():
    return FileResponse(os.path.join(_WEB, "nacl.min.js"), media_type="application/javascript")


@app.get("/wallet-return")
def wallet_return():
    return FileResponse(os.path.join(_WEB, "return.html"))


@app.get("/glove.png")
def glove_png():
    return FileResponse(os.path.join(os.path.dirname(_WEB), "..", "assets", "glove.png"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("STAKE_PORT", "8090")))
