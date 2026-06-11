#!/usr/bin/env python3
"""
clean-gm.py — daily "GM" post to the channel: $CLEAN price + a stake CTA.
Point OpenClaw Cron (or plain cron) at this once a day. Reads the PUBLIC price
endpoint, posts via the Bot API. No keys, no DB.

  CLEAN_API=https://app.cleanhands.fun TG_ALERTS_TOKEN=123:ABC TG_ALERTS_CHAT=@yourchan python clean-gm.py
  python clean-gm.py --dry-run     # print the message, don't post
"""
from __future__ import annotations

import os
import sys
import asyncio
import httpx

API = os.environ.get("CLEAN_API", "https://app.cleanhands.fun").rstrip("/")
TOKEN = os.environ.get("TG_ALERTS_TOKEN") or os.environ.get("TG_COMMUNITY_TOKEN", "")
CHAT = os.environ.get("TG_ALERTS_CHAT", "")


def fmt_usd(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    a = abs(n)
    for u, d in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if a >= d:
            return f"${n / d:.2f}{u}"
    if a and a < 1:
        return f"${n:.6f}".rstrip("0").rstrip(".")
    return f"${n:,.2f}"


def build(price: dict | None) -> str:
    if price and price.get("available"):
        ch = price.get("change_24h")
        chs = f"{'+' if (ch or 0) >= 0 else ''}{float(ch):.1f}%" if ch is not None else "—"
        body = (
            f"💰 {fmt_usd(price.get('price_usd'))} ({chs} 24h)\n"
            f"📊 MC {fmt_usd(price.get('market_cap'))} · Liq {fmt_usd(price.get('liquidity_usd'))} · "
            f"Vol {fmt_usd(price.get('volume_24h'))}"
        )
    else:
        body = "Stake your bags, burn for boosts, climb the board."
    return f"☀️ <b>GM, clean hands.</b> 🧤\n{body}\n\nStake &amp; earn 👉 {API}"


async def main() -> int:
    dry = "--dry-run" in sys.argv
    price = None
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            price = (await c.get(f"{API}/api/price")).json()
    except Exception:  # noqa: BLE001
        pass
    msg = build(price)
    if dry:
        print(msg)
        return 0
    if not (TOKEN and CHAT):
        print("set TG_ALERTS_TOKEN (or TG_COMMUNITY_TOKEN) + TG_ALERTS_CHAT", file=sys.stderr)
        return 2
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={"chat_id": CHAT, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True},
            )
    except Exception as e:  # noqa: BLE001
        # httpx errors embed the request URL, which contains the bot token
        print(f"post failed: {str(e).replace(TOKEN, '***')}", file=sys.stderr)
        return 1
    print("posted GM")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
