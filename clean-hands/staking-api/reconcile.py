#!/usr/bin/env python3
"""
reconcile.py — read-only integrity check over the staking ledger. Proves the
money invariants hold and flags any drift. Cron it; alert on non-zero exit.

Invariants (all in integer base units):
  • per wallet: stakers.claimed_total == Σ claims.amount
  • per wallet: stakers.claimed_total == Σ ledger(action='claim').amount
  • per wallet: stakers.total_burned  == Σ ledger(action='burn').amount
  • global:     Σ stakers.total_burned == Σ burns.amount

Usage:
    python reconcile.py            # prints report, exit 1 if any drift
"""

from __future__ import annotations

import sys
import db


def _sum(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()["s"]


def reconcile(conn) -> dict:
    issues = []
    rows = conn.execute("SELECT wallet, claimed_total, total_burned FROM stakers").fetchall()
    for r in rows:
        w = r["wallet"]
        claims_sum = _sum(conn, "SELECT COALESCE(SUM(amount),0) AS s FROM claims WHERE wallet=?", (w,))
        ledger_claim = _sum(
            conn, "SELECT COALESCE(SUM(amount),0) AS s FROM ledger WHERE wallet=? AND action='claim'", (w,)
        )
        ledger_burn = _sum(
            conn, "SELECT COALESCE(SUM(amount),0) AS s FROM ledger WHERE wallet=? AND action='burn'", (w,)
        )
        if r["claimed_total"] != claims_sum:
            issues.append({"wallet": w, "kind": "claimed_total_vs_claims", "state": r["claimed_total"], "expected": claims_sum})
        if r["claimed_total"] != ledger_claim:
            issues.append({"wallet": w, "kind": "claimed_total_vs_ledger", "state": r["claimed_total"], "expected": ledger_claim})
        if r["total_burned"] != ledger_burn:
            issues.append({"wallet": w, "kind": "total_burned_vs_ledger", "state": r["total_burned"], "expected": ledger_burn})

    total_burned = _sum(conn, "SELECT COALESCE(SUM(total_burned),0) AS s FROM stakers")
    burns_total = _sum(conn, "SELECT COALESCE(SUM(amount),0) AS s FROM burns")
    if total_burned != burns_total:
        issues.append({"wallet": "*", "kind": "global_burned_vs_burns", "state": total_burned, "expected": burns_total})

    return {"ok": not issues, "wallets_checked": len(rows), "issues": issues}


def main():
    with db.db() as conn:
        report = reconcile(conn)
    if report["ok"]:
        print(f"✅ reconciled {report['wallets_checked']} wallets — no drift")
        return 0
    print(f"❌ DRIFT DETECTED ({len(report['issues'])} issue(s)):", file=sys.stderr)
    for i in report["issues"]:
        print(
            f"  {i['wallet']}: {i['kind']} state={db.to_ui(i['state'])} expected={db.to_ui(i['expected'])}",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
