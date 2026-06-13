#!/usr/bin/env python3
"""
easybit.py — thin, defensive server-side client for the EasyBit exchange API.

This is the engine behind the "No Stains Bridge" tab. The API key lives ONLY on
the server: the browser/Mini App never sees it and never talks to EasyBit
directly — it talks to our own /api/bridge/* endpoints, which proxy to EasyBit
here. That's what makes the bridge a genuine white-label rather than a redirect.

Everything that could drift against the live spec is env-overridable so an
operator can re-point a path or header without a code change:

    EASYBIT_API_KEY          your EasyBit API key (REQUIRED to enable API mode)
    EASYBIT_API_BASE         default https://api.easybit.com
    EASYBIT_API_KEY_HEADER   default API-KEY
    EASYBIT_TIMEOUT          per-request timeout seconds (default 20)
    EASYBIT_PATH_CURRENCY    default /currencyList
    EASYBIT_PATH_PAIRINFO    default /pairInfo
    EASYBIT_PATH_RATE        default /rate
    EASYBIT_PATH_ORDER       default /order
    EASYBIT_PATH_ORDERSTATUS default /orderStatus
    EASYBIT_PATH_VALIDATE    default /validateAddress
    EASYBIT_EXTRA_FEE_PARAM  default extraFeeOverride

EasyBit wraps responses as {"success": 1, "data": {...}} on success and
{"success": 0, "errorMessage": "..."} on failure; we normalise both and raise
EasyBitError(message) with the provider's user-facing message on any failure.
Field access is defensive (multiple key fallbacks) so minor naming differences
in the live API don't hard-break the flow.
"""

from __future__ import annotations

import os
import time

import httpx


class EasyBitError(Exception):
    """A clean, user-safe failure from the exchange. `message` is shown to the
    user (e.g. "amount below minimum"); we never surface keys or stack traces."""

    def __init__(self, message: str, *, status: int = 502):
        super().__init__(message)
        self.message = message
        self.status = status


def _env(name: str, default: str) -> str:
    return (os.environ.get(name, "") or "").strip() or default


API_BASE = _env("EASYBIT_API_BASE", "https://api.easybit.com").rstrip("/")
API_KEY_HEADER = _env("EASYBIT_API_KEY_HEADER", "API-KEY")
EXTRA_FEE_PARAM = _env("EASYBIT_EXTRA_FEE_PARAM", "extraFeeOverride")
TIMEOUT = float(_env("EASYBIT_TIMEOUT", "20"))

_PATHS = {
    "currency": _env("EASYBIT_PATH_CURRENCY", "/currencyList"),
    "pairinfo": _env("EASYBIT_PATH_PAIRINFO", "/pairInfo"),
    "rate": _env("EASYBIT_PATH_RATE", "/rate"),
    "order": _env("EASYBIT_PATH_ORDER", "/order"),
    "orderstatus": _env("EASYBIT_PATH_ORDERSTATUS", "/orderStatus"),
    "validate": _env("EASYBIT_PATH_VALIDATE", "/validateAddress"),
}

# currencyList is large and changes rarely — cache it process-local.
_CURRENCY_TTL = int(_env("EASYBIT_CURRENCY_TTL", "600"))
_cache: dict[str, tuple[float, object]] = {}


def api_key() -> str:
    return (os.environ.get("EASYBIT_API_KEY", "") or "").strip()


def enabled() -> bool:
    """True when API mode is configured. When False the UI falls back to the
    existing external launch-card (link) mode — nothing breaks."""
    return bool(api_key())


def _headers() -> dict:
    return {
        API_KEY_HEADER: api_key(),
        "Accept": "application/json",
        "User-Agent": "no-stains-bridge/1.0",
    }


def _error_message(body: dict) -> str:
    """Best user-facing message from any of EasyBit's failure shapes."""
    for k in ("errorMessage", "message"):
        v = body.get(k)
        if v:
            return str(v)
    err = body.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("errorMessage") or "exchange rejected the request")
    if err:
        return str(err)
    return "exchange rejected the request"


def _unwrap(body: object) -> object:
    """Normalise EasyBit's envelope into the inner payload, or raise on error.

    Success:  {"success": 1, "data": {...}}  -> {...}
    Failure:  {"success": 0, "errorMessage": "..."} -> raise EasyBitError
    Some deployments omit the envelope and return the object directly; we accept
    that too rather than assuming a shape that might drift.
    """
    if not isinstance(body, dict):
        # A bare list/scalar is a valid payload for some endpoints.
        return body
    if "success" in body:
        if body.get("success") in (1, True, "1", "true"):
            return body.get("data", body)
        raise EasyBitError(_error_message(body), status=400)
    # No envelope: an explicit error field still counts as a failure.
    if body.get("error") or body.get("errorMessage"):
        raise EasyBitError(_error_message(body), status=400)
    return body.get("data", body)


