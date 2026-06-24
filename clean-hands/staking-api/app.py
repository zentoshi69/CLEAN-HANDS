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
import secrets
import asyncio
from urllib.parse import urlencode

import base58
from nacl.public import PrivateKey, PublicKey, Box
from nacl.utils import random as nacl_random

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse, Response
from pydantic import BaseModel

import db
import auth
import bridge
import config
import easybit
import economics as econ
import market
import ratelimit
import solana
import store

CONFIG_STATUS = config.validate_config()  # fail-fast on misconfig (prod)
BALANCE_TTL = int(os.environ.get("STAKE_BALANCE_TTL", "300"))  # re-check chain every 5 min
# Claims vest: rewards are claimable only after this many days of CONTINUOUS
# staking (the stake_start_ts clock, which survives re-stakes but resets on
# unstake). Unstaking forfeits pending rewards. 0 disables the lock.
CLAIM_LOCK_DAYS = int(os.environ.get("STAKE_CLAIM_LOCK_DAYS", "90"))


# Payout setup window + claim fee are read per-request so operators can tune
# them with just a restart (and tests can toggle them).
def _payout_setup_days() -> int:
    return int(os.environ.get("STAKE_PAYOUT_SETUP_DAYS", "3") or 0)


def _claim_fee_usd() -> float:
    return float(os.environ.get("STAKE_CLAIM_FEE_USD", "5") or 0)
# Explicit allow-list so your website's browser can call this API cross-origin.
# Comma-separated origins, e.g. "https://clean.fun,https://app.clean.fun".
# The Telegram Mini App webview sends requests from the app's own HTTPS origin.
_CORS = [o.strip() for o in os.environ.get("STAKE_CORS_ORIGINS", "").split(",") if o.strip()]
MAX_BODY = int(os.environ.get("STAKE_MAX_BODY", "16384"))  # bytes; reject larger payloads
# In prod, don't expose the interactive docs / OpenAPI schema (reduce surface).
_docs = dict(docs_url=None, redoc_url=None, openapi_url=None) if config.is_prod() else {}
app = FastAPI(title="CLEAN soft-staking API", **_docs)


# --------------------------------------------------------------------------- #
#  SECURITY HEADERS                                                            #
# --------------------------------------------------------------------------- #
# CSP: the webapp has no inline <script> BLOCKS, but index.html wires its
# buttons through inline onclick="App.*" ATTRIBUTES (27 of them) — and
# attribute handlers are governed by script-src too, so 'unsafe-inline' is
# REQUIRED here until those are converted to addEventListener wiring.
# (Shipping without it turned the CSP on and silently killed every button
# in the Mini App — the 2026-06-12 incident.) We deliberately use
# 'unsafe-inline' in script-src rather than script-src-attr because older
# iOS webviews ignore script-src-attr and fall back to script-src.
# script-src still pins remote code to self + the wallet/swap CDNs.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://telegram.org https://unpkg.com "
    "https://esm.sh https://esm.run https://cdn.jsdelivr.net https://plugin.jup.ag; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data: https:; "
    # /play (the tap game) plays its SFX + music from inlined data: audio URIs;
    # without media-src these fall back to default-src 'self' and are blocked.
    "media-src 'self' data:; "
    "connect-src 'self' https: wss:; "
    "frame-src https:; "
    "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org; "
    "base-uri 'self'; form-action 'self'"
)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", _CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return resp


@app.middleware("http")
async def _guard(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > MAX_BODY:
                return JSONResponse({"detail": "payload too large"}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "bad content-length"}, status_code=400)
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            return JSONResponse({"detail": "payload too large"}, status_code=413)
    # Starlette/FastAPI read the cached body downstream for JSON parsing.
    request._body = body
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


# Per-worker, in-memory cache of native SOL balances for the wallet-balance
# booster (best-effort; the booster fails safe to 0 when cold or on RPC error,
# so this never blocks or corrupts the $CLEAN accrual path).
_sol_cache: dict[str, tuple[float, float]] = {}  # wallet -> (ts, sol_ui_balance)


async def _refresh_wallet_usd(wallet: str, force: bool = False) -> None:
    """Refresh the wallet's SOL balance + CLEAN/SOL USD prices used by the
    wallet-balance booster. Never raises; on any hiccup we keep last-known."""
    if not econ.BAL_BOOST_ENABLED:
        return
    try:
        await asyncio.wait_for(market.refresh_prices(), timeout=5.0)
    except Exception:  # noqa: BLE001
        pass  # nosec B110
    now = time.time()
    hit = _sol_cache.get(wallet)
    if hit and not force and (now - hit[0]) < BALANCE_TTL:
        return
    try:
        _sol_cache[wallet] = (now, await asyncio.wait_for(solana.sol_balance(wallet), timeout=5.0))
    except Exception:  # noqa: BLE001
        pass  # nosec B110


def _wallet_usd(wallet: str, row) -> tuple[float, float]:
    """(sol_usd, clean_usd) from cached balances + last-known prices. Pure/sync
    and safe to call from the accrual path; returns (0, 0) when disabled/cold."""
    if not econ.BAL_BOOST_ENABLED:
        return 0.0, 0.0
    pr = market.last_prices()
    sol_ui = _sol_cache.get(wallet, (0.0, 0.0))[1]
    sol_usd = sol_ui * float(pr.get("sol_usd") or 0.0)
    clean_usd = db.to_ui(row["cached_balance"]) * float(pr.get("clean_usd") or 0.0)
    return sol_usd, clean_usd


def _escape_score_for(conn, row) -> float:
    """Telegram-bound verified game progress -> staking Escape score.

    Raw cloud saves are untrusted and can be faked with scripts. Money reads
    only the server-side verification ledger.
    """
    if db.flag_enabled(conn, "halt_escape_boost"):
        return 0.0
    tg_id = row["tg_id"] if row and row["tg_id"] is not None else None
    if tg_id is None:
        return 0.0
    verified = db.game_verify_load(conn, f"tg:{int(tg_id)}")
    if not verified or verified["status"] in ("blocked", "review"):
        return 0.0
    return float(verified["verified_escape_score"] or 0.0)


def _escape_public_status(conn, row) -> dict:
    tg_id = row["tg_id"] if row and row["tg_id"] is not None else None
    if tg_id is None:
        return {
            "escape_status": "telegram_required",
            "escape_raw_score": 0.0,
            "escape_verified_score": 0.0,
        }
    if db.flag_enabled(conn, "halt_escape_boost"):
        return {
            "escape_status": "paused",
            "escape_raw_score": 0.0,
            "escape_verified_score": 0.0,
        }
    verified = db.game_verify_load(conn, f"tg:{int(tg_id)}")
    if not verified:
        return {
            "escape_status": "play_to_unlock",
            "escape_raw_score": 0.0,
            "escape_verified_score": 0.0,
        }
    raw = float(verified["raw_escape_score"] or 0.0)
    score = float(verified["verified_escape_score"] or 0.0)
    status = verified["status"] or "unverified"
    public = "verified" if score >= 5 else "play_to_unlock"
    if status in ("review", "blocked"):
        public = "review"
    elif econ.escape_boost(raw) > econ.escape_boost(score) and raw >= 5:
        public = "verifying"
    return {
        "escape_status": public,
        "escape_raw_score": raw,
        "escape_verified_score": score,
    }


def _social_public(conn, wallet: str) -> dict:
    """Public social activation status for the Escape reward gate.

    This is intentionally low-detail: the client can see missing/pending/
    verified, but not internal review logic.
    """
    return db.social_summary(conn, wallet)


async def _refresh_balance(conn, row, force: bool = False, fail_closed: bool = False) -> int:
    """Returns the wallet's $CLEAN balance in integer base units (cached)."""
    now = int(time.time())
    # opportunistically refresh SOL + prices for the wallet-balance booster
    await _refresh_wallet_usd(row["wallet"], force=force)
    if not force and (now - row["balance_ts"]) < BALANCE_TTL:
        return row["cached_balance"]
    try:
        bal_base = db.to_base(await asyncio.wait_for(solana.token_balance(row["wallet"]), timeout=8.0))
    except Exception:  # noqa: BLE001 — cache is OK for reads, not money-finalizing actions
        if fail_closed:
            raise HTTPException(503, "live balance check unavailable — try again shortly")
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
    sol_usd, clean_usd = _wallet_usd(wallet, row)
    escape_score = _escape_score_for(conn, row)
    social_gate = _social_public(conn, wallet)
    apr = econ.effective_apr(
        db.to_ui(eff_base), secs, refs, db.to_ui(row["total_burned"]),
        (row["mm_liquidity_cents"] or 0) / 100.0, sol_usd, clean_usd,
        vip=bool(row["mm_vip"]),
        escape_score=escape_score,
        escape_boost_scale=float(social_gate["multiplier"]),
    )
    return eff_base, secs, refs, apr


def _accrue(conn, wallet: str) -> None:
    """Bring a staker's rewards up to `now`. All arithmetic in integer base units
    (floored), so there is no float drift over many small accruals."""
    # Compare-and-swap on last_accrual_ts: every money path opens its own
    # connection, so two concurrent accruals could otherwise credit the same
    # [old_ts, now] window twice. Retry a few times instead of silently dropping
    # the loser; if the row keeps changing, ask the client to retry.
    for _ in range(5):
        row = db.get_staker(conn, wallet)
        if not row:
            return
        now = int(time.time())
        old_ts = int(row["last_accrual_ts"] or 0)
        dt = now - old_ts
        if dt <= 0:
            return
        eff_base, _secs, _refs, apr = _apr_for(conn, wallet, row)
        reward_base = int(econ.accrue(eff_base, apr.effective_apr, dt))  # floor
        cur = conn.execute(
            "UPDATE stakers SET accrued = accrued + ?, last_accrual_ts=? "
            "WHERE wallet=? AND last_accrual_ts=?",
            (reward_base, now, wallet, old_ts),
        )
        if cur.rowcount == 1:
            conn.commit()
            return
        conn.rollback()
    raise HTTPException(409, "staking balance changed — retry")


