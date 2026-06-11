#!/usr/bin/env python3
"""
alerts_bot.py — posts $CLEAN market signals to your Telegram channel: big price
moves, market-cap milestones, and a periodic price heartbeat. Pulls DexScreener
(public, no key); posts via the Bot API. State is persisted so a restart doesn't
re-spam.

  pip install httpx
  export TG_ALERTS_TOKEN="123:ABC..."     # a bot that is ADMIN of the channel
                                          #   (falls back to TG_COMMUNITY_TOKEN)
  export TG_ALERTS_CHAT="@yourchannel"    # or a numeric -100… channel id
  export DEFAULT_TOKEN_MINT="<mint>"
  python alerts_bot.py                    # run forever (systemd: degen-alerts)
  python alerts_bot.py --selftest         # run pure-logic checks, exit

Tunables: ALERTS_POLL (s, 120), ALERTS_PRICE_PCT (15), ALERTS_HEARTBEAT_HRS (6).

Note: DexScreener gives aggregate stats, not per-trade. For true per-buy "buy
bot" alerts, plug a paid stream (Helius logsSubscribe / Bitquery) into
`emit_from_summary` — the posting + dedupe framework here is ready for it.
"""

from __future__ import annotations

import os
import sys
import json
import time
import asyncio
import logging

import httpx

TOKEN = os.environ.get("TG_ALERTS_TOKEN") or os.environ.get("TG_COMMUNITY_TOKEN", "")
CHAT = os.environ.get("TG_ALERTS_CHAT", "")
MINT = os.environ.get("DEFAULT_TOKEN_MINT", "").strip()
PAIR = os.environ.get("DEFAULT_TOKEN_PAIR", "").strip()  # exact pool (optional)
POLL = int(os.environ.get("ALERTS_POLL", "120"))
PRICE_PCT = float(os.environ.get("ALERTS_PRICE_PCT", "15"))
HEARTBEAT = float(os.environ.get("ALERTS_HEARTBEAT_HRS", "6")) * 3600
STATE_PATH = os.environ.get("ALERTS_STATE", os.path.join(os.path.dirname(__file__), "alerts_state.json"))
DEXS = "https://api.dexscreener.com/latest/dex/tokens"
MCAP_LADDER = [100_000, 250_000, 500_000, 1_000_000, 2_500_000, 5_000_000, 10_000_000, 25_000_000, 50_000_000, 100_000_000]

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("alerts")