async def _request(method: str, path_key: str, *, params: dict | None = None,
                   json: dict | None = None) -> object:
    if not enabled():
        raise EasyBitError("bridge is not configured", status=503)
    url = API_BASE + _PATHS[path_key]
    # Drop None-valued params/body fields so optional args don't become the
    # literal string "None" on the wire.
    if params:
        params = {k: v for k, v in params.items() if v is not None and v != ""}
    if json:
        json = {k: v for k, v in json.items() if v is not None and v != ""}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.request(method, url, params=params, json=json, headers=_headers())
    except httpx.TimeoutException:
        raise EasyBitError("the exchange timed out — try again", status=504)
    except httpx.HTTPError:
        raise EasyBitError("could not reach the exchange — try again", status=502)
    # Parse JSON regardless of status; EasyBit returns its error envelope with
    # non-2xx codes too, and that envelope carries the useful message.
    try:
        body = r.json()
    except ValueError:
        if r.status_code >= 400:
            raise EasyBitError("the exchange is unavailable — try again", status=502)
        raise EasyBitError("unexpected response from the exchange", status=502)
    try:
        return _unwrap(body)
    except EasyBitError as e:
        # Preserve the provider's message but cap the HTTP status sanely.
        if r.status_code in (401, 403):
            # An auth failure is OUR misconfiguration, not the user's fault.
            raise EasyBitError("the bridge is temporarily unavailable", status=502)
        raise e


# --------------------------------------------------------------------------- #
#  PUBLIC API                                                                  #
# --------------------------------------------------------------------------- #
async def currency_list(force: bool = False) -> list:
    """Supported currencies, each typically with a `networkList`. Cached."""
    now = time.time()
    hit = _cache.get("currencies")
    if hit and not force and (now - hit[0]) < _CURRENCY_TTL:
        return hit[1]  # type: ignore[return-value]
    data = await _request("GET", "currency")
    out = data if isinstance(data, list) else (data.get("currencies") if isinstance(data, dict) else [])
    out = out or []
    _cache["currencies"] = (now, out)
    return out


async def pair_info(send: str, receive: str, send_network: str = "",
                    receive_network: str = "") -> dict:
    """Min/max and network-fee info for a pair (amounts are in the SEND coin)."""
    data = await _request("GET", "pairinfo", params={
        "send": send, "receive": receive,
        "sendNetwork": send_network, "receiveNetwork": receive_network,
    })
    return data if isinstance(data, dict) else {}


async def rate(send: str, receive: str, amount: str, send_network: str = "",
               receive_network: str = "", extra_fee: float | None = None) -> dict:
    """Live estimate for `amount` of `send` -> `receive`. `extra_fee` (percent)
    is passed through so the quoted receiveAmount already reflects our fee."""
    params = {
        "send": send, "receive": receive, "amount": amount,
        "sendNetwork": send_network, "receiveNetwork": receive_network,
    }
    if extra_fee is not None:
        params[EXTRA_FEE_PARAM] = extra_fee
    data = await _request("GET", "rate", params=params)
    return data if isinstance(data, dict) else {}


async def create_order(*, send: str, receive: str, amount: str, receive_address: str,
                       send_network: str = "", receive_network: str = "",
                       receive_tag: str | None = None, refund_address: str | None = None,
                       refund_tag: str | None = None, extra_fee: float | None = None) -> dict:
    """Open a fixed exchange order. Returns the order incl. the DEPOSIT address
    the user must send funds to. Non-custodial: funds go user -> EasyBit ->
    receiveAddress; this app never holds them."""
    body = {
        "send": send, "receive": receive, "amount": amount,
        "sendNetwork": send_network, "receiveNetwork": receive_network,
        "receiveAddress": receive_address, "receiveTag": receive_tag,
        "refundAddress": refund_address, "refundTag": refund_tag,
    }
    if extra_fee is not None:
        body[EXTRA_FEE_PARAM] = extra_fee
    data = await _request("POST", "order", json=body)
    if not isinstance(data, dict) or not (data.get("id") or data.get("orderId")):
        raise EasyBitError("the exchange did not return an order — try again", status=502)
    return data


async def order_info(order_id: str) -> dict:
    """Current state of an order (status + amounts + tx hashes when available)."""
    data = await _request("GET", "orderstatus", params={"id": order_id})
    return data if isinstance(data, dict) else {}


async def validate_address(currency: str, address: str, network: str = "") -> bool:
    """True if EasyBit considers `address` valid for `currency`/`network`."""
    try:
        data = await _request("GET", "validate", params={
            "currency": currency, "address": address, "network": network,
        })
    except EasyBitError:
        # Treat a provider hiccup as "unknown" rather than blocking the user; the
        # order call itself will reject a truly bad address.
        return True
    if isinstance(data, dict):
        for k in ("result", "valid", "isValid", "success"):
            if k in data:
                return bool(data[k])
    return bool(data)


# --------------------------------------------------------------------------- #
#  FIELD HELPERS — tolerate naming variation in the live API                   #
# --------------------------------------------------------------------------- #
def pick(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def order_id_of(order: dict) -> str:
    return str(pick(order, "id", "orderId", default="") or "")


def deposit_address_of(order: dict) -> str:
    return str(pick(order, "sendAddress", "depositAddress", "payinAddress", "address", default="") or "")


def deposit_tag_of(order: dict):
    return pick(order, "sendTag", "depositTag", "payinTag", "tag", "memo")


def status_of(order: dict) -> str:
    return str(pick(order, "status", "state", default="") or "")
