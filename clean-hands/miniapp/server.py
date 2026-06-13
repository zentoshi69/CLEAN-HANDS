#!/usr/bin/env python3
"""
CLEAN Mini App — backend (FastAPI + SQLite).

A self-contained Telegram Mini App server: points economy, staking-for-yield,
referrals, and a leaderboard. Serves the frontend (index.html) AND the JSON API
from one origin, so there's no CORS to configure.

SECURITY: every API call must include Telegram `initData`; we verify its HMAC
signature with the bot token, so a user can only ever act as themselves and
points cannot be spoofed. Never trust a tg_id that didn't come from a valid
signature.

Run:
    pip install -r requirements.txt
    export TG_COMMUNITY_TOKEN="123456:ABC..."   # SAME bot that owns the mini app
    export MINIAPP_DB="clean.db"                  # optional, defaults to ./clean.db
    python server.py                              # serves on :8080

Behind HTTPS: a Mini App MUST be served over HTTPS. Put this behind Caddy/nginx
with a real cert, or expose it with `cloudflared tunnel`. See README.md.
"""

import os
import json
import time
import hmac
import hashlib
import sqlite3
from urllib.parse import parse_qsl
from contextlib import closing

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# --------------------------------------------------------------------------- #
#  CONFIG — tune the economy here                                             #
# --------------------------------------------------------------------------- #
BOT_TOKEN = os.environ.get("TG_COMMUNITY_TOKEN", "")
DB_PATH = os.environ.get("MINIAPP_DB", os.path.join(os.path.dirname(__file__), "clean.db"))
PORT = int(os.environ.get("MINIAPP_PORT", "8080"))

BOT_USERNAME = os.environ.get("MINIAPP_BOT_USERNAME", "").lstrip("@")  # for referral links
APP_SHORT_NAME = os.environ.get("MINIAPP_SHORT_NAME", "app")          # the /newapp short name
DAILY_CLAIM = int(os.environ.get("MINIAPP_DAILY_CLAIM", "100"))      # points per daily claim
CLAIM_COOLDOWN = int(os.environ.get("MINIAPP_CLAIM_COOLDOWN", "86400"))  # seconds
WELCOME_BONUS = int(os.environ.get("MINIAPP_WELCOME_BONUS", "200"))  # points on first open
REF_REFERRER = int(os.environ.get("MINIAPP_REF_REFERRER", "500"))    # to the inviter
REF_REFEREE = int(os.environ.get("MINIAPP_REF_REFEREE", "250"))      # to the invited
INITDATA_TTL = int(os.environ.get("MINIAPP_INITDATA_TTL", "86400"))  # reject older initData

# Staking tiers: lock points for N days, get them back + yield (total return %).
# Kept simple & off-chain — these are app points, not on-chain tokens.
STAKE_TIERS = {
    7: 0.05,    # 7 days  -> +5%
    30: 0.25,   # 30 days -> +25%
    90: 1.00,   # 90 days -> +100%
}

