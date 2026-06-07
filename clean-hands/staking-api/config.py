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
