#!/usr/bin/env python3
"""
bridge.py — business logic for the No Stains Bridge (white-label EasyBit).

Owns the three things EasyBit itself doesn't: the $55 USD minimum order, the
flat $5 service fee, and input sanitisation. The swap stays fully non-custodial
— funds go user -> EasyBit -> the user's receive address, the app never holds
them.

FEE MODEL (non-custodial, no extra moving parts):
  The $5 fee is collected as EasyBit's per-order PARTNER fee. We convert the
  flat $5 target into the percentage EasyBit needs for THIS order
  (pct = 5 / order_usd * 100) and pass it as the per-order fee override, so the
  quoted receive amount already reflects it. EasyBit credits that commission to
  the partner balance, which EasyBit pays out to the wallet configured in your
  EasyBit dashboard — set that to your corporate reserve wallet
  (BRIDGE_RESERVE_WALLET here is recorded for display/reconciliation only).

  Defaults are mutually consistent: $55 min + $5 fee + 10% cap means the flat $5
  is achievable across the whole allowed range (5/55 = 9.09% < 10%); above the
  minimum the percentage shrinks so the fee stays ~$5 flat.

USD VALUATION:
  Single-vendor — we value the send amount through EasyBit's own rate to USDT
  (stablecoins short-circuit to face value), so the min check reflects the same
  liquidity the swap will actually use. If a coin can't be priced and
  BRIDGE_REQUIRE_USD is on (default), we ask the user to retry rather than let an
  unmetered order through.

Env:
    BRIDGE_FEE_USD            flat fee target in USD (default 5)
    BRIDGE_MIN_ORDER_USD      minimum order in USD (default 55)
    BRIDGE_EXTRA_FEE_MAX_PCT  clamp for the per-order fee % (default 10)
    BRIDGE_EXTRA_FEE_MIN_PCT  floor for the per-order fee % (default 0)
    BRIDGE_REQUIRE_USD        1/true: block orders we can't price (default 1)
    BRIDGE_RESERVE_WALLET     reserve wallet, recorded for display (optional)
    BRIDGE_BRAND              UI brand name (default "No Stains Bridge")
"""

from __future__ import annotations

import hmac
import hashlib
import os
import re
import sys
import time
from decimal import Decimal, InvalidOperation

import easybit
import db


# --------------------------------------------------------------------------- #
#  CONFIG                                                                      #
# --------------------------------------------------------------------------- #
def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, "") or default)
    except (TypeError, ValueError):
        return default