def _profile(conn, wallet: str) -> dict:
    row = db.get_staker(conn, wallet)
    eff_base, secs, refs, apr = _apr_for(conn, wallet, row)
    socials = _social_public(conn, wallet)
    apr_dict = apr.to_dict()
    apr_dict.update(_escape_public_status(conn, row))
    apr_dict["social_verified_count"] = socials["verified_count"]
    apr_dict["social_required_count"] = socials["required"]
    vest_secs = int(time.time()) - row["stake_start_ts"] if row["stake_start_ts"] else 0
    eff_expr = db.effective_staked_expr()
    # eff_expr is an internal SQL constant, not user input.
    rank = conn.execute(
        f"SELECT COUNT(*)+1 AS r FROM stakers WHERE ({eff_expr}) > ?",  # nosec B608
        (eff_base,),
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
        "mm_liquidity_usd": round((row["mm_liquidity_cents"] or 0) / 100.0, 2),
        "vip": bool(row["mm_vip"]),
        "active_referrals": refs,
        "days_staked": round(secs / 86400, 2),
        "rank": rank,
        "ref_code": db.ref_code(conn, wallet),
        "claim_lock_days": CLAIM_LOCK_DAYS,
        # vesting clock: raw stake_start_ts, independent of effective stake —
        # must match what the /api/claim and /api/payout gates enforce
        "claim_locked": bool(
            CLAIM_LOCK_DAYS > 0
            and (not row["stake_start_ts"] or vest_secs < CLAIM_LOCK_DAYS * 86400)
        ),
        "claim_unlock_in_days": (
            0
            if CLAIM_LOCK_DAYS <= 0
            else (
                -(-(CLAIM_LOCK_DAYS * 86400 - vest_secs) // 86400)
                if row["stake_start_ts"] and vest_secs < CLAIM_LOCK_DAYS * 86400
                else (0 if row["stake_start_ts"] else CLAIM_LOCK_DAYS)
            )
        ),
        "payout_wallet": row["payout_wallet"],
        "payout_confirmed": bool(row["payout_confirmed_ts"]),
        # the setup window opens N days before unlock (or once unlocked)
        "payout_setup_open": bool(
            _payout_setup_days() > 0
            and row["stake_start_ts"]
            and vest_secs >= max(0, (CLAIM_LOCK_DAYS - _payout_setup_days())) * 86400
        ),
        "claim_fee_usd": _claim_fee_usd(),
        "balance_verified_at": row["balance_ts"],
        "socials": socials,
        "apr": apr_dict,
    }


# --------------------------------------------------------------------------- #
#  MODELS                                                                      #
# --------------------------------------------------------------------------- #
class LoginBody(BaseModel):
    wallet: str
    signature: str
    nonce: str
    initData: str | None = None
    ref: str | None = None  # referrer wallet or short code (from a referral link)


class Tok(BaseModel):
    token: str


class StakeBody(Tok):
    # stake only part of the bag: 1..100 (default 100 = everything)
    percent: int | None = None


class BurnBody(BaseModel):
    token: str
    signature: str


class MmBody(BaseModel):
    token: str
    signature: str


class PayoutBody(BaseModel):
    token: str
    address: str | None = None  # default: the staking wallet itself
    signature: str | None = None
    nonce: str | None = None


class PayoutNonceBody(BaseModel):
    token: str
    address: str | None = None


class TgStart(BaseModel):
    initData: str
    wallet: str  # wallet id: phantom | solflare | backpack


class TgPoll(BaseModel):
    initData: str
    sid: str | None = None


class SocialClaimBody(Tok):
    platform: str
    handle: str | None = None
    proof: str | None = None


# --------------------------------------------------------------------------- #
#  AUTH                                                                        #
# --------------------------------------------------------------------------- #
@app.get("/api/nonce")
def api_nonce(wallet: str, request: Request):
    ratelimit.hit(request, "nonce")
    if not auth.is_valid_wallet(wallet):
        print(f"[login] nonce REJECT bad_wallet ip={ratelimit.client_ip(request)}", flush=True)
        raise HTTPException(400, "invalid Solana wallet address")
    nonce = auth.issue_nonce(wallet)
    print(f"[login] nonce wallet={wallet[:6]}… ip={ratelimit.client_ip(request)}", flush=True)
    return {"nonce": nonce, "message": auth.login_message(wallet, nonce)}


@app.post("/api/login")
async def api_login(body: LoginBody, request: Request):
    ratelimit.hit(request, "login", extra_key=body.wallet)
    ip = ratelimit.client_ip(request)
    w6 = (body.wallet or "")[:6]
    # Instrument every branch — a mobile login must never fail invisibly again.
    print(f"[login] attempt wallet={w6}… initData={'yes' if body.initData else 'no'} ip={ip}", flush=True)
    if not auth.is_valid_wallet(body.wallet):
        print(f"[login] REJECT bad_wallet ip={ip}", flush=True)
        raise HTTPException(400, "invalid Solana wallet address")
    if not auth.consume_nonce(body.wallet, body.nonce):
        print(f"[login] REJECT bad_nonce wallet={w6}…", flush=True)
        raise HTTPException(401, "bad or expired nonce — request a new one")
    msg = auth.login_message(body.wallet, body.nonce)
    if not auth.verify_wallet_signature(body.wallet, msg, body.signature):
        print(f"[login] REJECT bad_signature wallet={w6}…", flush=True)
        raise HTTPException(401, "bad wallet signature")

    tg_id = username = None
    if body.initData:
        tg = auth.verify_init_data(body.initData)
        if not tg:
            print(f"[login] REJECT bad_initData wallet={w6}… (expired/TTL or TG_COMMUNITY_TOKEN unset)", flush=True)
            raise HTTPException(401, "bad Telegram initData")
        try:
            tg_id = int(tg["id"])
        except (KeyError, ValueError, TypeError):
            print(f"[login] REJECT bad_tg_user wallet={w6}…", flush=True)
            raise HTTPException(401, "bad Telegram user")
        username = tg.get("username") or tg.get("first_name")

    token, profile = await _complete_login(body.wallet, tg_id, body.ref, username)
    print(f"[login] OK wallet={w6}… tg={tg_id if tg_id is not None else '-'}", flush=True)
    return {"token": token, "profile": profile}


async def _complete_login(wallet: str, tg_id, ref, username):
    """Shared tail of every login: bind the (optional) Telegram identity, upsert
    the staker, refresh balance, settle accrual, mint a session. Used by both the
    body-based /api/login (site + extension) and the server-side Telegram flow."""
    with db.db() as conn:
        # One Telegram account links to one wallet — reject a hijack of someone
        # else's TG identity (and avoid the UNIQUE-constraint 500).
        if tg_id is not None:
            other = db.get_staker_by_tg(conn, tg_id)
            if other and other["wallet"] != wallet:
                raise HTTPException(409, "this Telegram account is already linked to another wallet")
        existed = db.get_staker(conn, wallet) is not None
        r = None
        if not existed and ref:
            # the referral may be a full wallet (legacy links) or a short code
            cand = ref if auth.is_valid_wallet(ref) else None
            if cand is None:
                code = re.sub(r"[^A-Za-z0-9]", "", str(ref)).upper()
                if 4 <= len(code) <= 12:
                    cand = db.wallet_by_ref_code(conn, code)
            if cand and cand != wallet and db.get_staker(conn, cand):
                r = cand
        db.upsert_staker(conn, wallet, tg_id=tg_id, username=username, referred_by=r)
        row = db.get_staker(conn, wallet)
        await _refresh_balance(conn, row)
        # Settle rewards under the OLD social gate before Telegram verification
        # changes future Escape activation. Socials must never retro-boost a
        # previous accrual window.
        _accrue(conn, wallet)
        if tg_id is not None:
            db.social_set(
                conn,
                wallet=wallet,
                platform="tg",
                verified=True,
                status="verified",
                handle=username,
                method="telegram_initData",
            )
        token = auth.create_session(wallet, tg_id)
        return token, _profile(conn, wallet)


# --------------------------------------------------------------------------- #
#  STAKING                                                                     #
# --------------------------------------------------------------------------- #
@app.post("/api/stake")
async def api_stake(body: StakeBody, request: Request):
    wallet = _require(body.token)["w"]
    ratelimit.hit(request, "write", extra_key=wallet)
    now = int(time.time())
    with db.db() as conn:
        _assert_ops_open(conn, "halt_staking")
        row = db.get_staker(conn, wallet)
        if not row:
            raise HTTPException(404, "unknown wallet")
        _accrue(conn, wallet)  # settle rewards on the prior amount first
        # FORCE a live on-chain read: a trader who just bought more $CLEAN must be
        # able to stake the new tokens immediately. Honouring the 5-min balance
        # cache here made staking feel like a one-time snapshot — a re-stake right
        # after buying saw the stale (pre-purchase) balance and added nothing.
        bal = await _refresh_balance(conn, db.get_staker(conn, wallet), force=True, fail_closed=True)
        if bal <= 0:
            raise HTTPException(400, "no $CLEAN in wallet to stake")
        pct = 100 if body.percent is None else int(body.percent)
        if not 1 <= pct <= 100:
            raise HTTPException(400, "percent must be between 1 and 100")
        stake_amt = bal * pct // 100  # integer base units, floor — never over-stake
        if stake_amt <= 0:
            raise HTTPException(400, "stake amount rounds to zero — raise the percent")
        start = row["stake_start_ts"] or now  # keep loyalty clock if already staking
        conn.execute(
            "UPDATE stakers SET recorded_staked=?, stake_start_ts=?, last_accrual_ts=? WHERE wallet=?",
            (stake_amt, start, now, wallet),
        )
        conn.commit()
        db.record(conn, wallet, "stake", stake_amt)
        return _profile(conn, wallet)


@app.post("/api/unstake")
async def api_unstake(body: Tok, request: Request):
    wallet = _require(body.token)["w"]
    ratelimit.hit(request, "write", extra_key=wallet)
    with db.db() as conn:
        _assert_ops_open(conn, "halt_staking")
        row = db.get_staker(conn, wallet)
        if not row:
            raise HTTPException(404, "unknown wallet")
        prev = row["recorded_staked"]
        _accrue(conn, wallet)  # settle the clock, then forfeit (policy below)
        row = db.get_staker(conn, wallet)
        forfeited = row["accrued"]
        # Unstaking resets EVERYTHING: stake, the vesting clock, and pending
        # rewards (tokenomics: pending vests only while you stay staked). The
        # forfeit is ledgered so reconciliation can always explain the delta.
        conn.execute(
            "UPDATE stakers SET recorded_staked=0, stake_start_ts=0, accrued=0 WHERE wallet=?",
            (wallet,),
        )
        conn.commit()
        db.record(conn, wallet, "unstake", prev)
        if forfeited > 0:
            db.record(conn, wallet, "forfeit", forfeited)
        return _profile(conn, wallet)


def _payout_addr(wallet: str, address: str | None) -> str:
    addr = (address or wallet).strip()
    if not auth.is_valid_wallet(addr):
        raise HTTPException(400, "invalid payout wallet address")
    return addr


def _assert_payout_window(row) -> None:
    if _payout_setup_days() > 0 and CLAIM_LOCK_DAYS > 0:
        start = row["stake_start_ts"] or 0
        secs = int(time.time()) - start if start else 0
        open_from = max(0, (CLAIM_LOCK_DAYS - _payout_setup_days())) * 86400
        if not start or secs < open_from:
            left = -(-(open_from - secs) // 86400) if start else CLAIM_LOCK_DAYS
            raise HTTPException(
                400,
                f"payout setup opens {_payout_setup_days()} days before your claim "
                f"unlocks — {left}d to go",
            )


@app.post("/api/payout/nonce")
def api_payout_nonce(body: PayoutNonceBody, request: Request):
    """Issue a one-time payout-change approval message for the staking wallet."""
    wallet = _require(body.token)["w"]
    ratelimit.hit(request, "write", extra_key=wallet)
    with db.db() as conn:
        _assert_ops_open(conn, "halt_payout_setup")
        row = db.get_staker(conn, wallet)
        if not row:
            raise HTTPException(404, "unknown wallet")
        _assert_payout_window(row)
    addr = _payout_addr(wallet, body.address)
    nonce = auth.issue_action_nonce(wallet, "payout")
    return {"nonce": nonce, "message": auth.payout_message(wallet, addr, nonce), "address": addr}


@app.post("/api/payout")
async def api_payout(body: PayoutBody, request: Request):
    """Confirm where claim payouts go.

    A valid session can start the flow, but the staking wallet must freshly sign
    the exact destination so a stolen bearer token cannot redirect rewards.
    """
    wallet = _require(body.token)["w"]
    ratelimit.hit(request, "write", extra_key=wallet)
    if not (body.nonce and body.signature):
        raise HTTPException(401, "a fresh wallet signature is required to set the payout address")
    addr = _payout_addr(wallet, body.address)
    msg = auth.payout_message(wallet, addr, body.nonce)
    if not auth.consume_action_nonce(wallet, "payout", body.nonce):
        raise HTTPException(401, "bad or expired payout nonce — try again")
    if not auth.verify_wallet_signature(wallet, msg, body.signature):
        raise HTTPException(401, "bad payout wallet signature")
    with db.db() as conn:
        _assert_ops_open(conn, "halt_payout_setup")
        row = db.get_staker(conn, wallet)
        if not row:
            raise HTTPException(404, "unknown wallet")
        _assert_payout_window(row)
        await _refresh_balance(conn, row, force=True, fail_closed=True)
        conn.execute(
            "UPDATE stakers SET payout_wallet=?, payout_confirmed_ts=? WHERE wallet=?",
            (addr, int(time.time()), wallet),
        )
        conn.commit()
        db.record(conn, wallet, "payout_set", 0, detail=addr)
        return _profile(conn, wallet)


@app.post("/api/claim")
async def api_claim(body: Tok, request: Request):
    wallet = _require(body.token)["w"]
    ratelimit.hit(request, "write", extra_key=wallet)
    with db.db() as conn:
        _assert_ops_open(conn, "halt_claims")
        row = db.get_staker(conn, wallet)
        if not row:
            raise HTTPException(404, "unknown wallet")
        # Settle at the user's REAL current holdings, not a stale cache, so you
        # can't briefly over-accrue by selling right before claiming.
        await _refresh_balance(conn, row, force=True, fail_closed=True)
        _accrue(conn, wallet)
        row = db.get_staker(conn, wallet)
        # 90-day vesting gate: the stake_start_ts clock must have run the full
        # lock before anything is claimable. Checked BEFORE the amount so the
        # user sees the real reason, not "nothing to claim".
        if CLAIM_LOCK_DAYS > 0:
            start = row["stake_start_ts"] or 0
            staked_secs = int(time.time()) - start if start else 0
            lock_secs = CLAIM_LOCK_DAYS * 86400
            if not start or staked_secs < lock_secs:
                left = (
                    -(-(lock_secs - staked_secs) // 86400) if start else CLAIM_LOCK_DAYS
                )
                raise HTTPException(
                    400,
                    f"rewards unlock after {CLAIM_LOCK_DAYS} days of staking — {left}d to go",
                )
        # payout destination must be confirmed (window opens pre-unlock)
        if _payout_setup_days() > 0 and not row["payout_confirmed_ts"]:
            raise HTTPException(
                400,
                "confirm your payout wallet first — setup opens "
                f"{_payout_setup_days()} days before your claim unlocks",
            )
        destination = row["payout_wallet"] or wallet
        amount = row["accrued"]
        if amount <= 0:
            raise HTTPException(400, "nothing to claim")
        # $ claim fee, charged in $CLEAN at the live price and DEDUCTED from the
        # payout (non-custodial: no extra payment transaction needed).
        fee_base = 0
        fee_usd = _claim_fee_usd()
        if fee_usd > 0:
            # Prefer the hardened (liquidity-floored) CLEAN price so the fee can't
            # be shrunk by pumping a thin pool; fall back to spot only when the
            # hardened price isn't available, so a genuinely thin pool never
            # bricks claims.
            await market.refresh_prices()
            price = float(market.last_prices().get("clean_usd") or 0)
            if price <= 0:
                p = market.summary(await market.best_pair())
                price = float(p["price_usd"]) if p and p.get("price_usd") else 0.0
            if price <= 0:
                raise HTTPException(503, "claim fee pricing unavailable — try again shortly")
            fee_base = int(-(-(fee_usd / price) * db.BASE // 1))  # ceil in base units
            if amount <= fee_base:
                raise HTTPException(
                    400,
                    f"pending rewards must exceed the ${fee_usd:g} claim fee "
                    f"({db.to_ui(fee_base)} $CLEAN right now)",
                )
        net = amount - fee_base
        # Atomic compare-and-swap: only one request can flip THIS exact accrued
        # amount to 0, so a claim can never be double-counted or double-paid even
        # under concurrent submits. claimed_total counts the NET payout.
        cur = conn.execute(
            "UPDATE stakers SET accrued=0, claimed_total = claimed_total + ? "
            "WHERE wallet=? AND accrued=?",
            (net, wallet, amount),
        )
        if cur.rowcount != 1:
            raise HTTPException(409, "claim already in progress")
        # Manual payout (PAYOUT_MODE=manual): record a 'requested' claim. An
        # operator/cron pays it from the treasury and marks it paid with the tx.
        # No funds move here and NO private key lives on the server.
        db.create_claim(
            conn,
            wallet,
            net,
            gross_amount_base=amount,
            fee_amount_base=fee_base,
            fee_usd=fee_usd,
            destination=destination,
            rules_version=f"claim_lock_{CLAIM_LOCK_DAYS}d_fee_usd_{fee_usd:g}",
            status="requested",
        )
        db.record(conn, wallet, "claim", net)
        if fee_base > 0:
            db.record(conn, wallet, "fee", fee_base, detail=f"claim fee ${fee_usd:g}")
        return {
            "claimed": db.to_ui(net),
            "requested": db.to_ui(net),
            "fee": db.to_ui(fee_base),
            "fee_usd": fee_usd,
            "destination": destination,
            "status": "requested",
            "profile": _profile(conn, wallet),
        }


@app.post("/api/burn")
async def api_burn(body: BurnBody, request: Request):
    wallet = _require(body.token)["w"]
    ratelimit.hit(request, "burn", extra_key=wallet)
    with db.db() as conn:
        _assert_ops_open(conn, "halt_burns")
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
        _assert_ops_open(conn, "halt_burns")
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


# MM deposits are valued at the live price, so they must be recent (a stale
# transfer could be credited against a price that no longer reflects it).
MM_DEPOSIT_MAX_AGE_S = int(os.environ.get("MM_DEPOSIT_MAX_AGE_S", "1800"))


@app.post("/api/mm/add")
async def api_mm_add(body: MmBody, request: Request):
    """Credit the market-maker liquidity booster after a wallet deposits SOL
    (+ optional $CLEAN) to the configured reserve. Mirrors /api/burn: verify the
    on-chain transfer, value it in USD, enforce the deposit rules, credit once."""
    mm_wallet = os.environ.get("CLEAN_MM_WALLET", "").strip()
    if not mm_wallet:
        raise HTTPException(503, "market-maker liquidity is not configured")
    wallet = _require(body.token)["w"]
    ratelimit.hit(request, "mm", extra_key=wallet)
    with db.db() as conn:
        _assert_ops_open(conn, "halt_mm")
        if not db.get_staker(conn, wallet):
            raise HTTPException(404, "unknown wallet")
        if db.mm_seen(conn, body.signature):
            raise HTTPException(409, "deposit already credited")
    sol, clean = await solana.verify_mm_deposit(
        body.signature, wallet, mm_wallet, max_age_s=MM_DEPOSIT_MAX_AGE_S
    )
    if sol <= 0 and clean <= 0:
        raise HTTPException(
            400,
            "no recent SOL/$CLEAN transfer to the MM reserve found — submit the deposit "
            "within ~30 min of the on-chain transfer",
        )
    # Value each leg with the HARDENED prices (liquidity floor + SOL/USD clamp +
    # independent SOL pool), NEVER raw spot — otherwise an attacker who moves
    # their own thin CLEAN/SOL pool could mint max LP boost + permanent VIP for
    # negligible real value. SOL must be priceable (it is the real-money gate);
    # the CLEAN leg is valued at 0 when its pool is too thin to trust.
    await market.refresh_prices()
    px = market.last_prices()
    sol_px = float(px.get("sol_usd") or 0)
    clean_px = float(px.get("clean_usd") or 0)
    if sol_px <= 0:
        raise HTTPException(503, "can't price SOL safely right now — try again shortly")
    sol_usd = sol * sol_px
    clean_usd = clean * clean_px
    # rules: SOL mandatory (>= MIN); $CLEAN optional but if present in [MIN, MAX);
    # never $CLEAN-only; total credited capped at MAX.
    if sol_usd < econ.MM_MIN_USD:
        raise HTTPException(400, f"the SOL leg must be at least ${econ.MM_MIN_USD:.0f} (got ${sol_usd:.2f})")
    if clean_usd > 0 and clean_usd < econ.MM_MIN_USD:
        raise HTTPException(400, f"if you add $CLEAN it must be at least ${econ.MM_MIN_USD:.0f} (got ${clean_usd:.2f})")
    if clean_usd >= econ.MM_MAX_USD:
        raise HTTPException(400, f"the $CLEAN leg must be under ${econ.MM_MAX_USD:.0f} (got ${clean_usd:.2f})")
    total_usd = min(sol_usd + clean_usd, econ.MM_MAX_USD)
    cents = int(round(total_usd * 100))
    now = int(time.time())
    with db.db() as conn:
        if not db.get_staker(conn, wallet):
            raise HTTPException(404, "unknown wallet")
        cur = conn.execute(
            "INSERT OR IGNORE INTO mm_deposits (signature, wallet, usd_cents, lamports, clean_base, ts) "
            "VALUES (?,?,?,?,?,?)",
            (body.signature, wallet, cents, int(round(sol * 1e9)), db.to_base(clean), now),
        )
        if cur.rowcount == 0:
            raise HTTPException(409, "deposit already credited")
        _accrue(conn, wallet)
        # cumulative, but capped at MAX so the boost can never exceed MM_LP_CAP
        # A qualifying deposit permanently locks VIP (the 3x booster) and adds the
        # wallet to the VIP airdrop list (mm_deposits log + mm_vip flag).
        conn.execute(
            "UPDATE stakers SET mm_liquidity_cents = MIN(?, mm_liquidity_cents + ?), mm_vip = 1 WHERE wallet=?",
            (int(round(econ.MM_MAX_USD * 100)), cents, wallet),
        )
        conn.commit()
        db.record(conn, wallet, "mm_liquidity", cents, body.signature)
        return {
            "added_usd": round(total_usd, 2),
            "sol": round(sol, 6),
            "clean": round(clean, 6),
            "vip": True,
            "profile": _profile(conn, wallet),
        }


@app.get("/api/mm/quote")
async def api_mm_quote():
    """Live SOL/$CLEAN prices + deposit limits so the UI can validate a deposit
    before the user signs (the /api/mm/add path re-checks authoritatively)."""
    # Quote with the SAME hardened prices /api/mm/add credits with, so the UI
    # validates identically to the authoritative server-side check.
    await market.refresh_prices()
    px = market.last_prices()
    return {
        "enabled": bool(os.environ.get("CLEAN_MM_WALLET", "").strip()),
        "wallet": os.environ.get("CLEAN_MM_WALLET", "").strip(),
        "sol_usd": float(px.get("sol_usd") or 0),
        "clean_usd": float(px.get("clean_usd") or 0),
        "min_usd": econ.MM_MIN_USD,
        "max_usd": econ.MM_MAX_USD,
    }


# --------------------------------------------------------------------------- #
#  READS                                                                       #
# --------------------------------------------------------------------------- #
@app.post("/api/profile")
async def api_profile(body: Tok):
    payload = _require(body.token)
    wallet = payload["w"]
    with db.db() as conn:
        row = db.get_staker(conn, wallet)
        if not row:
            raise HTTPException(404, "unknown wallet")
        await _refresh_balance(conn, row)
        _accrue(conn, wallet)
        out = _profile(conn, wallet)
        # sliding session: hand the client a fresh token once the current one
        # has aged past the refresh threshold (active users never re-login)
        fresh = auth.maybe_refresh(payload)
        if fresh:
            out["refreshed_token"] = fresh
        return out


def _clean_social_text(v: str | None, limit: int = 96) -> str:
    return re.sub(r"[\r\n\t]+", " ", str(v or "").strip())[:limit]


@app.post("/api/social/claim")
def api_social_claim(body: SocialClaimBody, request: Request):
    """User-facing social verification request.

    Telegram can be verified automatically because we have signed Telegram
    initData on the session. X/Discord are intentionally only queued as pending;
    paying a boost for self-reported handles would be a farmable money bug.
    """
    payload = _require(body.token)
    wallet = payload["w"]
    ratelimit.hit(request, "write", extra_key=wallet)
    try:
        platform = db._social_platform(body.platform)
    except ValueError:
        raise HTTPException(400, "unknown social platform")
    with db.db() as conn:
        row = db.get_staker(conn, wallet)
        if not row:
            raise HTTPException(404, "unknown wallet")
        _accrue(conn, wallet)
        if platform == "tg":
            tg_id = row["tg_id"] if row["tg_id"] is not None else payload.get("t")
            if tg_id is None:
                raise HTTPException(400, "open the Mini App in Telegram to verify TG")
            db.social_set(
                conn,
                wallet=wallet,
                platform="tg",
                verified=True,
                status="verified",
                handle=row["username"],
                method="telegram_initData",
            )
            return {"ok": True, "platform": "tg", "status": "verified", "profile": _profile(conn, wallet)}
        existing = db.social_status(conn, wallet).get(platform, {})
        if existing.get("verified"):
            return {"ok": True, "platform": platform, "status": "verified", "profile": _profile(conn, wallet)}
        handle = _clean_social_text(body.handle)
        if not handle:
            raise HTTPException(400, f"{platform} handle required")
        db.social_set(
            conn,
            wallet=wallet,
            platform=platform,
            verified=False,
            status="pending",
            handle=handle,
            method="user_submitted",
            proof=_clean_social_text(body.proof, 200),
        )
        return {"ok": True, "platform": platform, "status": "pending", "profile": _profile(conn, wallet)}


@app.post("/api/leaderboard")
def api_leaderboard(body: Tok):
    wallet = _require(body.token)["w"]
    with db.db() as conn:
        eff_expr = db.effective_staked_expr()
        # eff_expr is an internal SQL constant, not user input.
        rows = conn.execute(
            f"SELECT wallet, username, recorded_staked, cached_balance, total_burned, "  # nosec B608
            f"({eff_expr}) AS effective_staked FROM stakers "
            f"WHERE ({eff_expr}) > 0 ORDER BY effective_staked DESC LIMIT 50"
        ).fetchall()
        board = [
            {
                "rank": i + 1,
                "name": r["username"] or (r["wallet"][:4] + "…" + r["wallet"][-4:]),
                "staked": db.to_ui(r["effective_staked"]),
                "recorded_staked": db.to_ui(r["recorded_staked"]),
                "burned": db.to_ui(r["total_burned"]),
                "me": r["wallet"] == wallet,
            }
            for i, r in enumerate(rows)
        ]
        return {"leaderboard": board}


@app.post("/api/referrals")
def api_referrals(body: Tok, request: Request):
    wallet = _require(body.token)["w"]
    with db.db() as conn:
        code = db.ref_code(conn, wallet)
        share = f"{_origin(request)}/g/{code}" if code else None
        return {
            "ref_code": code or wallet,
            "link": share,
            "active_referrals": db.active_referrals(conn, wallet),
            "reward": "each active referral adds to your APR (see /api/profile apr.referral_boost)",
        }


# --------------------------------------------------------------------------- #
#  GAME (Clean Hands tap/idle) — cloud save + Most Wanted leaderboard.         #
#  Identity is the Telegram user (verified initData), so progress follows the  #
#  player across devices without a connected wallet. Opened outside Telegram   #
#  (no initData) the client stays localStorage-only and never calls these.     #
#  Additive routes — they never touch the staking / payment tables.            #
# --------------------------------------------------------------------------- #
GAME_STATE_MAX = int(os.environ.get("GAME_STATE_MAX", "8192"))  # max save-blob bytes

# Server-side Escape verification. These values are deliberately operator-tuned
# via env so the live anti-farm thresholds do not have to match the public repo.
# Active time is credited only through server-observed heartbeat saves; a single
# forged x33 blob earns no staking boost.
GAME_ESCAPE_MIN_SECONDS = {
    5.0: int(os.environ.get("GAME_ESCAPE_MIN_SECONDS_X5", "1800")),    # 30m active
    10.0: int(os.environ.get("GAME_ESCAPE_MIN_SECONDS_X10", "7200")),  # 2h active
    20.0: int(os.environ.get("GAME_ESCAPE_MIN_SECONDS_X20", "28800")), # 8h active
    33.0: int(os.environ.get("GAME_ESCAPE_MIN_SECONDS_X33", "86400")), # 24h active
}
GAME_ESCAPE_HEARTBEAT_MIN = int(os.environ.get("GAME_ESCAPE_HEARTBEAT_MIN", "3"))
GAME_ESCAPE_HEARTBEAT_MAX = int(os.environ.get("GAME_ESCAPE_HEARTBEAT_MAX", "120"))
GAME_ESCAPE_AUTO_RISK_MAX = int(os.environ.get("GAME_ESCAPE_AUTO_RISK_MAX", "35"))
GAME_ESCAPE_REVIEW_RISK = int(os.environ.get("GAME_ESCAPE_REVIEW_RISK", "60"))
GAME_ESCAPE_BLOCK_RISK = int(os.environ.get("GAME_ESCAPE_BLOCK_RISK", "110"))


class GameSaveBody(BaseModel):
    initData: str
    state: str = ""
    score: int = 0
    name: str | None = None
    proof: dict | None = None


class GameLoadBody(BaseModel):
    initData: str


def _game_player(init_data: str) -> tuple[str, str]:
    tg = auth.verify_init_data(init_data)
    if not tg:
        raise HTTPException(401, "bad Telegram initData")
    name = tg.get("username") or tg.get("first_name") or "anon"
    return f"tg:{tg['id']}", str(name)[:32]


def _game_state_with_escape_floor(state: str, floor_score: float) -> str | None:
    """Preserve the highest permanent Escape multiplier across stale saves."""
    try:
        data = json.loads(state or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    cur = econ.escape_score_from_state(data)
    if cur >= floor_score:
        return state
    s = data.get("S")
    if not isinstance(s, dict):
        s = {}
        data["S"] = s
    # In the current game, Escape multiplier = 1 + 0.75 * prestige.
    prestige_floor = int(max(0.0, ((float(floor_score) - 1.0) / 0.75)) + 0.999999)
    try:
        cur_prestige = float(s.get("prestige") or 0)
    except (TypeError, ValueError):
        cur_prestige = 0.0
    s["prestige"] = max(cur_prestige, prestige_floor)
    return json.dumps(data, separators=(",", ":"))


def _n(proof: dict | None, key: str, default: float = 0.0) -> float:
    if not isinstance(proof, dict):
        return default
    try:
        return float(proof.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _s(proof: dict | None, key: str, default: str = "") -> str:
    if not isinstance(proof, dict):
        return default
    return str(proof.get(key, default) or default)


def _escape_tier_for(score: float) -> float:
    score = float(score or 0.0)
    for threshold, _boost in econ.ESCAPE_TIERS:
        if score >= threshold:
            return float(threshold)
    return 0.0


def _escape_min_seconds_for(score: float) -> int:
    tier = _escape_tier_for(score)
    return GAME_ESCAPE_MIN_SECONDS.get(tier, 0)


def _max_escape_score_allowed(raw_score: float, active_seconds: int, hold_until_ts: int, now: int) -> float:
    if hold_until_ts and now < hold_until_ts:
        return 0.0
    for threshold, _boost in econ.ESCAPE_TIERS:
        if raw_score >= threshold and active_seconds >= GAME_ESCAPE_MIN_SECONDS.get(float(threshold), 0):
            return float(threshold)
    return 0.0


def _verify_escape_progress(
    conn,
    player: str,
    raw_escape_score: float,
    raw_prestige: int,
    proof: dict | None,
    now: int | None = None,
) -> dict:
    """Update the hidden server-side Escape trust ledger.

    This is intentionally conservative. It never trusts a final client save for
    money; it only promotes to a reward tier after server-observed active time
    and low-risk telemetry. Suspicious progress is kept for cloud restore but
    marked review/blocked and ignored by staking APR.
    """
    now = int(now or time.time())
    raw_escape_score = max(0.0, min(float(raw_escape_score or 0.0), 10_000.0))
    raw_prestige = max(0, int(raw_prestige or 0))
    row = db.game_verify_load(conn, player)

    first_seen = int(row["first_seen_ts"]) if row else now
    last_save = int(row["last_save_ts"]) if row else now
    active = int(row["active_seconds"]) if row else 0
    save_count = int(row["save_count"]) if row else 0
    risk = int(row["risk_score"]) if row else 0
    verified_score = float(row["verified_escape_score"]) if row else 0.0
    verified_prestige = int(row["verified_prestige"]) if row else 0
    prior_raw = float(row["raw_escape_score"]) if row else 0.0
    hold_until = int(row["hold_until_ts"]) if row else 0
    prior_sid = row["session_id"] if row else ""
    prior_seq = int(row["last_seq"]) if row else 0

    dt = max(0, now - last_save)
    clean_heartbeat = GAME_ESCAPE_HEARTBEAT_MIN <= dt <= GAME_ESCAPE_HEARTBEAT_MAX
    if row and clean_heartbeat:
        active += dt

    reasons: list[str] = []
    delta_risk = 0
    seq = int(_n(proof, "seq", 0))
    sid = _s(proof, "sid", "")[:64]
    inputs = int(_n(proof, "inputs", 0))
    taps = int(_n(proof, "taps", 0))
    client_active = max(0.0, _n(proof, "activeMs", 0) / 1000.0)

    raw_jump = raw_escape_score - prior_raw
    if raw_jump > 0:
        if not row:
            delta_risk += 20
            reasons.append("new_escape_claim")
        if raw_jump >= 4:
            delta_risk += 20
            reasons.append("large_escape_jump")
        if raw_jump >= 10:
            delta_risk += 35
            reasons.append("huge_escape_jump")
        tier = _escape_tier_for(raw_escape_score)
        if tier and active < max(60, int(_escape_min_seconds_for(tier) * 0.15)):
            delta_risk += 45
            reasons.append("too_fast_for_tier")
        if inputs + taps <= 0:
            delta_risk += 10
            reasons.append("no_input_signal")

    if row and dt < 2:
        delta_risk += 8
        reasons.append("save_flood")
    if row and dt > 0 and dt > GAME_ESCAPE_HEARTBEAT_MAX * 6 and raw_jump > 0:
        delta_risk += 12
        reasons.append("long_gap_jump")
    if not isinstance(proof, dict):
        delta_risk += 5
        reasons.append("missing_proof")
    else:
        elapsed = max(0, now - first_seen)
        if client_active > elapsed + 180:
            delta_risk += 20
            reasons.append("client_time_ahead")
        if prior_sid and sid == prior_sid and seq and seq <= prior_seq:
            delta_risk += 8
            reasons.append("seq_replay")
        if _s(proof, "vis", "visible") == "hidden" and raw_jump > 0:
            delta_risk += 8
            reasons.append("hidden_progress")

    # Slow decay for clean heartbeats keeps normal players from being trapped by
    # tiny telemetry oddities, while major fake jumps remain sticky.
    if clean_heartbeat and delta_risk == 0 and risk > 0:
        risk = max(0, risk - 1)
    risk = min(1000, risk + delta_risk)

    allowed_score = _max_escape_score_allowed(raw_escape_score, active, hold_until, now)
    if risk < GAME_ESCAPE_AUTO_RISK_MAX and allowed_score > verified_score:
        verified_score = allowed_score
        verified_prestige = econ.escape_prestige_for_score(verified_score)
        reasons.append("tier_promoted")

    if risk >= GAME_ESCAPE_BLOCK_RISK:
        status = "blocked"
    elif risk >= GAME_ESCAPE_REVIEW_RISK:
        status = "review"
    elif raw_escape_score > verified_score + 1e-9:
        status = "verifying"
    elif verified_score >= 5:
        status = "verified"
    else:
        status = "unverified"

    reason = ",".join(reasons[-6:])
    db.game_verify_save(
        conn,
        player=player,
        verified_escape_score=verified_score,
        verified_prestige=verified_prestige,
        raw_escape_score=max(raw_escape_score, prior_raw),
        raw_prestige=max(raw_prestige, int(row["raw_prestige"]) if row else 0),
        first_seen_ts=first_seen,
        last_save_ts=now,
        active_seconds=active,
        save_count=save_count + 1,
        risk_score=risk,
        status=status,
        hold_until_ts=hold_until,
        session_id=sid or prior_sid,
        last_seq=max(seq, prior_seq),
        reason=reason,
        updated_ts=now,
    )
    if "tier_promoted" in reasons or status in ("review", "blocked"):
        db.game_verify_event(conn, player, status, raw_escape_score, verified_score, risk, reason)
    return {
        "raw_escape_score": raw_escape_score,
        "verified_escape_score": verified_score,
        "verified_escape_boost": econ.escape_boost(verified_score),
        "status": status,
    }


@app.post("/api/game/save")
def api_game_save(body: GameSaveBody):
    player, tg_name = _game_player(body.initData)
    state = body.state or ""
    if len(state) > GAME_STATE_MAX:
        raise HTTPException(413, "game state too large")
    score = max(0, min(int(body.score or 0), 10**15))
    name = str(body.name or tg_name)[:32]
    escape_score = econ.escape_score_from_state(state)
    raw_prestige = econ.escape_prestige_from_state(state)
    with db.db() as conn:
        existing = db.game_load(conn, player)
        if existing:
            prior_escape = econ.escape_score_from_state(existing["state"])
            if prior_escape > escape_score:
                state = _game_state_with_escape_floor(state, prior_escape) or existing["state"]
                escape_score = econ.escape_score_from_state(state)
                raw_prestige = econ.escape_prestige_from_state(state)
        db.game_save(conn, player, name, state, score)
        verify = _verify_escape_progress(conn, player, escape_score, raw_prestige, body.proof)
    return {
        "ok": True,
        "escape_score": escape_score,
        "escape_boost": econ.escape_boost(verify["verified_escape_score"]),
        "verified_escape_score": verify["verified_escape_score"],
        "verified_escape_boost": verify["verified_escape_boost"],
        "escape_status": verify["status"],
    }


@app.post("/api/game/load")
def api_game_load(body: GameLoadBody):
    player, _ = _game_player(body.initData)
    with db.db() as conn:
        row = db.game_load(conn, player)
    if not row:
        return {"state": None, "score": 0}
    return {"state": row["state"], "score": row["score"], "updated_ts": row["updated_ts"]}


@app.get("/api/game/leaderboard")
def api_game_leaderboard(limit: int = 20):
    limit = max(1, min(int(limit), 50))
    with db.db() as conn:
        rows = db.game_top(conn, limit)
    return {"top": [{"name": (r["name"] or "anon"), "score": r["score"]} for r in rows]}


@app.post("/api/track")
def api_track():
    # Anonymous, fire-and-forget product analytics from the game/mini-app. We do
    # not persist it (no PII, no storage growth); accept and drop so the client's
    # beacon/fetch never 404s. Payload size is already capped by the body guard.
    return Response(status_code=204)


@app.get("/api/ref")
def api_ref(action: str = "", ref: str = "", nid: str = ""):
    # The game's lightweight, anonymous (device-id) referral ping. The welcome
    # bonus is applied client-side; there's no server-side game economy to credit
    # yet, so acknowledge it instead of 404ing.
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
#  PORTFOLIO — link several wallets under one dashboard. Ownership of EVERY    #
#  linked wallet is proven the same way as login: a fresh ed25519 signature    #
#  over a single-use server nonce. No signature, no link — you can't claim a   #
#  whale wallet you don't control.                                             #
# --------------------------------------------------------------------------- #
PORTFOLIO_LIMIT = int(os.environ.get("STAKE_PORTFOLIO_LIMIT", "20"))


class LinkBody(BaseModel):
    token: str
    wallet: str
    signature: str
    nonce: str


class UnlinkBody(BaseModel):
    token: str
    wallet: str


async def _portfolio(conn, sess_wallet: str) -> dict:
    """Live snapshot of every wallet in the caller's cluster + aggregates.
    Balances go through the same cached on-chain refresh as /api/profile."""
    anchor = db.link_owner(conn, sess_wallet)
    wallets, totals = [], {"balance": 0.0, "staked": 0.0, "pending_rewards": 0.0, "total_burned": 0.0}
    for w in db.cluster_wallets(conn, sess_wallet):
        row = db.get_staker(conn, w)
        if not row:
            continue
        await _refresh_balance(conn, row)
        _accrue(conn, w)
        row = db.get_staker(conn, w)
        eff_base, _secs, _refs, apr = _apr_for(conn, w, row)
        item = {
            "wallet": w,
            "anchor": w == anchor,
            "me": w == sess_wallet,
            "balance": db.to_ui(row["cached_balance"]),
            "staked": db.to_ui(row["recorded_staked"]),
            "staked_effective": db.to_ui(eff_base),
            "pending_rewards": db.to_ui(row["accrued"]),
            "total_burned": db.to_ui(row["total_burned"]),
            "apr_pct": round(apr.effective_apr * 100, 2),
        }
        for k in totals:
            totals[k] += item[k]
        wallets.append(item)
    totals = {k: round(v, 6) for k, v in totals.items()}
    totals["holdings"] = round(totals["balance"] + totals["pending_rewards"], 6)
    return {"wallets": wallets, "totals": totals, "count": len(wallets), "limit": PORTFOLIO_LIMIT}


@app.post("/api/portfolio")
async def api_portfolio(body: Tok):
    sess_wallet = _require(body.token)["w"]
    with db.db() as conn:
        if not db.get_staker(conn, sess_wallet):
            raise HTTPException(404, "unknown wallet")
        return await _portfolio(conn, sess_wallet)


@app.post("/api/link")
async def api_link(body: LinkBody, request: Request):
    sess_wallet = _require(body.token)["w"]
    ratelimit.hit(request, "write", extra_key=sess_wallet)
    w = body.wallet.strip()
    if not auth.is_valid_wallet(w):
        raise HTTPException(400, "invalid Solana wallet address")
    if not auth.consume_nonce(w, body.nonce):
        raise HTTPException(401, "bad or expired nonce — request a new one")
    if not auth.verify_wallet_signature(w, auth.login_message(w, body.nonce), body.signature):
        raise HTTPException(401, "bad wallet signature")
    with db.db() as conn:
        if not db.get_staker(conn, sess_wallet):
            raise HTTPException(404, "unknown wallet")
        db.upsert_staker(conn, w)
        err = db.link_wallet(conn, sess_wallet, w, PORTFOLIO_LIMIT)
        if err:
            raise HTTPException(409, err)
        db.record(conn, sess_wallet, "link", 0, detail=w)
        return await _portfolio(conn, sess_wallet)


@app.post("/api/unlink")
async def api_unlink(body: UnlinkBody, request: Request):
    sess_wallet = _require(body.token)["w"]
    ratelimit.hit(request, "write", extra_key=sess_wallet)
    with db.db() as conn:
        err = db.unlink_wallet(conn, sess_wallet, body.wallet.strip())
        if err:
            raise HTTPException(400, err)
        db.record(conn, sess_wallet, "unlink", 0, detail=body.wallet.strip())
        return await _portfolio(conn, sess_wallet)


# --------------------------------------------------------------------------- #
#  ADMIN — manual payout workflow (treasury/cron). Gate with STAKE_ADMIN_TOKEN. #
# --------------------------------------------------------------------------- #
ADMIN_TOKEN = os.environ.get("STAKE_ADMIN_TOKEN", "")
OPS_FLAGS = {
    "halt_all",
    "halt_staking",
    "halt_claims",
    "halt_burns",
    "halt_payout_setup",
    "halt_mm",
    "halt_bridge",
    "halt_escape_boost",
}


class AdminTok(BaseModel):
    admin_token: str


class AdminMark(BaseModel):
    admin_token: str
    claim_id: int
    tx_sig: str


class AdminFlag(BaseModel):
    admin_token: str
    key: str
    value: bool


class AdminGameVerify(BaseModel):
    admin_token: str
    tg_id: int | None = None
    player: str | None = None
    verified_escape_score: float


class AdminSocialVerify(BaseModel):
    admin_token: str
    wallet: str
    platform: str
    verified: bool = True
    handle: str | None = None
    proof: str | None = None


def _require_admin(tok: str) -> None:
    if not ADMIN_TOKEN or not hmac.compare_digest(tok or "", ADMIN_TOKEN):
        raise HTTPException(403, "admin only")


def _assert_ops_open(conn, flag: str) -> None:
    if db.flag_enabled(conn, "halt_all") or db.flag_enabled(conn, flag):
        raise HTTPException(503, "temporarily paused by operator")


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
                    "gross": db.to_ui(r["gross_amount"]),
                    "fee": db.to_ui(r["fee_amount"]),
                    "fee_usd": r["fee_usd"],
                    "destination": r["destination"] or r["wallet"],
                    "rules_version": r["rules_version"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        }


@app.post("/api/admin/vip")
def api_admin_vip(body: AdminTok):
    """VIP airdrop snapshot — every wallet that made a qualifying MM deposit."""
    _require_admin(body.admin_token)
    with db.db() as conn:
        vips = db.list_vips(conn)
        return {"count": len(vips), "vips": vips}


@app.post("/api/admin/mark_paid")
async def api_admin_mark_paid(body: AdminMark):
    _require_admin(body.admin_token)
    with db.db() as conn:
        claim = db.get_claim(conn, body.claim_id)
        if not claim or claim["status"] != "requested":
            raise HTTPException(409, "claim not found or already paid")
        ok = await solana.verify_transfer(
            body.tx_sig,
            claim["destination"] or claim["wallet"],
            int(claim["amount"]),
        )
        if not ok:
            raise HTTPException(400, "treasury transfer was not confirmed for this claim")
        n = db.mark_claim_paid(conn, body.claim_id, body.tx_sig)
        if n != 1:
            raise HTTPException(409, "claim not found or already paid")
        return {"ok": True, "claim_id": body.claim_id, "status": "paid"}


@app.post("/api/admin/flags")
def api_admin_flags(body: AdminTok):
    _require_admin(body.admin_token)
    with db.db() as conn:
        return {
            "flags": [
                {"key": r["key"], "value": r["value"], "updated_at": r["updated_at"]}
                for r in db.list_flags(conn)
            ],
            "known_flags": sorted(OPS_FLAGS),
        }


@app.post("/api/admin/set_flag")
def api_admin_set_flag(body: AdminFlag):
    _require_admin(body.admin_token)
    key = body.key.strip()
    if key not in OPS_FLAGS:
        raise HTTPException(400, "unknown ops flag")
    with db.db() as conn:
        db.set_flag(conn, key, "1" if body.value else "0")
    return {"ok": True, "key": key, "value": body.value}


@app.post("/api/admin/game_reviews")
def api_admin_game_reviews(body: AdminTok):
    """Review queue for suspicious Escape reward saves. Admin-only; never exposed
    to the client because exact reasons are detector intelligence."""
    _require_admin(body.admin_token)
    with db.db() as conn:
        rows = conn.execute(
            "SELECT player, raw_escape_score, verified_escape_score, active_seconds, "
            "risk_score, status, reason, updated_ts FROM game_verification "
            "WHERE status IN ('review','blocked','verifying') "
            "ORDER BY risk_score DESC, updated_ts DESC LIMIT 100"
        ).fetchall()
        return {
            "reviews": [
                {
                    "player": r["player"],
                    "raw_escape_score": r["raw_escape_score"],
                    "verified_escape_score": r["verified_escape_score"],
                    "active_seconds": r["active_seconds"],
                    "risk_score": r["risk_score"],
                    "status": r["status"],
                    "reason": r["reason"],
                    "updated_ts": r["updated_ts"],
                }
                for r in rows
            ]
        }


@app.post("/api/admin/game_verify")
def api_admin_game_verify(body: AdminGameVerify):
    """Manual escape-verification override for support/audit review."""
    _require_admin(body.admin_token)
    player = (body.player or "").strip()
    if not player and body.tg_id is not None:
        player = f"tg:{int(body.tg_id)}"
    if not player.startswith("tg:"):
        raise HTTPException(400, "player or tg_id required")
    verified_score = max(0.0, min(float(body.verified_escape_score or 0.0), econ.ESCAPE_TIERS[0][0]))
    now = int(time.time())
    with db.db() as conn:
        row = db.game_verify_load(conn, player)
        db.game_verify_save(
            conn,
            player=player,
            verified_escape_score=verified_score,
            verified_prestige=econ.escape_prestige_for_score(verified_score),
            raw_escape_score=max(verified_score, float(row["raw_escape_score"] or 0.0) if row else 0.0),
            raw_prestige=max(econ.escape_prestige_for_score(verified_score), int(row["raw_prestige"] or 0) if row else 0),
            first_seen_ts=int(row["first_seen_ts"] or now) if row else now,
            last_save_ts=int(row["last_save_ts"] or now) if row else now,
            active_seconds=int(row["active_seconds"] or 0) if row else 0,
            save_count=int(row["save_count"] or 0) if row else 0,
            risk_score=0,
            status="verified" if verified_score >= 5 else "unverified",
            hold_until_ts=0,
            session_id=row["session_id"] if row else None,
            last_seq=int(row["last_seq"] or 0) if row else 0,
            reason="admin_override",
            updated_ts=now,
        )
        db.game_verify_event(conn, player, "admin_override", verified_score, verified_score, 0, "admin_override")
    return {"ok": True, "player": player, "verified_escape_score": verified_score}


@app.post("/api/admin/social_reviews")
def api_admin_social_reviews(body: AdminTok):
    _require_admin(body.admin_token)
    with db.db() as conn:
        rows = conn.execute(
            "SELECT wallet, platform, handle, verified, status, updated_at "
            "FROM social_verifications WHERE status IN ('pending','rejected') "
            "ORDER BY updated_at DESC LIMIT 100"
        ).fetchall()
        return {
            "reviews": [
                {
                    "wallet": r["wallet"],
                    "platform": r["platform"],
                    "handle": r["handle"],
                    "verified": bool(r["verified"]),
                    "status": r["status"],
                    "updated_at": r["updated_at"],
                }
                for r in rows
            ]
        }


@app.post("/api/admin/social_verify")
def api_admin_social_verify(body: AdminSocialVerify):
    _require_admin(body.admin_token)
    if not auth.is_valid_wallet(body.wallet):
        raise HTTPException(400, "invalid wallet")
    try:
        platform = db._social_platform(body.platform)
    except ValueError:
        raise HTTPException(400, "unknown social platform")
    with db.db() as conn:
        if not db.get_staker(conn, body.wallet):
            raise HTTPException(404, "unknown wallet")
        _accrue(conn, body.wallet)
        existing = db.social_status(conn, body.wallet).get(platform, {})
        handle = _clean_social_text(body.handle) if body.handle is not None else existing.get("handle", "")
        db.social_set(
            conn,
            wallet=body.wallet,
            platform=platform,
            verified=bool(body.verified),
            status="verified" if body.verified else "rejected",
            handle=handle,
            method="admin_review",
            proof=_clean_social_text(body.proof, 200),
        )
        return {
            "ok": True,
            "wallet": body.wallet,
            "platform": platform,
            "verified": bool(body.verified),
            "socials": _social_public(conn, body.wallet),
        }


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
            "escape_tiers": econ.ESCAPE_TIERS,
            "escape_cap": econ.ESCAPE_TIERS[0][0],
            "escape_cap_boost": econ.ESCAPE_TIERS[0][1],
            "escape_verification": True,
            "escape_social_gate": True,
            "social_required": list(db.SOCIAL_PLATFORMS),
            "social_weight_each": 1 / len(db.SOCIAL_PLATFORMS),
            "burn_unit": econ.BURN_UNIT,
            "burn_apr_per_unit": econ.BURN_APR_PER_UNIT,
            "burn_cap_apr": econ.BURN_CAP_APR,
            "claim_lock_days": CLAIM_LOCK_DAYS,
            "claim_fee_usd": _claim_fee_usd(),
            "payout_setup_days": _payout_setup_days(),
            "botUsername": os.environ.get("MINIAPP_BOT_USERNAME", "").lstrip("@"),
            "appShortName": os.environ.get("MINIAPP_SHORT_NAME", "app"),
            "miniappUrl": os.environ.get("MINIAPP_URL", "").rstrip("/"),
            # Browser-safe RPC for the in-app swap widget. NEVER expose the paid
            # SOLANA_RPC_URL here; operators set a separate public-ish endpoint.
            "swapRpc": os.environ.get("MINIAPP_SWAP_RPC", ""),
            # No Stains Bridge tab: the exchange URL (your public affiliate/ref
            # link, or a reskinned widget URL). Defaults to the CLEAN EasyBit ref
            # so it works out of the box; override per-deploy with MINIAPP_BRIDGE_URL.
            "bridgeUrl": os.environ.get(
                "MINIAPP_BRIDGE_URL", "https://easybit.com/?ref_id=d4RqwQRDBs"
            ).strip(),
            # Most exchange HOMEPAGES refuse to be iframed (X-Frame-Options), so by
            # default the tab is a branded launch card that opens the URL externally.
            # Set MINIAPP_BRIDGE_EMBED=1 ONLY when bridgeUrl is a real embeddable
            # widget URL (one that allows framing) to render it inline.
            "bridgeEmbed": os.environ.get("MINIAPP_BRIDGE_EMBED", "").strip() in ("1", "true", "yes"),
            # No Stains Bridge (white-label EasyBit) — when EASYBIT_API_KEY is set,
            # bridgeMode is "api" and the app renders the in-app swap form; without
            # it, bridgeMode is "link" and the existing launch-card stays. These
            # fields let the UI show the SAME min/fee the server enforces.
            **bridge.public_config(),
            "decimals": db.DECIMALS,
            "mint": market.MINT,
            # WalletConnect relay (QR — works with ANY wallet app; the escape
            # hatch when Telegram users don't have Phantom/Solflare installed).
            # OFF unless the operator sets the project id; never a secret.
            "wcProjectId": os.environ.get("WALLETCONNECT_PROJECT_ID", "").strip(),
            # In-app burn (signs a real burn tx in the wallet). OFF by default —
            # burn is irreversible; enable only after on-device QA.
            "inAppBurn": os.environ.get("MINIAPP_INAPP_BURN", "").strip() in ("1", "true", "yes"),
            # Market-maker liquidity booster. OFF until CLEAN_MM_WALLET (the reserve
            # that receives deposits) is set. mmMinUsd/mmMaxUsd mirror the rules the
            # server enforces so the UI can validate identically before signing.
            "mmEnabled": bool(os.environ.get("CLEAN_MM_WALLET", "").strip()),
            "mmWallet": os.environ.get("CLEAN_MM_WALLET", "").strip(),
            "mmMinUsd": econ.MM_MIN_USD,
            "mmMaxUsd": econ.MM_MAX_USD,
            "mmLpCap": econ.MM_LP_CAP,
            "solMint": solana.SOL_MINT,
            # Embedded Game tab — defaults to our same-origin /play build (the
            # self-contained standalone served by this API). Override per-deploy
            # with MINIAPP_GAME_URL if the game is hosted elsewhere.
            "gameUrl": os.environ.get("MINIAPP_GAME_URL", "/play").strip(),
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
    """Public, aggregate-only protocol stats for the app's 'Supply washed' panel
    and the season campaign card. Real DB aggregates only — never invented."""
    with db.db() as conn:
        eff_expr = db.effective_staked_expr()
        # eff_expr is an internal SQL constant, not user input.
        row = conn.execute(
            "SELECT COALESCE(SUM(total_burned),0) AS burned,"
            " SUM(CASE WHEN total_burned > 0 THEN 1 ELSE 0 END) AS burners,"
            f" SUM(CASE WHEN ({eff_expr}) > 0 THEN 1 ELSE 0 END) AS stakers,"  # nosec B608
            f" COALESCE(SUM({eff_expr}),0) AS staked"
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
    # Season campaign: community goal to burn SEASON_BURN_GOAL_PCT of the supply
    # before SEASON_END_TS. Pure presentation over real aggregates.
    season_end = int(os.environ.get("SEASON_END_TS", "0") or 0)
    now = int(time.time())
    if season_end > now and supply > 0:
        goal_pct = float(os.environ.get("SEASON_BURN_GOAL_PCT", "5") or 5)
        goal_tokens = supply * goal_pct / 100.0
        out["season"] = {
            "name": os.environ.get("SEASON_NAME", "Season 1 — The Big Wash"),
            "ends_at": season_end,
            "days_left": max(1, -(-(season_end - now) // 86400)),
            "goal_pct": goal_pct,
            "goal_tokens": goal_tokens,
            "progress_pct": round(min(100.0, out["total_burned"] / goal_tokens * 100.0), 2)
            if goal_tokens > 0
            else 0,
        }
    return out


# --------------------------------------------------------------------------- #
#  TELEGRAM WALLET HANDSHAKE (server-side)                                      #
#  Telegram iOS kills/relaunches the Mini App webview across the wallet hop, so #
#  doing the encrypted deeplink handshake IN the webview is unreliable (lost    #
#  keys -> "decrypt failed" / stuck spinner). Here the SERVER holds the         #
#  ephemeral x25519 key: Phantom's connect/sign callbacks land on the server,   #
#  which decrypts, verifies the ed25519 signature over a single-use nonce, and  #
#  mints the session. The webview only polls /api/tg/poll keyed by its verified #
#  Telegram identity, so it completes no matter how often the webview reloads.  #
#  Security is unchanged: ownership is still proven by the wallet signature over #
#  a server nonce; the Telegram HMAC still binds one TG id to one wallet. The    #
#  deeplink encryption is transport only, so moving it server-side adds no trust.#
# --------------------------------------------------------------------------- #
_WALLET_BASE = {
    "phantom": "https://phantom.app/ul/v1",
    "solflare": "https://solflare.com/ul/v1",
    "backpack": "https://backpack.app/ul/v1",
}
# Custom URL schemes open the installed app unconditionally. iOS sometimes
# refuses to app-open an https universal link that points back at the app the
# user just came from (anti-loop heuristic) and lands on the wallet's website
# — the "download Phantom" page — so the primary button uses the scheme and
# the https UL stays as a fallback link.
_WALLET_SCHEME = {
    "phantom": "phantom://ul/v1",
    "solflare": "solflare://ul/v1",
    "backpack": "backpack://ul/v1",
}
_SID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_TG_TTL = 600  # seconds for a whole connect->sign handshake


def _tg_log(sid: str, step: str, **kv) -> None:
    """One greppable line per handshake step in journalctl. Never logs secrets —
    sid is truncated, wallets shortened, payloads never printed."""
    extras = " ".join(f"{k}={v}" for k, v in kv.items())
    print(f"[tg-handshake] sid={sid[:8]}… {step} {extras}".rstrip(), flush=True)


def _b58(b: bytes) -> str:
    return base58.b58encode(bytes(b)).decode()


def _origin(request: Request) -> str:
    base = os.environ.get("MINIAPP_URL", "").rstrip("/")
    return base or str(request.base_url).rstrip("/")


def _tg_get(sid: str):
    v = store.get_store().get("tg:" + sid)
    return json.loads(v) if v else None


def _tg_put(sid: str, st: dict) -> None:
    store.get_store().setex("tg:" + sid, _TG_TTL, json.dumps(st))


def _tg_page(title: str, body: str, err: bool = False) -> HTMLResponse:
    color = "#c0392b" if err else "#0F3E73"
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>$CLEAN</title><style>"
        "body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "background:#F4FAFF;color:#16385c;display:flex;align-items:center;justify-content:center;"
        "min-height:100vh;text-align:center;padding:24px}"
        ".c{background:#fff;border:1.5px solid rgba(27,93,166,.16);border-radius:22px;"
        "box-shadow:0 20px 50px -28px rgba(27,93,166,.4);padding:34px 26px;max-width:360px}"
        "img{width:72px;height:72px;object-fit:contain;margin-bottom:10px}"
        f"h1{{font-size:1.3rem;color:{color};margin:0 0 8px}}"
        "p{color:#5d7ea3;font-size:.95rem;line-height:1.5;margin:0 0 18px}"
        "a.btn{display:block;background:#2E74C0;color:#fff;text-decoration:none;font-weight:700;"
        "border-radius:14px;padding:14px;box-shadow:0 12px 24px -12px rgba(46,116,192,.8)}"
        "</style></head><body><div class='c'>"
        "<img src='/glove.png?v=2' alt='$CLEAN'>"
        f"<h1>{title}</h1>{body}</div></body></html>"
    )
    return HTMLResponse(html)


def _tg_open_app_button(request: Request) -> str:
    bot = os.environ.get("MINIAPP_BOT_USERNAME", "").lstrip("@")
    short = os.environ.get("MINIAPP_SHORT_NAME", "app")
    if bot:
        return f"<a class='btn' href='https://t.me/{bot}/{short}'>↩ Open $CLEAN</a>"
    return "<p>Switch back to Telegram to finish.</p>"


@app.post("/api/tg/start")
def api_tg_start(body: TgStart, request: Request):
    ratelimit.hit(request, "tg")
    tg = auth.verify_init_data(body.initData)
    if not tg:
        raise HTTPException(401, "bad Telegram initData")
    try:
        tg_id = int(tg["id"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(401, "bad Telegram user")
    base = _WALLET_BASE.get(body.wallet)
    if not base:
        raise HTTPException(400, "unknown wallet")
    sk = PrivateKey.generate()
    sid = secrets.token_urlsafe(18)
    _tg_put(
        sid,
        {
            "tg": tg_id,
            "base": base,
            "wid": body.wallet,
            "sk": _b58(bytes(sk)),
            "ref": tg.get("_start_param", "") or "",
            "username": tg.get("username") or tg.get("first_name"),
            "status": "started",
        },
    )
    _tg_log(sid, "start", tg=tg_id, wallet_app=body.wallet)
    return {"sid": sid, "dapp_pub": _b58(bytes(sk.public_key))}


@app.get("/api/tg/connect/{sid}")
def api_tg_connect(
    sid: str,
    request: Request,
    data: str | None = None,
    nonce: str | None = None,
    phantom_encryption_public_key: str | None = None,
    errorCode: str | None = None,
    errorMessage: str | None = None,
):
    if not _SID_RE.match(sid):
        _tg_log(sid, "connect REJECT", reason="bad_sid")
        return _tg_page("Invalid link", "<p>Reopen the app and connect again.</p>", err=True)
    st = _tg_get(sid)
    if not st:
        _tg_log(sid, "connect REJECT", reason="no_session")
        return _tg_page("Session expired", "<p>Reopen the app and connect again.</p>", err=True)
    if errorCode:
        st.update(status="error", err=errorMessage or "wallet error")
        _tg_put(sid, st)
        _tg_log(sid, "connect CANCELLED", code=errorCode)
        return _tg_page("Connection cancelled", _tg_open_app_button(request), err=True)
    try:
        sk = PrivateKey(base58.b58decode(st["sk"]))
        their = PublicKey(base58.b58decode(phantom_encryption_public_key))
        box = Box(sk, their)
        info = json.loads(box.decrypt(base58.b58decode(data), base58.b58decode(nonce)))
        wallet = info["public_key"]
        wsession = info["session"]
        if not auth.is_valid_wallet(wallet):
            raise ValueError("bad wallet")
        login_nonce = auth.issue_nonce(wallet)
        msg = auth.login_message(wallet, login_nonce)
        payload = json.dumps({"message": _b58(msg.encode()), "session": wsession}).encode()
        n = nacl_random(24)
        ct = box.encrypt(payload, n).ciphertext
        _tg_log(sid, "connect OK", wallet=wallet[:6] + "…")
        st.update(
            status="connected",
            their=phantom_encryption_public_key,
            wallet=wallet,
            wsession=wsession,
            nonce=login_nonce,
        )
        _tg_put(sid, st)
        params = urlencode(
            {
                "dapp_encryption_public_key": _b58(bytes(sk.public_key)),
                "nonce": _b58(n),
                "redirect_link": _origin(request) + "/api/tg/sign/" + sid,
                "payload": _b58(ct),
            }
        )
        ul = f"{st['base']}/signMessage?{params}"
        scheme_base = _WALLET_SCHEME.get(st.get("wid", ""), "")
        app_ul = f"{scheme_base}/signMessage?{params}" if scheme_base else ul
        # NO auto-redirect (JS navigation is not a user gesture). The primary
        # button uses the wallet's custom scheme — iOS opens the installed app
        # unconditionally — with the https UL as a fallback link.
        return _tg_page(
            "🧤 Wallet linked",
            "<p>One more tap — sign in your wallet to finish, then you land back in $CLEAN.</p>"
            f"<a class='btn' href='{app_ul}'>✍️ Approve signature</a>"
            f"<p style='margin-top:14px;font-size:.82rem'><a href='{ul}' "
            "style='color:#5d7ea3'>Wallet didn't open? Tap here.</a></p>",
        )
    except Exception as e:  # noqa: BLE001
        st.update(status="error", err="could not link wallet")
        _tg_put(sid, st)
        _tg_log(sid, "connect FAILED", reason=type(e).__name__)
        return _tg_page("Couldn't link wallet", "<p>Please reopen the app and try again.</p>", err=True)


@app.get("/api/tg/sign/{sid}")
async def api_tg_sign(
    sid: str,
    request: Request,
    data: str | None = None,
    nonce: str | None = None,
    errorCode: str | None = None,
    errorMessage: str | None = None,
):
    if not _SID_RE.match(sid):
        _tg_log(sid, "sign REJECT", reason="bad_sid")
        return _tg_page("Invalid link", "<p>Reopen the app and connect again.</p>", err=True)
    st = _tg_get(sid)
    if not st or st.get("status") not in ("connected", "done"):
        _tg_log(sid, "sign REJECT", reason="no_session")
        return _tg_page("Session expired", "<p>Reopen the app and connect again.</p>", err=True)
    if errorCode:
        st.update(status="error", err=errorMessage or "wallet error")
        _tg_put(sid, st)
        _tg_log(sid, "sign CANCELLED", code=errorCode)
        return _tg_page("Signature cancelled", _tg_open_app_button(request), err=True)
    if st["status"] == "done":  # idempotent — Phantom re-delivered the callback
        return _tg_page("🧤 Signed!", _tg_open_app_button(request))
    try:
        sk = PrivateKey(base58.b58decode(st["sk"]))
        their = PublicKey(base58.b58decode(st["their"]))
        info = json.loads(Box(sk, their).decrypt(base58.b58decode(data), base58.b58decode(nonce)))
        signature = info["signature"]
        wallet = st["wallet"]
        msg = auth.login_message(wallet, st["nonce"])
        if not auth.consume_nonce(wallet, st["nonce"]):
            raise ValueError("nonce expired")
        if not auth.verify_wallet_signature(wallet, msg, signature):
            raise ValueError("bad signature")
        token, _ = await _complete_login(wallet, st["tg"], st.get("ref"), st.get("username"))
        st.update(status="done", token=token)
        _tg_put(sid, st)
        _tg_log(sid, "sign OK -> session minted", wallet=wallet[:6] + "…")
        # so a fully cold-relaunched webview (empty localStorage) can still recover
        store.get_store().setex("tglast:" + str(st["tg"]), _TG_TTL, sid)
        return _tg_page("🧤 Signed!", _tg_open_app_button(request))
    except HTTPException as e:
        st.update(status="error", err=str(e.detail))
        _tg_put(sid, st)
        _tg_log(sid, "sign FAILED", reason=str(e.detail)[:60])
        import html as _html

        return _tg_page("Sign-in problem", f"<p>{_html.escape(str(e.detail))}</p>", err=True)
    except Exception as e:  # noqa: BLE001
        st.update(status="error", err="signature failed")
        _tg_put(sid, st)
        _tg_log(sid, "sign FAILED", reason=type(e).__name__)
        return _tg_page("Signature failed", "<p>Please reopen the app and try again.</p>", err=True)


@app.post("/api/tg/poll")
def api_tg_poll(body: TgPoll, request: Request):
    ratelimit.hit(request, "tg")
    tg = auth.verify_init_data(body.initData)
    if not tg:
        raise HTTPException(401, "bad Telegram initData")
    try:
        tg_id = int(tg["id"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(401, "bad Telegram user")
    sid = body.sid
    if not sid:  # recover after a localStorage wipe via the per-user pointer
        sid = store.get_store().get("tglast:" + str(tg_id))
    if not sid or not _SID_RE.match(sid):
        return {"status": "pending"}
    st = _tg_get(sid)
    if not st or st.get("tg") != tg_id:  # never reveal another user's handshake
        return {"status": "pending"}
    if st["status"] == "done":
        with db.db() as conn:
            prof = _profile(conn, st["wallet"])
        return {"status": "done", "token": st["token"], "profile": prof}
    if st["status"] == "error":
        return {"status": "error", "detail": st.get("err", "sign-in failed")}
    return {"status": st["status"]}


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


# --------------------------------------------------------------------------- #
#  NO STAINS BRIDGE — white-label EasyBit (API mode)                            #
#  The API key lives only here; the browser talks to these endpoints, never to #
#  EasyBit directly. All bridge endpoints are public (no login) but IP rate-    #
#  limited, so a swap works on the website without connecting a Solana wallet.  #
#  When EASYBIT_API_KEY is unset these 503, and the UI uses the link fallback.  #
# --------------------------------------------------------------------------- #
class BridgeQuoteBody(BaseModel):
    send: str
    receive: str
    amount: str | float | int
    sendNetwork: str | None = None
    receiveNetwork: str | None = None


class BridgeValidateBody(BaseModel):
    currency: str
    address: str
    network: str | None = None


class BridgeOrderBody(BaseModel):
    send: str
    receive: str
    amount: str | float | int
    receiveAddress: str
    sendNetwork: str | None = None
    receiveNetwork: str | None = None
    receiveTag: str | None = None
    refundAddress: str | None = None
    refundTag: str | None = None


def _bridge_err(e: Exception) -> HTTPException:
    """Map our typed bridge/exchange errors to a clean HTTP error. Never leak
    internals — only the user-facing message and a sane status escape."""
    if isinstance(e, bridge.BridgeError):
        return HTTPException(e.status, e.message)
    if isinstance(e, easybit.EasyBitError):
        return HTTPException(e.status, e.message)
    return HTTPException(502, "the bridge is temporarily unavailable")


def _bridge_ready():
    if not easybit.enabled():
        raise HTTPException(503, "bridge is not configured")
    with db.db() as conn:
        _assert_ops_open(conn, "halt_bridge")


def _trim_currency(c: dict) -> dict:
    sym = easybit.pick(c, "currency", "coin", "symbol", "ticker", default="")
    nets = []
    for n in (c.get("networkList") or c.get("networks") or []):
        if isinstance(n, dict):
            net = easybit.pick(n, "network", "id", "name", default="")
            if net:
                nets.append({"network": net, "name": easybit.pick(n, "name", default=net)})
        elif isinstance(n, str):
            nets.append({"network": n, "name": n})
    return {"coin": sym, "name": easybit.pick(c, "name", default=sym), "networks": nets}


@app.get("/api/bridge/currencies")
async def api_bridge_currencies(request: Request):
    ratelimit.hit(request, "bridge")
    _bridge_ready()
    try:
        raw = await easybit.currency_list()
    except easybit.EasyBitError as e:
        raise _bridge_err(e)
    out = [_trim_currency(c) for c in raw if isinstance(c, dict)]
    out = [c for c in out if c["coin"]]
    return JSONResponse({"currencies": out}, headers={"Cache-Control": "public, max-age=300"})


@app.post("/api/bridge/quote")
async def api_bridge_quote(body: BridgeQuoteBody, request: Request):
    ratelimit.hit(request, "bridge")
    _bridge_ready()
    try:
        return await bridge.quote(
            body.send, body.receive, body.amount,
            send_network=body.sendNetwork or "", receive_network=body.receiveNetwork or "",
        )
    except (bridge.BridgeError, easybit.EasyBitError) as e:
        raise _bridge_err(e)


@app.post("/api/bridge/validate-address")
async def api_bridge_validate(body: BridgeValidateBody, request: Request):
    ratelimit.hit(request, "bridge")
    _bridge_ready()
    try:
        coin = bridge.norm_coin(body.currency)
        net = bridge.norm_network(body.network or "")
        addr = bridge.norm_address(body.address)
    except bridge.BridgeError:
        return {"valid": False}
    try:
        ok = await easybit.validate_address(coin, addr, net)
    except easybit.EasyBitError as e:
        raise _bridge_err(e)
    return {"valid": bool(ok)}


@app.post("/api/bridge/order")
async def api_bridge_order(body: BridgeOrderBody, request: Request):
    ratelimit.hit(request, "bridge_order")
    _bridge_ready()
    try:
        return await bridge.create(
            send=body.send, receive=body.receive, amount=body.amount,
            receive_address=body.receiveAddress,
            send_network=body.sendNetwork or "", receive_network=body.receiveNetwork or "",
            receive_tag=body.receiveTag, refund_address=body.refundAddress,
            refund_tag=body.refundTag, client_ip=ratelimit.client_ip(request),
        )
    except (bridge.BridgeError, easybit.EasyBitError) as e:
        raise _bridge_err(e)


@app.get("/api/bridge/order/{order_id}")
async def api_bridge_order_status(order_id: str, request: Request):
    ratelimit.hit(request, "bridge_status")
    _bridge_ready()
    try:
        return await bridge.status_of_order(order_id)
    except (bridge.BridgeError, easybit.EasyBitError) as e:
        raise _bridge_err(e)


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


@app.get("/readyz")
async def readyz():
    """Dependency-aware readiness for deploy/canary gates.

    /healthz is liveness. /readyz answers "should traffic be routed here?" and
    checks the dependencies that matter before a 10k-user launch.
    """
    db_ok = False
    flags = {}
    pending_claims = {"count": 0, "oldest_age_seconds": 0}
    try:
        with db.db() as conn:
            conn.execute("SELECT 1").fetchone()
            db_ok = True
            flags = {r["key"]: r["value"] for r in db.list_flags(conn)}
            row = conn.execute(
                "SELECT COUNT(*) AS n, MIN(created_at) AS oldest "
                "FROM claims WHERE status='requested'"
            ).fetchone()
            now = int(time.time())
            pending_claims = {
                "count": int(row["n"] or 0),
                "oldest_age_seconds": int(now - row["oldest"]) if row["oldest"] else 0,
            }
    except Exception:  # noqa: BLE001
        db_ok = False

    store_ok = store.healthy()
    check_rpc = os.environ.get("STAKE_READYZ_CHECK_RPC", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    rpc_ok = await solana.rpc_health() if check_rpc else None
    halted = flags.get("halt_all", "").lower() in ("1", "true", "yes", "on")
    ready = db_ok and store_ok and CONFIG_STATUS.get("ok", True) and not halted
    if check_rpc:
        ready = ready and bool(rpc_ok)
    out = {
        "ok": ready,
        "db": db_ok,
        "store": {"backend": store.backend_name(), "ok": store_ok},
        "config": CONFIG_STATUS,
        "rpc": {"checked": check_rpc, "ok": rpc_ok},
        "ops_flags": flags,
        "pending_claims": pending_claims,
    }
    return JSONResponse(out, status_code=200 if ready else 503)


# --------------------------------------------------------------------------- #
#  STATIC FRONTEND (the Mini App, served same-origin → no CORS for the app)    #
# --------------------------------------------------------------------------- #
_WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")
# Without Cache-Control, webviews use HEURISTIC caching (reuse without
# revalidating, for ~10% of the file's age) — Telegram's webview kept serving
# STALE app.js/wallet.js across deploys, so client fixes never reached phones.
# no-cache forces an ETag revalidation on every load: 304 when unchanged,
# fresh bytes the moment a deploy lands.
_NO_CACHE = {"Cache-Control": "no-cache, max-age=0, must-revalidate"}
_DAY_CACHE = {"Cache-Control": "public, max-age=86400"}


@app.get("/")
def index():
    return FileResponse(os.path.join(_WEB, "index.html"), headers=_NO_CACHE)


@app.get("/app.js")
def app_js():
    return FileResponse(
        os.path.join(_WEB, "app.js"), media_type="application/javascript", headers=_NO_CACHE
    )


@app.get("/play")
def play():
    # $CLEAN tap game — a single self-contained HTML file (all CSS/JS/audio/art
    # inlined as data: URIs), so no extra asset routes are needed. Served here so
    # it ships with the same deploy as the Mini App; reachable at /play.
    return FileResponse(os.path.join(_WEB, "play.html"), headers=_NO_CACHE)


@app.get("/whitepaper")
def whitepaper():
    return FileResponse(os.path.join(_WEB, "whitepaper.html"), headers=_NO_CACHE)


@app.get("/whitepaper.html")
def whitepaper_html():
    return whitepaper()


@app.get("/whitepaper.js")
def whitepaper_js():
    # The whitepaper's ambience script lives in its own file because the CSP
    # has no 'unsafe-inline' for scripts — an inline block would be blocked.
    return FileResponse(
        os.path.join(_WEB, "whitepaper.js"), media_type="application/javascript", headers=_NO_CACHE
    )


@app.get("/wallet.js")
def wallet_js():
    return FileResponse(
        os.path.join(_WEB, "wallet.js"), media_type="application/javascript", headers=_NO_CACHE
    )


@app.get("/nacl.min.js")
def nacl_js():
    return FileResponse(
        os.path.join(_WEB, "nacl.min.js"), media_type="application/javascript", headers=_NO_CACHE
    )


@app.get("/wallet-return")
def wallet_return():
    return FileResponse(os.path.join(_WEB, "return.html"), headers=_NO_CACHE)


@app.get("/glove.png")
def glove_png():
    for path in (
        os.path.join(_WEB, "glove.png"),
        os.path.join(os.path.dirname(_WEB), "..", "assets", "glove.png"),
    ):
        if os.path.isfile(path):
            return FileResponse(path, headers=_DAY_CACHE)
    raise HTTPException(404, "asset missing")


@app.get("/banner.png")
def banner_png():
    for path in (
        os.path.join(_WEB, "banner.png"),
        os.path.join(os.path.dirname(_WEB), "..", "assets", "banner.png"),
    ):
        if os.path.isfile(path):
            return FileResponse(path, headers=_DAY_CACHE)
    raise HTTPException(404, "asset missing")


@app.get("/scenes/{filename}")
def scene_img(filename: str):
    """Serve level backdrop images from webapp/scenes/.
    Only single-segment filenames are accepted (FastAPI rejects '/' in path
    params); the '..' guard blocks the only remaining traversal vector."""
    if ".." in filename:
        raise HTTPException(400, "invalid filename")
    path = os.path.join(_WEB, "scenes", filename)
    if not os.path.isfile(path):
        raise HTTPException(404)
    return FileResponse(path, headers=_DAY_CACHE)


@app.get("/audio/{filename}")
def audio_file(filename: str):
    """Serve game music/SFX from webapp/audio/ (e.g. the per-scene track)."""
    if ".." in filename:
        raise HTTPException(400, "invalid filename")
    path = os.path.join(_WEB, "audio", filename)
    if not os.path.isfile(path):
        raise HTTPException(404)
    return FileResponse(path, media_type="audio/mpeg", headers=_DAY_CACHE)


# --------------------------------------------------------------------------- #
#  GLOVE-CODE LANDING (/g/<code>) — the shareable invite link                   #
#  Unfurls with a branded OG card in Telegram/X/Discord and funnels the tap     #
#  into the Mini App with the code as start_param.                              #
# --------------------------------------------------------------------------- #
_CODE_RE = re.compile(r"^[A-Za-z0-9]{4,12}$")


@app.get("/g/{code}")
def glove_link(code: str, request: Request):
    if not _CODE_RE.match(code):
        raise HTTPException(404, "unknown glove code")
    code = code.upper()
    with db.db() as conn:
        if not db.wallet_by_ref_code(conn, code):
            raise HTTPException(404, "unknown glove code")
    origin = _origin(request)
    bot = os.environ.get("MINIAPP_BOT_USERNAME", "").lstrip("@")
    short = os.environ.get("MINIAPP_SHORT_NAME", "app")
    tme = f"https://t.me/{bot}/{short}?startapp={code}" if bot else origin
    title = "CLEAN HANDS DIRTY MONEY"
    desc = "Play, stake & boost yield with my referral. Clean hands, dirty money. 🧤"
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{title}</title>"
        f"<meta property='og:title' content='{title}'>"
        f"<meta property='og:description' content='{desc}'>"
        f"<meta property='og:image' content='{origin}/banner.png'>"
        f"<meta property='og:url' content='{origin}/g/{code}'>"
        "<meta property='og:type' content='website'>"
        "<meta name='twitter:card' content='summary_large_image'>"
        f"<meta name='twitter:image' content='{origin}/banner.png'>"
        "<style>"
        "body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "background:#F4FAFF;color:#16385c;display:flex;align-items:center;justify-content:center;"
        "min-height:100vh;text-align:center;padding:24px}"
        ".c{background:#fff;border:1.5px solid rgba(27,93,166,.16);border-radius:22px;"
        "box-shadow:0 20px 50px -28px rgba(27,93,166,.4);padding:34px 26px;max-width:380px}"
        "img{width:84px;height:84px;object-fit:contain;margin-bottom:10px}"
        "h1{font-size:1.35rem;color:#0F3E73;margin:0 0 6px}"
        ".code{font-family:ui-monospace,Menlo,monospace;font-size:1.6rem;font-weight:700;"
        "color:#2E74C0;letter-spacing:.18em;background:#EAF4FE;border:1.5px solid rgba(27,93,166,.28);"
        "border-radius:12px;padding:10px 16px;display:inline-block;margin:8px 0 14px}"
        "p{color:#5d7ea3;font-size:.95rem;line-height:1.5;margin:0 0 18px}"
        "a.btn{display:block;background:#2E74C0;color:#fff;text-decoration:none;font-weight:700;"
        "border-radius:14px;padding:15px;box-shadow:0 12px 24px -12px rgba(46,116,192,.8)}"
        "</style></head><body><div class='c'>"
        "<img src='/glove.png?v=2' alt='$CLEAN'>"
        "<h1>CLEAN HANDS DIRTY MONEY</h1>"
        f"<div class='code'>{code}</div>"
        f"<p>{desc}<br>Connect, stake, and you BOTH get the referral boost.</p>"
        f"<a class='btn' href='{tme}'>🧤 Open in Telegram</a>"
        "</div></body></html>"
    )
    return HTMLResponse(html)


if __name__ == "__main__":
    import uvicorn

    # Bind localhost by default — the API sits behind nginx/Caddy (see DNS.md:
    # "never expose 8090 publicly"). Set STAKE_HOST=0.0.0.0 only to deliberately
    # expose it (and firewall the port yourself).
    uvicorn.run(
        app,
        host=os.environ.get("STAKE_HOST", "127.0.0.1"),
        port=int(os.environ.get("STAKE_PORT", "8090")),
    )
