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
    ./venv/bin/python reconcile.py # prints report, exit 1 if any drift
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


async def reconcile_chain(conn) -> dict:
    """Deeper, on-chain check (opt-in via --chain): every recorded burn signature
    must correspond to a real finalized on-chain burn of at least that amount by
    that wallet. Catches DB tampering / fabricated burn rows that the internal
    check (all in the same writable DB) cannot see. One RPC per burn — slower, so
    schedule it on a less-frequent timer than the fast internal reconcile.

    A transient RPC failure is INCONCLUSIVE (a warning), never counted as drift,
    so a flaky RPC can't trigger a false tamper alert."""
    import solana

    issues, warnings, checked = [], [], 0
    rows = conn.execute("SELECT signature, wallet, amount FROM burns").fetchall()
    for r in rows:
        checked += 1
        try:
            onchain_base = db.to_base(await solana.verify_burn(r["signature"], r["wallet"]))
        except Exception as e:  # noqa: BLE001 — RPC hiccup is inconclusive, not drift
            warnings.append({"signature": r["signature"], "detail": str(e)})
            continue
        # The recorded burn must not exceed what actually burned on-chain (allow a
        # 1-base-unit rounding tolerance). A fabricated row verifies as 0.
        if onchain_base + 1 < r["amount"]:
            issues.append(
                {"wallet": r["wallet"], "kind": "burn_not_on_chain",
                 "signature": r["signature"], "state": r["amount"], "expected": onchain_base}
            )
    return {"ok": not issues, "burns_checked": checked, "issues": issues, "warnings": warnings}


def main():
    chain = "--chain" in sys.argv
    creport = None
    with db.db() as conn:
        report = reconcile(conn)
        if chain:
            import asyncio
            creport = asyncio.run(reconcile_chain(conn))

    ok = report["ok"] and (creport is None or creport["ok"])
    if report["ok"]:
        print(f"✅ reconciled {report['wallets_checked']} wallets — no internal drift")
    else:
        print(f"❌ DRIFT DETECTED ({len(report['issues'])} issue(s)):", file=sys.stderr)
        for i in report["issues"]:
            print(
                f"  {i['wallet']}: {i['kind']} state={db.to_ui(i['state'])} expected={db.to_ui(i['expected'])}",
                file=sys.stderr,
            )
    if creport is not None:
        for w in creport["warnings"]:
            print(f"  ⚠️ on-chain check inconclusive for {w['signature']}: {w['detail']}", file=sys.stderr)
        if creport["ok"]:
            print(f"✅ on-chain: {creport['burns_checked']} burns verified against the chain")
        else:
            print(f"❌ ON-CHAIN DRIFT ({len(creport['issues'])} issue(s)):", file=sys.stderr)
            for i in creport["issues"]:
                print(
                    f"  {i['wallet']}: recorded {db.to_ui(i['state'])} $CLEAN but on-chain burn "
                    f"is {db.to_ui(i['expected'])} (sig {i['signature']})",
                    file=sys.stderr,
                )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
