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

import base58


def env() -> str:
    return os.environ.get("STAKE_ENV", "dev").lower()


def is_prod() -> bool:
    return env() in ("prod", "production")


def _missing(name: str) -> bool:
    return not os.environ.get(name, "").strip()


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _valid_solana_address(value: str) -> bool:
    try:
        return len(base58.b58decode(value)) == 32
    except Exception:  # noqa: BLE001
        return False


def validate_config() -> dict:
    """Validate environment. In prod, exit(1) on any hard failure. Returns a
    non-secret status summary for /healthz."""
    hard, warn = [], []

    secret = os.environ.get("STAKE_SERVER_SECRET", "")
    if not secret:
        msg = "STAKE_SERVER_SECRET is unset — sessions won't survive a restart and break across workers."
        (hard if is_prod() else warn).append(msg)
    elif len(secret) < 32:
        msg = "STAKE_SERVER_SECRET is too short — use `openssl rand -hex 32`."
        (hard if is_prod() else warn).append(msg)

    mint = os.environ.get("DEFAULT_TOKEN_MINT", "").strip()
    if not mint:
        msg = "DEFAULT_TOKEN_MINT is unset — wallet balances and burns will read as 0."
        (hard if is_prod() else warn).append(msg)
    elif not _valid_solana_address(mint):
        msg = "DEFAULT_TOKEN_MINT is not a valid Solana mint address."
        (hard if is_prod() else warn).append(msg)

    if _missing("TG_COMMUNITY_TOKEN"):
        msg = "TG_COMMUNITY_TOKEN unset — Telegram initData binding disabled (wallet-only login)."
        (hard if is_prod() else warn).append(msg)

    admin = os.environ.get("STAKE_ADMIN_TOKEN", "")
    if not admin:
        msg = "STAKE_ADMIN_TOKEN unset — payout settlement and kill-switch admin endpoints are unusable."
        (hard if is_prod() else warn).append(msg)
    elif len(admin) < 32:
        msg = "STAKE_ADMIN_TOKEN is too short — use `openssl rand -hex 32`."
        (hard if is_prod() else warn).append(msg)

    rpc = os.environ.get("SOLANA_RPC_URL", "")
    if is_prod() and not rpc:
        hard.append("SOLANA_RPC_URL is required in prod.")
    elif is_prod() and "api.mainnet-beta.solana.com" in rpc and not _truthy("STAKE_ALLOW_PUBLIC_RPC"):
        hard.append("Public Solana RPC is refused in prod for a 10k-user launch. Use Helius/Triton.")

    if is_prod() and _missing("REDIS_URL") and not _truthy("STAKE_ALLOW_MEMORY_STORE"):
        hard.append("REDIS_URL is required in prod so nonces/rate limits survive multiple workers.")

    if is_prod() and _missing("DATABASE_URL") and not _truthy("STAKE_ALLOW_SQLITE"):
        warn.append("DATABASE_URL unset — SQLite is acceptable only for a single-node canary, not horizontal scale.")

    if is_prod() and _missing("STAKE_CORS_ORIGINS"):
        warn.append("STAKE_CORS_ORIGINS unset — external website origins cannot call the API.")

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
        "ok": not hard,
        "warnings": warn,
        "mint_set": bool(mint),
        "secret_set": bool(secret),
        "tg_set": not _missing("TG_COMMUNITY_TOKEN"),
        "admin_set": bool(admin),
        "redis_set": not _missing("REDIS_URL"),
        "database": "postgres"
        if os.environ.get("DATABASE_URL", "").startswith(("postgres://", "postgresql://"))
        else "sqlite",
    }