app = FastAPI(title="CLEAN Mini App")
HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------- #
#  DB                                                                          #
# --------------------------------------------------------------------------- #
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with closing(db()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                tg_id       INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                points      INTEGER NOT NULL DEFAULT 0,
                referred_by INTEGER,
                ref_count   INTEGER NOT NULL DEFAULT 0,
                last_claim  INTEGER NOT NULL DEFAULT 0,
                created_at  INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stakes (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id     INTEGER NOT NULL,
                amount    INTEGER NOT NULL,
                rate      REAL NOT NULL,
                days      INTEGER NOT NULL,
                start_ts  INTEGER NOT NULL,
                end_ts    INTEGER NOT NULL,
                claimed   INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_stakes_user ON stakes(tg_id);
            CREATE INDEX IF NOT EXISTS idx_users_points ON users(points DESC);
            """
        )
        conn.commit()


# --------------------------------------------------------------------------- #
#  TELEGRAM initData VERIFICATION (the security boundary)                      #
# --------------------------------------------------------------------------- #
def verify_init_data(init_data: str) -> dict:
    """Validate Telegram WebApp initData and return the parsed `user` dict.
    Raises HTTPException(401) on any failure."""
    if not BOT_TOKEN:
        raise HTTPException(500, "server missing TG_COMMUNITY_TOKEN")
    if not init_data:
        raise HTTPException(401, "missing initData")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    their_hash = pairs.pop("hash", None)
    if not their_hash:
        raise HTTPException(401, "no hash in initData")

    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, their_hash):
        raise HTTPException(401, "bad initData signature")

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except (TypeError, ValueError):
        raise HTTPException(401, "bad initData")
    if INITDATA_TTL and (time.time() - auth_date) > INITDATA_TTL:
        raise HTTPException(401, "initData expired")

    user_raw = pairs.get("user")
    if not user_raw:
        raise HTTPException(401, "no user in initData")
    try:
        user = json.loads(user_raw)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(401, "bad user in initData")
    if not isinstance(user, dict) or "id" not in user:
        raise HTTPException(401, "bad user in initData")
    user["_start_param"] = pairs.get("start_param", "")
    return user


# --------------------------------------------------------------------------- #
#  CORE LOGIC                                                                  #
# --------------------------------------------------------------------------- #
def get_or_create_user(conn, user: dict) -> sqlite3.Row:
    tg_id = int(user["id"])
    row = conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    if row:
        # keep username fresh
        conn.execute(
            "UPDATE users SET username=?, first_name=? WHERE tg_id=?",
            (user.get("username"), user.get("first_name"), tg_id),
        )
        return row

    # New user: welcome bonus + referral credit.
    referred_by = None
    sp = (user.get("_start_param") or "").strip()
    if sp.isdigit() and int(sp) != tg_id:
        ref = conn.execute("SELECT tg_id FROM users WHERE tg_id=?", (int(sp),)).fetchone()
        if ref:
            referred_by = int(sp)

    now = int(time.time())
    starting = WELCOME_BONUS + (REF_REFEREE if referred_by else 0)
    conn.execute(
        "INSERT INTO users (tg_id, username, first_name, points, referred_by, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (tg_id, user.get("username"), user.get("first_name"), starting, referred_by, now),
    )
    if referred_by:
        conn.execute(
            "UPDATE users SET points = points + ?, ref_count = ref_count + 1 WHERE tg_id=?",
            (REF_REFERRER, referred_by),
        )
    conn.commit()
    return conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()


def profile_payload(conn, tg_id: int) -> dict:
    u = conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    rank = conn.execute(
        "SELECT COUNT(*)+1 AS r FROM users WHERE points > (SELECT points FROM users WHERE tg_id=?)",
        (tg_id,),
    ).fetchone()["r"]
    staked = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS s FROM stakes WHERE tg_id=? AND claimed=0",
        (tg_id,),
    ).fetchone()["s"]
    now = int(time.time())
    next_claim_in = max(0, (u["last_claim"] + CLAIM_COOLDOWN) - now)
    return {
        "tg_id": u["tg_id"],
        "username": u["username"],
        "first_name": u["first_name"],
        "points": u["points"],
        "staked": staked,
        "rank": rank,
        "ref_count": u["ref_count"],
        "can_claim": next_claim_in == 0,
        "next_claim_in": next_claim_in,
        "daily_claim": DAILY_CLAIM,
    }


# --------------------------------------------------------------------------- #
#  API                                                                         #
# --------------------------------------------------------------------------- #
class AuthBody(BaseModel):
    initData: str


class StakeBody(BaseModel):
    initData: str
    amount: int
    days: int


class IdBody(BaseModel):
    initData: str
    id: int | None = None


@app.post("/api/auth")
def api_auth(body: AuthBody):
    user = verify_init_data(body.initData)
    with closing(db()) as conn:
        get_or_create_user(conn, user)
        return profile_payload(conn, int(user["id"]))


@app.post("/api/claim")
def api_claim(body: AuthBody):
    user = verify_init_data(body.initData)
    tg_id = int(user["id"])
    now = int(time.time())
    with closing(db()) as conn:
        get_or_create_user(conn, user)
        u = conn.execute("SELECT last_claim FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        if (now - u["last_claim"]) < CLAIM_COOLDOWN:
            raise HTTPException(429, "claim on cooldown")
        conn.execute(
            "UPDATE users SET points = points + ?, last_claim = ? WHERE tg_id=?",
            (DAILY_CLAIM, now, tg_id),
        )
        conn.commit()
        return {"claimed": DAILY_CLAIM, **profile_payload(conn, tg_id)}


@app.post("/api/stake")
def api_stake(body: StakeBody):
    user = verify_init_data(body.initData)
    tg_id = int(user["id"])
    if body.days not in STAKE_TIERS:
        raise HTTPException(400, f"invalid lock period; choose {sorted(STAKE_TIERS)}")
    if body.amount <= 0:
        raise HTTPException(400, "amount must be positive")
    now = int(time.time())
    with closing(db()) as conn:
        get_or_create_user(conn, user)
        u = conn.execute("SELECT points FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        if u["points"] < body.amount:
            raise HTTPException(400, "not enough points")
        rate = STAKE_TIERS[body.days]
        conn.execute("UPDATE users SET points = points - ? WHERE tg_id=?", (body.amount, tg_id))
        conn.execute(
            "INSERT INTO stakes (tg_id, amount, rate, days, start_ts, end_ts) VALUES (?,?,?,?,?,?)",
            (tg_id, body.amount, rate, body.days, now, now + body.days * 86400),
        )
        conn.commit()
        return {"ok": True, **profile_payload(conn, tg_id)}


@app.post("/api/stakes")
def api_stakes(body: AuthBody):
    user = verify_init_data(body.initData)
    tg_id = int(user["id"])
    now = int(time.time())
    with closing(db()) as conn:
        get_or_create_user(conn, user)
        rows = conn.execute(
            "SELECT * FROM stakes WHERE tg_id=? AND claimed=0 ORDER BY end_ts", (tg_id,)
        ).fetchall()
        out = []
        for r in rows:
            payout = int(r["amount"] * (1 + r["rate"]))
            out.append(
                {
                    "id": r["id"],
                    "amount": r["amount"],
                    "rate": r["rate"],
                    "days": r["days"],
                    "payout": payout,
                    "matured": now >= r["end_ts"],
                    "ends_in": max(0, r["end_ts"] - now),
                }
            )
        return {"stakes": out}


@app.post("/api/unstake")
def api_unstake(body: IdBody):
    user = verify_init_data(body.initData)
    tg_id = int(user["id"])
    now = int(time.time())
    with closing(db()) as conn:
        get_or_create_user(conn, user)
        r = conn.execute(
            "SELECT * FROM stakes WHERE id=? AND tg_id=? AND claimed=0", (body.id, tg_id)
        ).fetchone()
        if not r:
            raise HTTPException(404, "stake not found")
        if now < r["end_ts"]:
            raise HTTPException(400, "stake not matured yet")
        payout = int(r["amount"] * (1 + r["rate"]))
        conn.execute("UPDATE stakes SET claimed=1 WHERE id=?", (r["id"],))
        conn.execute("UPDATE users SET points = points + ? WHERE tg_id=?", (payout, tg_id))
        conn.commit()
        return {"payout": payout, **profile_payload(conn, tg_id)}


@app.post("/api/leaderboard")
def api_leaderboard(body: AuthBody):
    user = verify_init_data(body.initData)
    tg_id = int(user["id"])
    with closing(db()) as conn:
        get_or_create_user(conn, user)
        rows = conn.execute(
            "SELECT tg_id, username, first_name, points FROM users ORDER BY points DESC LIMIT 50"
        ).fetchall()
        top = [
            {
                "rank": i + 1,
                "name": r["username"] or r["first_name"] or f"user{r['tg_id']}",
                "points": r["points"],
                "me": r["tg_id"] == tg_id,
            }
            for i, r in enumerate(rows)
        ]
        return {"leaderboard": top, **profile_payload(conn, tg_id)}


@app.post("/api/referrals")
def api_referrals(body: AuthBody):
    user = verify_init_data(body.initData)
    tg_id = int(user["id"])
    with closing(db()) as conn:
        get_or_create_user(conn, user)
        u = conn.execute("SELECT ref_count FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        return {
            "ref_count": u["ref_count"],
            "ref_code": str(tg_id),
            "reward_each": REF_REFERRER,
        }


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "index.html"))


@app.get("/glove.png")
def glove_png():
    return FileResponse(os.path.join(HERE, "..", "assets", "glove.png"))


@app.get("/config.js")
def config_js():
    # expose only non-secret economy config to the frontend
    cfg = {
        "tiers": [{"days": d, "rate": r} for d, r in sorted(STAKE_TIERS.items())],
        "dailyClaim": DAILY_CLAIM,
        "refReward": REF_REFERRER,
        "botUsername": BOT_USERNAME,
        "appShortName": APP_SHORT_NAME,
    }
    return JSONResponse(
        content=cfg,
        headers={"Cache-Control": "no-store"},
        media_type="application/json",
    )


init_db()

if __name__ == "__main__":
    import uvicorn

    if not BOT_TOKEN:
        print("WARNING: TG_COMMUNITY_TOKEN not set — initData verification will fail.")
    # Bind localhost by default — this process sits behind nginx/Caddy. Set
    # MINIAPP_HOST=0.0.0.0 only when deliberately exposing it directly.
    uvicorn.run(app, host=os.environ.get("MINIAPP_HOST", "127.0.0.1"), port=PORT)
