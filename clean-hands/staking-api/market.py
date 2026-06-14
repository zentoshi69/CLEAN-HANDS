#!/usr/bin/env python3
"""
market.py — $CLEAN market data from DexScreener (public, no key), with a short
TTL cache so the app and alerts can read price/mcap/volume cheaply at scale.
"""

from __future__ import annotations

import os
import re
import time
import httpx

DEXS_TOKENS = "https://api.dexscreener.com/latest/dex/tokens"
DEXS_PAIRS = "https://api.dexscreener.com/latest/dex/pairs/solana"

# A base58 Solana address. Used to reject the .env.example placeholder
# ("# the $CLEAN mint — your pump.fun CA …") and any other non-address value, so
# a not-yet-filled .env can't leak that text into the UI or break the price.
_ADDR_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _addr_or(default: str, raw: str | None) -> str:
    """Return the env value only if it's a real address; otherwise the default."""
    raw = (raw or "").strip()
    return raw if _ADDR_RE.match(raw) else default


# Built-in $CLEAN mint so the app shows live price/links out-of-the-box; a valid
# address in DEFAULT_TOKEN_MINT still overrides it.
MINT = _addr_or("6jb4XWggYJjoo3fx7irPVxhNiuFbHUyVyKR8mBL8pump", os.environ.get("DEFAULT_TOKEN_MINT"))
# Optional: pin price to a specific pool. Empty default => auto-pick the
# highest-liquidity pool for MINT (robust to pair-address casing).
PAIR = _addr_or("", os.environ.get("DEFAULT_TOKEN_PAIR"))
TTL = int(os.environ.get("MARKET_TTL", "60"))
_cache: dict[str, tuple[float, dict | None]] = {}
# Last-known CLEAN/SOL USD prices, so synchronous code (the staking accrual path)
# can read a price without doing network I/O. Warmed by refresh_prices().
_last_prices: dict[str, float] = {"clean_usd": 0.0, "sol_usd": 0.0, "ts": 0.0}


async def _get(url: str):
    async with httpx.AsyncClient(timeout=15) as c:
        return await c.get(url, headers={"User-Agent": "clean-market/1.0"})


async def best_pair(mint: str | None = None) -> dict | None:
    """The $CLEAN pool, cached. Uses DEFAULT_TOKEN_PAIR if set (exact pool),
    else the highest-liquidity pair for the mint. None if no data."""
    use_pair = PAIR if (PAIR and not mint) else ""
    mint = (mint or MINT).strip()
    key = f"pair:{use_pair}" if use_pair else mint
    if not key:
        return None
    now = time.time()
    hit = _cache.get(key)
    if hit and (now - hit[0]) < TTL:
        return hit[1]
    try:
        if use_pair:
            r = await _get(f"{DEXS_PAIRS}/{use_pair}")
            data = r.json() if r.status_code == 200 else {}
            pairs = data.get("pairs") or ([data["pair"]] if data.get("pair") else [])
            best = pairs[0] if pairs else None
        else:
            r = await _get(f"{DEXS_TOKENS}/{mint}")
            pairs = (r.json() or {}).get("pairs") or [] if r.status_code == 200 else []
            best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0) if pairs else None
        _cache[key] = (now, best)
        return best
    except Exception:  # noqa: BLE001 — keep last good value on a hiccup
        return hit[1] if hit else None


def summary(p: dict | None) -> dict | None:
    """Flatten a DexScreener pair to the fields the UI/alerts need."""
    if not p:
        return None
    base = p.get("baseToken") or {}
    ch = p.get("priceChange") or {}
    try:
        price = float(p.get("priceUsd") or 0)
    except (TypeError, ValueError):
        price = 0.0
    return {
        "symbol": base.get("symbol") or "?",
        "name": base.get("name") or "",
        "price_usd": price,
        "change_24h": ch.get("h24"),
        "market_cap": p.get("marketCap"),
        "fdv": p.get("fdv"),
        "liquidity_usd": (p.get("liquidity") or {}).get("usd"),
        "volume_24h": (p.get("volume") or {}).get("h24"),
        "url": p.get("url"),
        "pair_address": p.get("pairAddress"),
    }


def _sol_usd(p: dict | None) -> float:
    """USD per SOL, derived for free from the pair: when the quote token is SOL,
    priceUsd / priceNative = USD per SOL. 0.0 if it can't be derived."""
    if not p:
        return 0.0
    quote = ((p.get("quoteToken") or {}).get("symbol") or "").upper()
    if quote not in ("SOL", "WSOL"):
        return 0.0
    try:
        pu = float(p.get("priceUsd") or 0)
        pn = float(p.get("priceNative") or 0)
    except (TypeError, ValueError):
        return 0.0
    return pu / pn if (pu > 0 and pn > 0) else 0.0


async def refresh_prices() -> dict:
    """Refresh the cached CLEAN/SOL USD prices from DexScreener. Best-effort:
    keeps the last-known value for anything it can't read this round."""
    p = await best_pair()
    try:
        clean = float((p or {}).get("priceUsd") or 0)
    except (TypeError, ValueError):
        clean = 0.0
    sol = _sol_usd(p)
    if clean > 0:
        _last_prices["clean_usd"] = clean
    if sol > 0:
        _last_prices["sol_usd"] = sol
    if clean > 0 or sol > 0:
        _last_prices["ts"] = time.time()
    return dict(_last_prices)


def last_prices() -> dict:
    """Last-known CLEAN/SOL USD prices (synchronous, no network I/O)."""
    return dict(_last_prices)