# --------------------------------------------------------------------------- #
#  PURE LOGIC (unit-tested via --selftest)                                     #
# --------------------------------------------------------------------------- #
def fmt_usd(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    a = abs(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if a >= div:
            return f"${n / div:.2f}{unit}"
    if a and a < 1:
        return f"${n:.6f}".rstrip("0").rstrip(".")
    return f"${n:,.2f}"


def pct_change(old: float, new: float) -> float:
    return ((new - old) / old * 100.0) if old else 0.0


def crossed_milestones(prev_mcap: float, cur_mcap: float, ladder=MCAP_LADDER) -> list[int]:
    """Milestones strictly crossed upward since last check."""
    if not prev_mcap:
        return []
    return [m for m in ladder if prev_mcap < m <= cur_mcap]


def emit_from_summary(state: dict, s: dict, now: float) -> list[str]:
    """Return the alert messages to post given prior `state` and current summary
    `s`. Mutates `state` to record what was alerted (caller persists it)."""
    msgs: list[str] = []
    sym = s.get("symbol") or "CLEAN"
    price = float(s.get("price_usd") or 0)
    mcap = float(s.get("market_cap") or 0)

    # 1) big price move vs the last alerted price
    last = state.get("last_alert_price") or price
    move = pct_change(last, price)
    if last and abs(move) >= PRICE_PCT:
        arrow = "📈🟢" if move > 0 else "📉🔴"
        msgs.append(
            f"{arrow} <b>${sym}</b> {'+' if move>=0 else ''}{move:.1f}% → {fmt_usd(price)}\n"
            f"MC {fmt_usd(mcap)} · Vol24h {fmt_usd(s.get('volume_24h'))} 🧤"
        )
        state["last_alert_price"] = price

    # 2) market-cap milestones
    for m in crossed_milestones(state.get("last_mcap") or 0, mcap):
        msgs.append(f"🏆 <b>${sym}</b> just crossed <b>{fmt_usd(m)}</b> market cap! clean hands, dirty money 🧤")
    state["last_mcap"] = mcap

    # 3) heartbeat
    if (now - (state.get("last_heartbeat") or 0)) >= HEARTBEAT:
        ch = s.get("change_24h")
        chs = f"{'+' if (ch or 0)>=0 else ''}{float(ch):.1f}%" if ch is not None else "—"
        msgs.append(
            f"🧤 <b>${sym}</b> {fmt_usd(price)} ({chs} 24h)\n"
            f"MC {fmt_usd(mcap)} · Liq {fmt_usd(s.get('liquidity_usd'))} · Vol {fmt_usd(s.get('volume_24h'))}"
        )
        state["last_heartbeat"] = now
        if "last_alert_price" not in state:
            state["last_alert_price"] = price

    return msgs


# --------------------------------------------------------------------------- #
#  IO                                                                          #
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f)
    except OSError as e:
        log.warning("could not persist state: %s", e)


async def fetch_summary() -> dict | None:
    try:
        url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{PAIR}" if PAIR else f"{DEXS}/{MINT}"
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(url, headers={"User-Agent": "clean-alerts/1.0"})
        data = r.json() if r.status_code == 200 else {}
        pairs = (data.get("pairs") or ([data["pair"]] if data.get("pair") else [])) if PAIR else (data.get("pairs") or [])
        if not pairs:
            return None
        p = pairs[0] if PAIR else max(pairs, key=lambda x: (x.get("liquidity") or {}).get("usd") or 0)
        base = p.get("baseToken") or {}
        return {
            "symbol": base.get("symbol"),
            "price_usd": p.get("priceUsd"),
            "change_24h": (p.get("priceChange") or {}).get("h24"),
            "market_cap": p.get("marketCap"),
            "liquidity_usd": (p.get("liquidity") or {}).get("usd"),
            "volume_24h": (p.get("volume") or {}).get("h24"),
        }
    except Exception as e:  # noqa: BLE001
        log.warning("dexscreener fetch failed: %s", e)
        return None


async def post(text: str) -> None:
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(url, json={"chat_id": CHAT, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
    except Exception as e:  # noqa: BLE001
        # httpx errors embed the request URL, which contains the bot token
        log.warning("post failed: %s", str(e).replace(TOKEN, "***"))


async def run() -> None:
    if not (TOKEN and CHAT and MINT):
        raise SystemExit("Set TG_ALERTS_TOKEN (or TG_COMMUNITY_TOKEN), TG_ALERTS_CHAT, DEFAULT_TOKEN_MINT.")
    state = load_state()
    log.info("alerts bot started; polling every %ss", POLL)
    while True:
        s = await fetch_summary()
        if s:
            for msg in emit_from_summary(state, s, time.time()):
                await post(msg)
                log.info("alert: %s", msg.split(chr(10))[0])
            save_state(state)
        await asyncio.sleep(POLL)


def _selftest() -> None:
    assert abs(pct_change(100, 115) - 15) < 1e-9
    assert crossed_milestones(900_000, 1_200_000) == [1_000_000]
    assert crossed_milestones(0, 5_000_000) == []  # no prior baseline -> no spam
    st = {"last_alert_price": 0.01, "last_mcap": 900_000, "last_heartbeat": time.time()}
    msgs = emit_from_summary(st, {"symbol": "CLEAN", "price_usd": 0.012, "market_cap": 1_050_000, "volume_24h": 5000}, time.time())
    assert any("20.0%" in m or "+20" in m for m in msgs), msgs  # 0.01->0.012 = +20%
    assert any("crossed" in m for m in msgs)  # crossed 1M
    assert st["last_alert_price"] == 0.012
    print("alerts selftest ✓")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        asyncio.run(run())
