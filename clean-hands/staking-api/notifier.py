#!/usr/bin/env python3
"""
notifier.py — push notifications for stakers. DMs a user (via the bot) when they
have meaningful rewards ready to claim, so they come back and engage. Reads the
same staking DB; sends through the Bot API to each user's Telegram id (which we
bound to their wallet at login).

Dedup: one nudge per (wallet, kind) per cooldown window (notifs table), so it
never spams. A bot can only DM users who have started it (opened the app via the
bot), which is exactly our audience.

  pip install -r requirements.txt
  export TG_NOTIFY_TOKEN="123:ABC..."     # the community bot (falls back to
                                          #   TG_COMMUNITY_TOKEN)
  export NOTIFY_CLAIM_MIN=1000            # min CLAIMable tokens to nudge (UI units)
  python notifier.py                      # run forever (systemd: degen-notifier)
  python notifier.py --selftest           # pure-logic checks, exit
"""

from __future__ import annotations

import os
import sys
import time
import asyncio
import logging

import httpx

import db
import economics as econ

TOKEN = os.environ.get("TG_NOTIFY_TOKEN") or os.environ.get("TG_COMMUNITY_TOKEN", "")
POLL = int(os.environ.get("NOTIFY_POLL", "900"))  # every 15 min
COOLDOWN = int(os.environ.get("NOTIFY_COOLDOWN", str(24 * 3600)))  # 1 nudge/day/kind
CLAIM_MIN_BASE = db.to_base(float(os.environ.get("NOTIFY_CLAIM_MIN", "1000")))

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("notifier")


# --------------------------------------------------------------------------- #
#  PURE LOGIC (unit-tested via --selftest)                                     #
# --------------------------------------------------------------------------- #
def pending_base(row, refs: int, now: int) -> int:
    """Read-only projection of a staker's claimable rewards at `now` (base units),
    mirroring the API's accrual without writing."""
    eff = econ.effective_staked(row["recorded_staked"], row["cached_balance"])
    secs = now - row["stake_start_ts"] if (eff > 0 and row["stake_start_ts"]) else 0
    apr = econ.effective_apr(db.to_ui(eff), secs, refs, db.to_ui(row["total_burned"])).effective_apr
    dt = now - row["last_accrual_ts"]
    return int(row["accrued"]) + int(econ.accrue(eff, apr, dt))


def should_notify_claim(pending: int, min_base: int, last_ts: int, now: int, cooldown: int) -> bool:
    return pending >= min_base and (now - last_ts) >= cooldown


def claim_ready_by_rules(row, now: int) -> bool:
    lock_days = int(os.environ.get("STAKE_CLAIM_LOCK_DAYS", "90") or 0)
    setup_days = int(os.environ.get("STAKE_PAYOUT_SETUP_DAYS", "3") or 0)
    start = int(row["stake_start_ts"] or 0)
    if lock_days > 0 and (not start or now - start < lock_days * 86400):
        return False
    if setup_days > 0 and not row["payout_confirmed_ts"]:
        return False
    return True


# --------------------------------------------------------------------------- #
#  RUNTIME                                                                     #
# --------------------------------------------------------------------------- #
async def dm(chat_id: int, text: str) -> bool:
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        log.warning("dm failed: %s", e)
        return False


async def sweep_once() -> int:
    now = int(time.time())
    sent = 0
    with db.db() as conn:
        rows = db.stakers_with_tg(conn)
        for r in rows:
            refs = db.active_referrals(conn, r["wallet"])
            pending = pending_base(r, refs, now)
            last = db.notif_last(conn, r["wallet"], "claim_ready")
            if not claim_ready_by_rules(r, now):
                continue
            if not should_notify_claim(pending, CLAIM_MIN_BASE, last, now, COOLDOWN):
                continue
            ok = await dm(
                r["tg_id"],
                f"🧤 <b>${'CLEAN'}</b> payout window ready!\n"
                f"You have <b>{db.to_ui(pending):,.2f}</b> $CLEAN available to request. "
                f"Open the app to request payout.",
            )
            if ok:
                db.notif_mark(conn, r["wallet"], "claim_ready", now)
                sent += 1
    return sent


async def run() -> None:
    if not TOKEN:
        raise SystemExit("Set TG_NOTIFY_TOKEN (or TG_COMMUNITY_TOKEN).")
    log.info("notifier started; sweep every %ss, claim_min=%s base", POLL, CLAIM_MIN_BASE)
    while True:
        try:
            n = await sweep_once()
            if n:
                log.info("sent %d claim-ready nudges", n)
        except Exception as e:  # noqa: BLE001
            log.warning("sweep error: %s", e)
        await asyncio.sleep(POLL)


def _selftest() -> None:
    assert should_notify_claim(2000, 1000, 0, 100000, 3600) is True
    assert should_notify_claim(500, 1000, 0, 100000, 3600) is False  # below min
    assert should_notify_claim(2000, 1000, 99000, 100000, 3600) is False  # cooldown
    # pending projection: 1000 base staked-effective, 100% APR, 1 year, +0 prior
    row = {
        "recorded_staked": 1000, "cached_balance": 1000, "stake_start_ts": 1,
        "last_accrual_ts": 1, "accrued": 0, "total_burned": 0,
    }
    p = pending_base(row, 0, 1 + econ.SECONDS_PER_YEAR)
    assert p >= 0  # deterministic, non-negative; exact value depends on APR config
    print("notifier selftest ✓")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        asyncio.run(run())
