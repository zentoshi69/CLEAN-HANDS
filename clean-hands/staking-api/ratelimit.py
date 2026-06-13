#!/usr/bin/env python3
"""
ratelimit.py — fixed-window rate limiting backed by the shared store (memory or
Redis), so limits hold across workers when REDIS_URL is set. Raises HTTP 429.

Limits are env-tunable; defaults are conservative for a 100k-user launch.
"""

from __future__ import annotations

import os
from fastapi import HTTPException, Request

import store


def _int(env: str, default: int) -> int:
    try:
        return int(os.environ.get(env, default))
    except (TypeError, ValueError):
        return default


# (limit, window_seconds) per named bucket.
LIMITS = {
    "nonce": (_int("RL_NONCE", 30), 60),
    "login": (_int("RL_LOGIN", 20), 60),
    "burn": (_int("RL_BURN", 10), 60),
    "write": (_int("RL_WRITE", 60), 60),  # stake/unstake/claim
    "relay": (_int("RL_RELAY", 240), 60),  # wallet-callback handoff polling
    "tg": (_int("RL_TG", 240), 60),  # Telegram server-side handshake start/poll
    "bridge": (_int("RL_BRIDGE", 90), 60),  # bridge quotes / currency list / validate
    "bridge_order": (_int("RL_BRIDGE_ORDER", 12), 60),  # opening exchange orders (strict)
    "bridge_status": (_int("RL_BRIDGE_STATUS", 240), 60),  # order-status polling
}


def client_ip(request: Request) -> str:
    # Trust the edge proxy's X-Forwarded-For (left-most = original client).
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def hit(request: Request, bucket: str, *, extra_key: str | None = None) -> None:
    """Count one request against `bucket` for this IP (and optionally a second
    key like a wallet). Raise 429 if either exceeds its limit."""
    if os.environ.get("RL_DISABLE", "").strip() in ("1", "true", "yes"):
        return
    limit, window = LIMITS.get(bucket, LIMITS["write"])
    s = store.get_store()
    keys = [f"rl:{bucket}:ip:{client_ip(request)}"]
    if extra_key:
        keys.append(f"rl:{bucket}:k:{extra_key}")
    for k in keys:
        if s.incr_window(k, window) > limit:
            raise HTTPException(429, "rate limit exceeded — slow down")
