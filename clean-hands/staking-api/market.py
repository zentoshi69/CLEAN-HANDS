#!/usr/bin/env python3
"""
market.py — $CLEAN market data from DexScreener (public, no key), with a short
TTL cache so the app and alerts can read price/mcap/volume cheaply at scale.
"""

from __future__ import annotations

import os
import time
import httpx

DEXS_TOKENS = "https://api.dexscreener.com/latest/dex/tokens"
DEXS_PAIRS = "https://api.dexscreener.com/latest/dex/pairs/solana"
MINT = os.environ.get("DEFAULT_TOKEN_MINT", "").strip()
# Pin price to a specific pool (the DexScreener pair address) for exactness.
PAIR = os.environ.get("DEFAULT_TOKEN_PAIR", "").strip()
TTL = int(os.environ.get("MARKET_TTL", "60"))
_cache: dict[str, tuple[float, dict | None]] = {}


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
