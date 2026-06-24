#!/usr/bin/env python3
"""
ops-digest.py — DM the owner a daily ops digest: pending payouts + reconcile
status + staking stats. Runs on the VPS (reads the DB directly). Schedule it in
OpenClaw Cron or system cron once a day.

  TG_NOTIFY_TOKEN=123:ABC OWNER_CHAT=<your telegram id> ./venv/bin/python ops-digest.py
  ./venv/bin/python ops-digest.py --dry-run     # print the digest, don't send

(OWNER_CHAT falls back to the first id in TG_ADMIN_IDS; token falls back to
TG_COMMUNITY_TOKEN. You must have started the bot in DM for it to message you.)
"""
from __future__ import annotations

import os
import sys
import asyncio
import httpx

import db
import reconcile

TOKEN = os.environ.get("TG_NOTIFY_TOKEN") or os.environ.get("TG_COMMUNITY_TOKEN", "")
OWNER = os.environ.get("OWNER_CHAT") or (os.environ.get("TG_ADMIN_IDS", "").split(",")[0].strip())


def build() -> str:
    db.init_db()
    with db.db() as conn:
        pend = db.list_pending_claims(conn)
        total = sum(r["amount"] for r in pend)
        rec = reconcile.reconcile(conn)
        eff = db.effective_staked_expr()
        # eff is an internal SQL constant, not user input.
        s = conn.execute(
            f"SELECT COUNT(*) AS n, COALESCE(SUM({eff}),0) AS s "  # nosec B608
            f"FROM stakers WHERE ({eff}) > 0"
        ).fetchone()
    lines = [
        "🧤 CLEAN ops digest",
        f"• Pending payouts: {len(pend)} ({db.to_ui(total):,.2f} $CLEAN to send)",
        f"• Active stakers: {s['n']} · staked {db.to_ui(s['s']):,.0f} $CLEAN",
        f"• Reconcile: {'✅ clean' if rec['ok'] else '❌ DRIFT — ' + str(len(rec['issues'])) + ' issue(s)!'}",
    ]
    if pend:
        lines.append("→ run `./venv/bin/python pay.py list` from staking-api to pay them out.")
    return "\n".join(lines)


async def main() -> int:
    msg = build()
    if "--dry-run" in sys.argv:
        print(msg)
        return 0
    if not (TOKEN and OWNER):
        print("set TG_NOTIFY_TOKEN (or TG_COMMUNITY_TOKEN) + OWNER_CHAT/TG_ADMIN_IDS", file=sys.stderr)
        return 2
    async with httpx.AsyncClient(timeout=15) as c:
        await c.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": int(OWNER), "text": msg},
        )
    print("sent digest")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
