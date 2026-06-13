#!/usr/bin/env python3
"""
config.py — startup validation so the staking API fails FAST and LOUD on a
misconfiguration instead of silently serving wrong data (e.g. 0 balances because
DEFAULT_TOKEN_MINT was never set).

Set STAKE_ENV=prod on production hosts to enforce the hard requirements.
"""

from __future__ import annotations

import os
import sys


def env() -> str:
    return os.environ.get("STAKE_ENV", "dev").lower()


def is_prod() -> bool:
    return env() in ("prod", "production")


def _missing(name: str) -> bool:
    return not os.environ.get(name, "").strip()


def validate_config() -> dict:
    """Validate environment. In prod, exit(1) on any hard failure. Returns a
    non-secret status summary for /healthz."""
    hard, warn = [], []

    if _missing("STAKE_SERVER_SECRET"):
        msg = "STAKE_SERVER_SECRET is unset — sessions won't survive a restart and break across workers."
        (hard if is_prod() else warn).append(msg)
    if _missing("DEFAULT_TOKEN_MINT"):
        msg = "DEFAULT_TOKEN_MINT is unset — wallet balances and burns will read as 0."
        (hard if is_prod() else warn).append(msg)
    if _missing("TG_COMMUNITY_TOKEN"):
        warn.append("TG_COMMUNITY_TOKEN unset — Telegram initData binding disabled (wallet-only login).")
    rpc = os.environ.get("SOLANA_RPC_URL", "")
    if is_prod() and ("api.mainnet-beta.solana.com" in rpc or not rpc):
        warn.append("Using the public Solana RPC in prod — it WILL rate-limit at scale. Use Helius/Triton.")

    # No Stains Bridge (white-label EasyBit) — only checked when API mode is on.
    if os.environ.get("EASYBIT_API_KEY", "").strip():
        if _missing("BRIDGE_RESERVE_WALLET"):
            warn.append(
                "EASYBIT_API_KEY set but BRIDGE_RESERVE_WALLET unset — the $ fee has no documented "
                "destination. Set it, and point your EasyBit affiliate payout at the same wallet."
            )
        try:
            fee = float(os.environ.get("BRIDGE_FEE_USD", "5") or 5)
            mn = float(os.environ.get("BRIDGE_MIN_ORDER_USD", "55") or 55)
            cap = float(os.environ.get("BRIDGE_EXTRA_FEE_MAX_PCT", "10") or 10)
            if mn > 0 and cap < (fee / mn * 100.0):
                warn.append(
                    f"BRIDGE_EXTRA_FEE_MAX_PCT ({cap}%) is below the fee needed at the minimum order "
                    f"({fee/mn*100:.2f}%) — the flat ${fee:.0f} fee will be clamped on the smallest orders."
                )
        except (TypeError, ValueError):
            warn.append("BRIDGE_* fee/min values are not numeric — using defaults.")

    for w in warn:
        print(f"[config][warn] {w}", file=sys.stderr)
    if hard:
        for h in hard:
            print(f"[config][FATAL] {h}", file=sys.stderr)
        print("Refusing to start in prod with the above. Fix env or unset STAKE_ENV for dev.", file=sys.stderr)
        raise SystemExit(1)

    return {
        "env": env(),
        "mint_set": not _missing("DEFAULT_TOKEN_MINT"),
        "secret_set": not _missing("STAKE_SERVER_SECRET"),
        "tg_set": not _missing("TG_COMMUNITY_TOKEN"),
    }