def _bool(env: str, default: bool) -> bool:
    v = (os.environ.get(env, "") or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


FEE_USD = _f("BRIDGE_FEE_USD", 5.0)
MIN_ORDER_USD = _f("BRIDGE_MIN_ORDER_USD", 55.0)
MAX_FEE_PCT = _f("BRIDGE_EXTRA_FEE_MAX_PCT", 10.0)
MIN_FEE_PCT = _f("BRIDGE_EXTRA_FEE_MIN_PCT", 0.0)
# Defense-in-depth: a misconfigured env must never produce a negative or >100%
# partner fee, nor an inverted band. Clamp to [0, 100] with MIN <= MAX at load.
MIN_FEE_PCT = max(0.0, MIN_FEE_PCT)
MAX_FEE_PCT = min(100.0, max(MIN_FEE_PCT, MAX_FEE_PCT))
# Decimal places kept on the fee PERCENTAGE. This must be fine enough that the
# flat $5 holds on big orders: the fee on a $1M order is 0.0005%, which 2 dp
# would round to 0.00% ($0!) — so we keep 6 dp (accurate to <1¢ up to ~$2M
# orders, <$1 up to ~$200M). Lower it only if the exchange rejects long
# fractional percents (and accept a small flat-fee drift on very large orders).
FEE_PCT_DECIMALS = max(2, min(12, int(_f("BRIDGE_FEE_PCT_DECIMALS", 6.0))))
REQUIRE_USD = _bool("BRIDGE_REQUIRE_USD", True)
RESERVE_WALLET = (os.environ.get("BRIDGE_RESERVE_WALLET", "") or "").strip()
BRAND = (os.environ.get("BRIDGE_BRAND", "") or "").strip() or "No Stains Bridge"

# Coins we treat as $1 without a pricing round-trip.
STABLES = {"USDT", "USDC", "DAI", "TUSD", "USDP", "BUSD", "FDUSD", "USDD", "GUSD", "PYUSD"}

# Small TTL cache of per-unit USD prices, keyed by (coin, network).
_PRICE_TTL = int(os.environ.get("BRIDGE_PRICE_TTL", "90") or 90)
_price_cache: dict[str, tuple[float, float]] = {}


class BridgeError(Exception):
    """User-facing validation/limit error. `status` maps to the HTTP code."""

    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# --------------------------------------------------------------------------- #
#  INPUT SANITISATION — everything here is forwarded to EasyBit, so it is       #
#  validated and normalised before it ever leaves the process.                 #
# --------------------------------------------------------------------------- #
_COIN_RE = re.compile(r"^[A-Z0-9]{1,16}$")
_NET_RE = re.compile(r"^[A-Z0-9]{1,24}$")
_AMOUNT_RE = re.compile(r"^\d{1,18}(\.\d{1,18})?$")
_ADDR_RE = re.compile(r"^[A-Za-z0-9:_.\-]{8,128}$")
_TAG_RE = re.compile(r"^[A-Za-z0-9:_.\-]{1,64}$")
_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def norm_coin(v: str | None) -> str:
    s = (v or "").strip().upper()
    if not _COIN_RE.match(s):
        raise BridgeError("invalid coin")
    return s


def norm_network(v: str | None) -> str:
    s = (v or "").strip().upper()
    if not s:
        return ""
    if not _NET_RE.match(s):
        raise BridgeError("invalid network")
    return s


def norm_amount(v) -> str:
    s = str(v if v is not None else "").strip()
    if not _AMOUNT_RE.match(s):
        raise BridgeError("invalid amount")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise BridgeError("invalid amount")
    if not d.is_finite() or d <= 0:
        raise BridgeError("amount must be greater than zero")
    if d > Decimal("1e12"):
        raise BridgeError("amount is too large")
    return s  # forwarded verbatim — never float-converted, so no rounding drift


def norm_address(v: str | None) -> str:
    s = (v or "").strip()
    if not _ADDR_RE.match(s):
        raise BridgeError("invalid destination address")
    return s


def norm_tag(v: str | None) -> str | None:
    s = (v or "").strip()
    if not s:
        return None
    if not _TAG_RE.match(s):
        raise BridgeError("invalid memo/tag")
    return s


def norm_order_id(v: str | None) -> str:
    s = (v or "").strip()
    if not _ORDER_ID_RE.match(s):
        raise BridgeError("invalid order id", status=404)
    return s


# --------------------------------------------------------------------------- #
#  PRICING + FEE                                                               #
# --------------------------------------------------------------------------- #
async def value_usd(coin: str, network: str, amount: str) -> float | None:
    """USD value of `amount` of `coin`, via EasyBit's own rate to USDT.
    Stablecoins short-circuit to face value. None when it can't be priced."""
    if coin in STABLES:
        return float(amount)
    key = f"{coin}:{network}"
    now = time.time()
    hit = _price_cache.get(key)
    unit = hit[1] if hit and (now - hit[0]) < _PRICE_TTL else None
    if unit is None:
        try:
            r = await easybit.rate(coin, "USDT", amount, send_network=network)
        except easybit.EasyBitError:
            return None
        recv = easybit.pick(r, "receiveAmount", "amount", "result")
        try:
            recv_f = float(recv)
            amt_f = float(amount)
        except (TypeError, ValueError):
            return None
        if recv_f <= 0 or amt_f <= 0:
            return None
        unit = recv_f / amt_f
        _price_cache[key] = (now, unit)
    return unit * float(amount)


def fee_for(send_usd: float) -> tuple[float, float]:
    """(fee_pct, effective_fee_usd) for an order worth `send_usd`. The percent is
    clamped to the allowed band; effective USD is what that percent collects."""
    if send_usd <= 0:
        pct = MAX_FEE_PCT
    else:
        pct = FEE_USD / send_usd * 100.0
    pct = max(MIN_FEE_PCT, min(MAX_FEE_PCT, pct))
    pct = round(pct, FEE_PCT_DECIMALS)  # NOT 2 dp — see FEE_PCT_DECIMALS
    effective = round(send_usd * pct / 100.0, 2)
    return pct, effective


def _applied_fee_pct(order: dict):
    """The partner-fee percentage the exchange actually applied, if it echoes
    one back. Lets us reconcile against what we asked for (the fee model is the
    whole revenue mechanism and the override param is provider-specific)."""
    v = easybit.pick(order, "extraFee", "extraFeeOverride", "partnerFee", "fee")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _min_hint(send_usd: float, amount: str) -> str:
    """A friendly 'about N COIN' hint for the minimum, derived from this order's
    own price so it's always self-consistent."""
    try:
        unit = send_usd / float(amount)
        if unit > 0:
            need = MIN_ORDER_USD / unit
            return f"{need:.6f}".rstrip("0").rstrip(".")
    except (ZeroDivisionError, ValueError):
        pass
    return ""


# --------------------------------------------------------------------------- #
#  QUOTE                                                                       #
# --------------------------------------------------------------------------- #
async def quote(send: str, receive: str, amount: str,
                send_network: str = "", receive_network: str = "") -> dict:
    send = norm_coin(send)
    receive = norm_coin(receive)
    send_network = norm_network(send_network)
    receive_network = norm_network(receive_network)
    amount = norm_amount(amount)
    if send == receive and send_network == receive_network:
        raise BridgeError("choose two different coins/networks")

    send_usd = await value_usd(send, send_network, amount)
    priced = send_usd is not None
    if priced and send_usd < MIN_ORDER_USD:
        hint = _min_hint(send_usd, amount)
        extra = f" (about {hint} {send})" if hint else ""
        raise BridgeError(
            f"Minimum order is ${MIN_ORDER_USD:.0f}{extra}. "
            f"This order is worth about ${send_usd:.2f}."
        )
    if not priced and REQUIRE_USD:
        raise BridgeError("couldn't price this asset right now — try again shortly", status=503)

    fee_pct, fee_usd = fee_for(send_usd or 0.0)
    # Only apply our override when we have a USD basis; otherwise let EasyBit's
    # dashboard default fee stand rather than guessing a percentage.
    override = fee_pct if priced else None

    r = await easybit.rate(send, receive, amount,
                           send_network=send_network, receive_network=receive_network,
                           extra_fee=override)
    info = {}
    try:
        info = await easybit.pair_info(send, receive, send_network, receive_network)
    except easybit.EasyBitError:
        info = {}

    receive_amount = easybit.pick(r, "receiveAmount", "result", "amount")
    rate_val = easybit.pick(r, "rate", "price")
    network_fee = easybit.pick(r, "networkFee", default=easybit.pick(info, "networkFee"))
    return {
        "send": send, "receive": receive,
        "sendNetwork": send_network, "receiveNetwork": receive_network,
        "sendAmount": amount,
        "receiveAmount": receive_amount,
        "rate": rate_val,
        "networkFee": network_fee,
        "min": easybit.pick(info, "minimumAmount", "min"),
        "max": easybit.pick(info, "maximumAmount", "max"),
        "priced": priced,
        "sendUsd": round(send_usd, 2) if priced else None,
        "feeUsd": fee_usd if priced else None,
        "feePct": fee_pct if priced else None,
        "minOrderUsd": MIN_ORDER_USD,
        "brand": BRAND,
    }


# --------------------------------------------------------------------------- #
#  ORDER                                                                       #
# --------------------------------------------------------------------------- #
async def create(*, send: str, receive: str, amount: str, receive_address: str,
                  send_network: str = "", receive_network: str = "",
                  receive_tag=None, refund_address=None, refund_tag=None,
                  client_ip: str = "") -> dict:
    send = norm_coin(send)
    receive = norm_coin(receive)
    send_network = norm_network(send_network)
    receive_network = norm_network(receive_network)
    amount = norm_amount(amount)
    receive_address = norm_address(receive_address)
    receive_tag = norm_tag(receive_tag)
    refund_address = norm_address(refund_address) if refund_address else None
    refund_tag = norm_tag(refund_tag)
    if send == receive and send_network == receive_network:
        raise BridgeError("choose two different coins/networks")

    # Re-enforce the minimum and recompute the fee SERVER-SIDE — never trust a
    # quote echoed back by the client.
    send_usd = await value_usd(send, send_network, amount)
    priced = send_usd is not None
    if priced and send_usd < MIN_ORDER_USD:
        raise BridgeError(f"Minimum order is ${MIN_ORDER_USD:.0f}.")
    if not priced and REQUIRE_USD:
        raise BridgeError("couldn't price this asset right now — try again shortly", status=503)
    fee_pct, fee_usd = fee_for(send_usd or 0.0)
    override = fee_pct if priced else None

    # Pre-flight address check. validate_address fails OPEN only on a transport
    # error (returns True); a definitive "invalid" returns False and blocks here.
    # Either way create_order below is the AUTHORITATIVE gate — the exchange
    # re-validates the address when generating the deposit address, so a bad
    # address cannot slip through even on a validation hiccup.
    if not await easybit.validate_address(receive, receive_address, receive_network):
        raise BridgeError("that destination address is not valid for the chosen coin/network")

    order = await easybit.create_order(
        send=send, receive=receive, amount=amount, receive_address=receive_address,
        send_network=send_network, receive_network=receive_network,
        receive_tag=receive_tag, refund_address=refund_address, refund_tag=refund_tag,
        extra_fee=override,
    )

    # Reconcile the fee: if the exchange echoes back the applied partner fee and
    # it disagrees with what we asked for, the override param/units may be wrong
    # for this provider — surface it loudly rather than silently mis-charging.
    if override is not None:
        applied = _applied_fee_pct(order)
        if applied is not None and abs(applied - override) > 1e-4:
            print(
                f"[bridge][warn] fee mismatch on order {easybit.order_id_of(order)}: "
                f"intended {override}% but exchange applied {applied}% — check "
                "EASYBIT_EXTRA_FEE_PARAM / fee units against the live API.",
                file=sys.stderr,
            )

    oid = easybit.order_id_of(order)
    deposit_addr = easybit.deposit_address_of(order)
    deposit_tag = easybit.deposit_tag_of(order)
    recv_amount = easybit.pick(order, "receiveAmount", "result")
    send_amount = easybit.pick(order, "sendAmount", default=amount)
    status = easybit.status_of(order) or "Awaiting Deposit"

    _persist(oid, send, send_network, receive, receive_network, send_amount, recv_amount,
             receive_address, deposit_addr, fee_usd if priced else 0.0, fee_pct if priced else 0.0,
             send_usd, status, client_ip)

    return {
        "orderId": oid,
        "depositAddress": deposit_addr,
        "depositTag": deposit_tag,
        "send": send, "receive": receive,
        "sendNetwork": send_network, "receiveNetwork": receive_network,
        "sendAmount": send_amount,
        "receiveAmount": recv_amount,
        "receiveAddress": receive_address,
        "status": status,
        "phase": phase_of(status),
        "expiresAt": easybit.pick(order, "expiresAt", "expires", "validUntil"),
        "feeUsd": fee_usd if priced else None,
        "feePct": fee_pct if priced else None,
        "brand": BRAND,
    }


async def status_of_order(order_id: str) -> dict:
    order_id = norm_order_id(order_id)
    order = await easybit.order_info(order_id)
    status = easybit.status_of(order) or "unknown"
    recv_amount = easybit.pick(order, "receiveAmount", "result")
    out_hash = easybit.pick(order, "hashOut", "outputHash", "payoutHash", "txHash")
    in_hash = easybit.pick(order, "hashIn", "inputHash", "depositHash")
    _update_status(order_id, status, recv_amount)
    return {
        "orderId": order_id,
        "status": status,
        "phase": phase_of(status),
        "receiveAmount": recv_amount,
        "depositAddress": easybit.deposit_address_of(order),
        "depositTag": easybit.deposit_tag_of(order),
        "inputHash": in_hash,
        "outputHash": out_hash,
    }


# Map EasyBit's human status to a stable phase the UI can style/branch on.
def phase_of(status: str) -> str:
    s = (status or "").strip().lower()
    if not s:
        return "unknown"
    if "complete" in s or "finished" in s or s == "done":
        return "done"
    if "refund" in s:
        return "refund"
    if "fail" in s or "expire" in s or "error" in s:
        return "failed"
    if "volatility" in s or "action" in s:
        return "action"
    if "send" in s or "payout" in s:
        return "sending"
    if "exchang" in s or "swap" in s:
        return "exchanging"
    if "confirm" in s:
        return "confirming"
    if "await" in s or "wait" in s or "deposit" in s or "new" in s:
        return "waiting"
    return "pending"


# --------------------------------------------------------------------------- #
#  PERSISTENCE — record-keeping / reconciliation (the swap works without it,   #
#  but the operator wants a forensic trail of what was bridged and the fee).   #
# --------------------------------------------------------------------------- #
def _hash_ip(ip: str) -> str:
    secret = (os.environ.get("STAKE_SERVER_SECRET", "") or "").encode()
    if not ip or not secret:
        return ""
    return hmac.new(secret, ip.encode(), hashlib.sha256).hexdigest()[:16]


def _persist(order_id, send, send_net, recv, recv_net, send_amt, recv_amt, recv_addr,
             deposit_addr, fee_usd, fee_pct, send_usd, status, client_ip) -> None:
    if not order_id:
        return
    now = int(time.time())
    try:
        with db.db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO bridge_orders "
                "(order_id, created_at, updated_at, send_coin, send_network, recv_coin, "
                " recv_network, send_amount, recv_amount, recv_address, deposit_address, "
                " fee_usd, fee_pct, send_usd, status, ip_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (order_id, now, now, send, send_net, recv, recv_net,
                 str(send_amt or ""), str(recv_amt or ""), recv_addr, deposit_addr,
                 float(fee_usd or 0), float(fee_pct or 0),
                 float(send_usd) if send_usd is not None else None,
                 status, _hash_ip(client_ip)),
            )
            conn.commit()
    except Exception:  # noqa: BLE001
        pass  # nosec B110


def _update_status(order_id, status, recv_amount) -> None:
    try:
        with db.db() as conn:
            conn.execute(
                "UPDATE bridge_orders SET status=?, recv_amount=COALESCE(?,recv_amount), "
                "updated_at=? WHERE order_id=?",
                (status, str(recv_amount) if recv_amount not in (None, "") else None,
                 int(time.time()), order_id),
            )
            conn.commit()
    except Exception:  # noqa: BLE001
        pass  # nosec B110


def public_config() -> dict:
    """Non-secret bridge config for /api/economics so the UI renders the same
    rules the server enforces."""
    # NB: RESERVE_WALLET is intentionally NOT exposed — it's recorded
    # server-side for reconciliation; the UI has no need for it.
    return {
        "bridgeMode": "api" if easybit.enabled() else "link",
        "bridgeMinUsd": MIN_ORDER_USD,
        "bridgeFeeUsd": FEE_USD,
        "bridgeBrand": BRAND,
    }
