#!/usr/bin/env python3
"""
Auth for the CLEAN staking API.

Two proofs, combined:
  1. WALLET ownership — the user signs a login message with their Solana wallet
     (ed25519). This is the same login the website uses, so identity matches.
  2. TELEGRAM identity (Mini App only) — verify Telegram `initData` HMAC so we can
     bind telegram_id <-> wallet for the leaderboard / referrals.

After proof we issue a short-lived HMAC-signed session token; reads use it, and
state-changing calls re-check it. No third-party JWT dependency.
"""

from __future__ import annotations

import os
import json
import time
import hmac
import base64
import hashlib
import secrets
from urllib.parse import parse_qsl

import base58
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

SERVER_SECRET = os.environ.get("STAKE_SERVER_SECRET", "").encode() or secrets.token_bytes(32)
BOT_TOKEN = os.environ.get("TG_COMMUNITY_TOKEN", "")
SESSION_TTL = int(os.environ.get("STAKE_SESSION_TTL", "3600"))
INITDATA_TTL = int(os.environ.get("STAKE_INITDATA_TTL", "86400"))
LOGIN_PREFIX = "CLEAN soft-staking login"


# --------------------------------------------------------------------------- #
#  WALLET (Solana ed25519)                                                     #
# --------------------------------------------------------------------------- #
def login_message(wallet: str, nonce: str) -> str:
    """The exact string the wallet must sign. Human-readable on purpose."""
    return f"{LOGIN_PREFIX}\nwallet: {wallet}\nnonce: {nonce}"


def is_valid_wallet(wallet: str) -> bool:
    """A Solana address is base58 of a 32-byte ed25519 public key."""
    if not isinstance(wallet, str) or not (32 <= len(wallet) <= 44):
        return False  # bound length before any decode work
    try:
        return len(base58.b58decode(wallet)) == 32
    except Exception:  # noqa: BLE001
        return False


def verify_wallet_signature(wallet: str, message: str, signature_b58: str) -> bool:
    """True iff `signature_b58` is `wallet`'s ed25519 signature over `message`."""
    try:
        pubkey = base58.b58decode(wallet)
        if len(pubkey) != 32:
            return False
        sig = base58.b58decode(signature_b58)
        VerifyKey(pubkey).verify(message.encode(), sig)
        return True
    except (BadSignatureError, ValueError):
        return False
    except Exception:  # noqa: BLE001 — any decode error == invalid
        return False


# --------------------------------------------------------------------------- #
#  TELEGRAM initData                                                           #
# --------------------------------------------------------------------------- #
def verify_init_data(init_data: str) -> dict | None:
    """Return the parsed Telegram `user` dict if initData is authentic, else None."""
    if not init_data or not BOT_TOKEN:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    their_hash = pairs.pop("hash", None)
    if not their_hash:
        return None
    dcs = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret_key, dcs.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, their_hash):
        return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except (TypeError, ValueError):
        return None
    if INITDATA_TTL and (time.time() - auth_date) > INITDATA_TTL:
        return None
    try:
        user = json.loads(pairs.get("user", ""))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(user, dict) or "id" not in user:
        return None
    user["_start_param"] = pairs.get("start_param", "")
    return user


# --------------------------------------------------------------------------- #
#  SESSIONS (HMAC-signed, stateless)                                           #
# --------------------------------------------------------------------------- #
def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def create_session(wallet: str, tg_id: int | None = None) -> str:
    payload = {"w": wallet, "t": tg_id, "exp": int(time.time()) + SESSION_TTL}
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64e(hmac.new(SERVER_SECRET, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_session(token: str) -> dict | None:
    try:
        body, sig = token.split(".", 1)
    except (ValueError, AttributeError):
        return None
    expect = _b64e(hmac.new(SERVER_SECRET, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expect, sig):
        return None
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) < int(time.time()):
        return None
    return payload


# --------------------------------------------------------------------------- #
#  NONCE STORE (shared: memory by default, Redis when REDIS_URL is set)        #
# --------------------------------------------------------------------------- #
import store

NONCE_TTL = 300


def issue_nonce(wallet: str) -> str:
    nonce = secrets.token_urlsafe(16)
    store.get_store().setex(f"nonce:{wallet}", NONCE_TTL, nonce)
    return nonce


def consume_nonce(wallet: str, nonce: str) -> bool:
    saved = store.get_store().getdel(f"nonce:{wallet}")  # atomic single-use
    return bool(saved and hmac.compare_digest(saved, nonce))
