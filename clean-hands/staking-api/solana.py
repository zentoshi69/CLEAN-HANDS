#!/usr/bin/env python3
"""
Solana read layer for the staking API — the trustless facts the engine builds on:
  * a wallet's current $CLEAN balance (for soft-stake snapshots & anti-gaming),
  * verification that a given transaction really BURNED $CLEAN from that wallet.

Public RPC works for low volume; use a paid RPC (Helius/Triton) in production.
"""

from __future__ import annotations

import os
import httpx

RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
MINT = os.environ.get("DEFAULT_TOKEN_MINT", "").strip()
TIMEOUT = 20


async def _rpc(method: str, params: list):
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(
            RPC_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers={"content-type": "application/json"},
        )
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data.get("result")


async def token_balance(wallet: str, mint: str | None = None) -> float:
    """Sum the wallet's UI balance of `mint` across its token accounts."""
    mint = mint or MINT
    if not mint:
        return 0.0
    res = await _rpc(
        "getTokenAccountsByOwner",
        [wallet, {"mint": mint}, {"encoding": "jsonParsed"}],
    )
    total = 0.0
    for acc in (res or {}).get("value", []):
        # Defensive .get() chain: skip any account the RPC didn't return as
        # jsonParsed (e.g. token-2022 / unusual accounts) rather than throwing —
        # one unparsable account must not freeze the wallet's balance refresh.
        data = ((acc or {}).get("account") or {}).get("data")
        info = (data.get("parsed") or {}).get("info") if isinstance(data, dict) else None
        if not isinstance(info, dict):
            continue
        ta = info.get("tokenAmount") or {}
        total += float(ta.get("uiAmount") or 0)
    return total


async def sol_balance(wallet: str) -> float:
    """The wallet's native SOL balance (in whole SOL, not lamports)."""
    if not wallet:
        return 0.0
    res = await _rpc("getBalance", [wallet])
    # getBalance returns {"context": ..., "value": <lamports>}; tolerate a bare int.
    lamports = res.get("value") if isinstance(res, dict) else res
    return float(lamports or 0) / 1_000_000_000


async def verify_burn(signature: str, wallet: str, mint: str | None = None) -> float:
    """Return the amount of `mint` BURNED by `wallet` in transaction `signature`,
    or 0.0 if the tx isn't a successful burn by that wallet. Parses both
    `burn` and `burnChecked` SPL-token instructions (top-level + inner)."""
    mint = mint or MINT
    if not (mint and signature and wallet):
        return 0.0
    # commitment=finalized: only credit burns that can no longer be rolled back.
    tx = await _rpc(
        "getTransaction",
        [
            signature,
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "finalized"},
        ],
    )
    if not tx or (tx.get("meta") or {}).get("err") is not None:
        return 0.0

    msg = (tx.get("transaction") or {}).get("message") or {}
    instrs = list(msg.get("instructions") or [])
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        instrs.extend(inner.get("instructions") or [])

    burned = 0.0
    for ix in instrs:
        if ix.get("program") != "spl-token":
            continue
        parsed = ix.get("parsed") or {}
        if parsed.get("type") not in ("burn", "burnChecked"):
            continue
        info = parsed.get("info") or {}
        # must be a burn of OUR mint — reject missing or mismatched mint outright
        if info.get("mint") != mint:
            continue
        # the authority/owner doing the burn must be our wallet
        who = info.get("authority") or info.get("owner")
        if who != wallet:
            continue
        ta = info.get("tokenAmount")
        if isinstance(ta, dict) and ta.get("uiAmount") is not None:
            burned += float(ta["uiAmount"])
        elif info.get("amount") is not None:
            # raw `burn` carries no uiAmount; prefer on-chain decimals when the
            # instruction provides them, else fall back to the configured value.
            dec = info.get("decimals")
            decimals = int(dec) if dec is not None else int(os.environ.get("DEFAULT_TOKEN_DECIMALS", "6"))
            burned += float(info["amount"]) / (10**decimals)
    return burned


SOL_MINT = "So11111111111111111111111111111111111111112"


async def verify_mm_deposit(
    signature: str, wallet: str, mm_wallet: str, mint: str | None = None
) -> tuple[float, float]:
    """Return (sol, clean) that `wallet` transferred TO `mm_wallet` in `signature`.

    SOL is summed from System-program transfers (source -> destination, which is
    unambiguous). CLEAN is read from the meta token-balance deltas: any account
    OWNED BY mm_wallet holding `mint` whose balance increased over the tx — this
    is robust to associated-token-account indirection. (0.0, 0.0) if the tx
    failed or moved nothing to the reserve."""
    mint = mint or MINT
    if not (signature and wallet and mm_wallet):
        return (0.0, 0.0)
    tx = await _rpc(
        "getTransaction",
        [
            signature,
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "finalized"},
        ],
    )
    if not tx or (tx.get("meta") or {}).get("err") is not None:
        return (0.0, 0.0)

    msg = (tx.get("transaction") or {}).get("message") or {}
    instrs = list(msg.get("instructions") or [])
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        instrs.extend(inner.get("instructions") or [])

    sol = 0.0
    for ix in instrs:
        if ix.get("program") != "system":
            continue
        parsed = ix.get("parsed") or {}
        if parsed.get("type") != "transfer":
            continue
        info = parsed.get("info") or {}
        if info.get("source") == wallet and info.get("destination") == mm_wallet:
            sol += float(info.get("lamports") or 0) / 1e9

    clean = 0.0
    if mint:
        meta = tx.get("meta") or {}
        pre = {}
        for b in meta.get("preTokenBalances") or []:
            if b.get("owner") == mm_wallet and b.get("mint") == mint:
                pre[b.get("accountIndex")] = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        for b in meta.get("postTokenBalances") or []:
            if b.get("owner") == mm_wallet and b.get("mint") == mint:
                before = pre.get(b.get("accountIndex"), 0.0)
                after = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
                if after > before:
                    clean += after - before
    return (sol, clean)
