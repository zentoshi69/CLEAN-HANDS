#!/usr/bin/env python3
"""
pay.py — operator CLI for the manual payout flow. Runs on the server against the
same DB (STAKE_DB / DATABASE_URL) as the API.

  python pay.py list                  # list unpaid (requested) claims + total
  python pay.py mark <id> <tx_sig>    # after sending $CLEAN from the treasury,
                                      #   record the payout tx (idempotent)

No private key is used here — you sign the treasury transfer in your own wallet,
then record its signature. `mark` only ever transitions a 'requested' claim to
'paid' once, so re-running it is safe.
"""

from __future__ import annotations

import sys
import db


def cmd_list() -> int:
    with db.db() as conn:
        rows = db.list_pending_claims(conn)
        payout = {}
        for r in rows:
            if r["wallet"] not in payout:
                s = db.get_staker(conn, r["wallet"])
                payout[r["wallet"]] = (s["payout_wallet"] if s else None) or r["wallet"]
    if not rows:
        print("No pending claims. 🎉")
        return 0
    print(f"{'ID':>6}  {'AMOUNT ($CLEAN)':>18}  PAY TO  (staker)")
    print("-" * 100)
    total = 0
    for r in rows:
        total += r["amount"]
        dest = payout[r["wallet"]]
        suffix = "" if dest == r["wallet"] else f"  (staker {r['wallet']})"
        print(f"{r['id']:>6}  {db.to_ui(r['amount']):>18,.6f}  {dest}{suffix}")
    print("-" * 100)
    print(f"{len(rows)} claim(s) — pay {db.to_ui(total):,.6f} $CLEAN total from the treasury.")
    return 0


def cmd_mark(claim_id: str, tx_sig: str) -> int:
    if not tx_sig or len(tx_sig) < 32:
        print("Refusing: tx_sig looks invalid (paste the real treasury transfer signature).", file=sys.stderr)
        return 2
    with db.db() as conn:
        n = db.mark_claim_paid(conn, int(claim_id), tx_sig)
    if n == 1:
        print(f"✅ claim {claim_id} marked paid (tx {tx_sig[:8]}…).")
        return 0
    print(f"⚠️ claim {claim_id} not found or already paid.", file=sys.stderr)
    return 1


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] == "list":
        return cmd_list()
    if args[0] == "mark" and len(args) == 3:
        return cmd_mark(args[1], args[2])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
