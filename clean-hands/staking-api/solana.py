#!/usr/bin/env python3
"""
Solana read layer for the staking API — the trustless facts the engine builds on:
  * a wallet's current $CLEAN balance (for soft-stake snapshots & anti-gaming),
  * verification that a given transaction really BURNED $CLEAN from that wallet.

Public RPC works for low volume; use a paid RPC (Helius/Triton) in production.
"""

from __future__ import annotations

import os
import time
import asyncio
import httpx

RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
MINT = os.environ.get("DEFAULT_TOKEN_MINT", "").strip()
TIMEOUT = 20
# How many times to re-try a single endpoint on a transient error before moving
# on to the next one. The public mainnet-beta node is heavily rate-limited (429),
# and a single-shot read that fails makes a wallet's balance silently read as 0 —
# which surfaces to users as "connected but balances don't load".
RETRIES = int(os.environ.get("SOLANA_RPC_RETRIES", "2"))
# Fallback read endpoints, tried in order after the configured RPC. All are
# CORS-open public nodes; reads (getBalance / getTokenAccountsByOwner /
# getTransaction) are idempotent, so trying another node is always safe.
_FALLBACK_RPCS = [
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet-beta.solana.com",
]
# transient HTTP statuses worth retrying / failing over (rate-limit + 5xx)
_RETRY_STATUS = {429, 500, 502, 503, 504}


def _endpoints() -> list[str]:
    """The configured RPC first, then the public fallbacks — de-duplicated."""
    out: list[str] = []
    for u in [RPC_URL, *_FALLBACK_RPCS]:
        if u and u not in out:
            out.append(u)
    return out


async def _rpc(method: str, params: list):
    """One JSON-RPC read, resilient to a flaky/rate-limited node: retry the same
    endpoint a few times on a transient error, then fail over to the next one.
    Only raises once every endpoint has been exhausted."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last_err: Exception | None = None
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        for url in _endpoints():
            for attempt in range(RETRIES + 1):
                try:
                    r = await c.post(
                        url, json=payload, headers={"content-type": "application/json"}
                    )
                    if r.status_code in _RETRY_STATUS:
                        last_err = RuntimeError(f"HTTP {r.status_code} from {url}")
                        await asyncio.sleep(0.4 * (attempt + 1))
                        continue  # retry same endpoint, then fail over
                    r.raise_for_status()
                    data = r.json()
                    if "error" in data:
                        # An RPC-level error (bad params, node quirk) won't be fixed
                        # by hammering the SAME node — try the next endpoint instead.
                        last_err = RuntimeError(data["error"])
                        break
                    return data.get("result")
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    last_err = e
                    await asyncio.sleep(0.4 * (attempt + 1))
                    continue
    if last_err is not None:
        raise last_err
    raise RuntimeError("rpc: no endpoints configured")


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


async def rpc_health() -> bool:
    """Cheap dependency probe for /readyz."""
    try:
        res = await _rpc("getHealth", [])
        return res in ("ok", None) or bool(res)
    except Exception:  # noqa: BLE001
        return False


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


async def verify_transfer(
    signature: str,
    destination_wallet: str,
    amount_base: int,
    mint: str | None = None,
) -> bool:
    """True iff a finalized tx increased `destination_wallet`'s `mint` balance
    by at least `amount_base` raw units.

    Token accounts are not wallets; the authoritative signal is the
    owner-attributed token balance delta emitted by finalized transaction
    metadata.
    """
    mint = mint or MINT
    if not (mint and signature and destination_wallet and amount_base > 0):
        return False
    tx = await _rpc(
        "getTransaction",
        [
            signature,
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "finalized"},
        ],
    )
    if not tx or (tx.get("meta") or {}).get("err") is not None:
        return False
    meta = tx.get("meta") or {}

    def raw_amount(entry) -> int:
        ui = (entry or {}).get("uiTokenAmount") or {}
        try:
            return int(ui.get("amount") or 0)
        except (TypeError, ValueError):
            return 0

    pre = {}
    for entry in meta.get("preTokenBalances") or []:
        if entry.get("mint") == mint and entry.get("owner") == destination_wallet:
            pre[entry.get("accountIndex")] = raw_amount(entry)
    delta = 0
    for entry in meta.get("postTokenBalances") or []:
        if entry.get("mint") != mint or entry.get("owner") != destination_wallet:
            continue
        delta += raw_amount(entry) - pre.get(entry.get("accountIndex"), 0)
    return delta >= int(amount_base)


SOL_MINT = "So11111111111111111111111111111111111111112"


async def verify_mm_deposit(
    signature: str, wallet: str, mm_wallet: str, mint: str | None = None,
    max_age_s: int | None = None,
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
    # Reject stale deposits: legs are valued at the LIVE price, so an old transfer
    # could be credited at a price that no longer reflects it — require recency.
    if max_age_s is not None:
        bt = tx.get("blockTime")
        if not bt or (time.time() - float(bt)) > max_age_s:
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
