#!/usr/bin/env python3
"""
test_bridge.py — unit tests for the No Stains Bridge (white-label EasyBit).

Deliberately light on deps: imports only easybit/bridge/db (httpx + sqlite), so
it runs in CI with just requirements.txt + pytest. The exchange network layer
(easybit._request) is stubbed, so nothing here touches the network.

Async paths are driven with asyncio.run() so we don't need pytest-asyncio.
"""

from __future__ import annotations

import asyncio
import pytest

import easybit
import bridge
import db


# --------------------------------------------------------------------------- #
#  F1 — the flat $5 fee must hold across order sizes (the round(pct,2) bug)     #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("usd", [55, 100, 500, 5_000, 100_000, 500_000, 1_000_000, 5_000_000])
def test_fee_is_flat_five_across_sizes(usd):
    pct, fee = bridge.fee_for(usd)
    # Above the minimum the effective fee is the flat target to within a cent —
    # this is exactly what broke at $100k/$500k with 2-dp rounding.
    assert abs(fee - bridge.FEE_USD) < 0.01, (usd, pct, fee)


def test_fee_clamped_below_minimum():
    # Below the min the percentage is capped, so the collected fee is < target.
    pct, fee = bridge.fee_for(10)
    assert pct <= bridge.MAX_FEE_PCT
    assert fee < bridge.FEE_USD


def test_fee_pct_precision_is_not_two_dp():
    # Regression guard for F1: a $1M order needs 0.0005%, which 2 dp would
    # destroy. The configured precision must preserve it.
    pct, _ = bridge.fee_for(1_000_000)
    assert pct > 0  # would be 0.00 under 2-dp rounding


# --------------------------------------------------------------------------- #
#  Input sanitisation                                                          #
# --------------------------------------------------------------------------- #
def test_norm_coin_ok_and_reject():
    assert bridge.norm_coin("sol") == "SOL"
    for bad in ["", "../etc", "a" * 17, "BTC!", "b c"]:
        with pytest.raises(bridge.BridgeError):
            bridge.norm_coin(bad)


def test_norm_amount_rejects_bad():
    assert bridge.norm_amount("55") == "55"
    assert bridge.norm_amount("0.0001") == "0.0001"
    for bad in ["-1", "0", "1e9", "abc", "", "1.2.3", "99999999999999"]:
        with pytest.raises(bridge.BridgeError):
            bridge.norm_amount(bad)


def test_norm_address_and_orderid():
    assert bridge.norm_address("SoLAddr1111111111111111111111") .startswith("SoL")
    with pytest.raises(bridge.BridgeError):
        bridge.norm_address("short")
    with pytest.raises(bridge.BridgeError):
        bridge.norm_address("bad address with spaces!!")
    assert bridge.norm_order_id("EB_123-abc") == "EB_123-abc"
    with pytest.raises(bridge.BridgeError):
        bridge.norm_order_id("x" * 200)


# --------------------------------------------------------------------------- #
#  EasyBit envelope normalisation (the _unwrap message-loss bug)               #
# --------------------------------------------------------------------------- #
def test_unwrap_success_and_bare():
    assert easybit._unwrap({"success": 1, "data": {"x": 1}}) == {"x": 1}
    assert easybit._unwrap([1, 2, 3]) == [1, 2, 3]


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"success": 0, "errorMessage": "amount below minimum"}, "amount below minimum"),
        ({"success": 0, "message": "pair not supported"}, "pair not supported"),
        ({"success": 0, "error": "bad key"}, "bad key"),
        ({"success": 0, "error": {"message": "deep msg"}}, "deep msg"),
        ({"success": 0}, "exchange rejected the request"),
    ],
)
def test_unwrap_surfaces_error_message(body, expected):
    with pytest.raises(easybit.EasyBitError) as ei:
        easybit._unwrap(body)
    assert ei.value.message == expected


# --------------------------------------------------------------------------- #
#  Status -> phase mapping                                                     #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "status,phase",
    [
        ("Awaiting Deposit", "waiting"),
        ("Confirming", "confirming"),
        ("Exchanging", "exchanging"),
        ("Sending", "sending"),
        ("Complete", "done"),
        ("Refund", "refund"),
        ("Failed", "failed"),
        ("Volatility Protection", "action"),
        ("", "unknown"),
    ],
)
def test_phase_of(status, phase):
    assert bridge.phase_of(status) == phase


# --------------------------------------------------------------------------- #
#  Async flow with a stubbed exchange                                          #
# --------------------------------------------------------------------------- #
PRICES = {"SOL": 150.0, "ETH": 3000.0, "USDC": 1.0, "USDT": 1.0}


def _make_stub(captured):
    async def fake_request(method, path_key, *, params=None, json=None):
        if path_key == "rate":
            s, r = params["send"], params["receive"]
            amt = float(params["amount"])
            return {
                "rate": PRICES.get(s, 1) / PRICES.get(r, 1),
                "receiveAmount": round(PRICES.get(s, 1) * amt / PRICES.get(r, 1), 8),
                "networkFee": 0.0001,
            }
        if path_key == "pairinfo":
            return {"minimumAmount": "0.01", "maximumAmount": "1000"}
        if path_key == "validate":
            return {"result": True}
        if path_key == "order":
            captured["order_body"] = json
            return {
                "id": "EBTEST",
                "sendAddress": "DEP_ADDR",
                "sendAmount": json.get("amount"),
                "receiveAmount": "0.5",
                "status": "Awaiting Deposit",
            }
        if path_key == "orderstatus":
            return {"id": params["id"], "status": "Complete", "receiveAmount": "0.5"}
        raise AssertionError(path_key)

    return fake_request


def test_quote_enforces_minimum(monkeypatch):
    bridge._price_cache.clear()
    monkeypatch.setattr(easybit, "_request", _make_stub({}))
    # 0.1 SOL ~ $15 < $55 -> rejected
    with pytest.raises(bridge.BridgeError):
        asyncio.run(bridge.quote("SOL", "ETH", "0.1"))
    # 1 SOL ~ $150 -> ok, fee ~ $5
    q = asyncio.run(bridge.quote("SOL", "ETH", "1"))
    assert q["priced"] and abs(q["feeUsd"] - bridge.FEE_USD) < 0.05


def test_create_forwards_fee_param_and_persists(monkeypatch, tmp_path):
    bridge._price_cache.clear()
    captured = {}
    monkeypatch.setattr(easybit, "_request", _make_stub(captured))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "bridge.db"))
    db.init_db()

    o = asyncio.run(
        bridge.create(
            send="SOL", receive="ETH", amount="1",
            receive_address="0xAbC1234567890def1234567890",
        )
    )
    # F2 guard: the per-order fee override must actually be on the wire.
    body = captured["order_body"]
    assert easybit.EXTRA_FEE_PARAM in body
    expected_pct = 5.0 / 150.0 * 100.0
    assert abs(float(body[easybit.EXTRA_FEE_PARAM]) - expected_pct) < 1e-3

    # And the order is recorded for reconciliation.
    assert o["orderId"] == "EBTEST" and o["depositAddress"] == "DEP_ADDR"
    with db.db() as conn:
        row = conn.execute(
            "SELECT order_id, send_coin, recv_coin, fee_usd FROM bridge_orders WHERE order_id=?",
            ("EBTEST",),
        ).fetchone()
    assert row is not None
    assert row["send_coin"] == "SOL" and row["recv_coin"] == "ETH"
    assert abs(row["fee_usd"] - bridge.FEE_USD) < 0.05


def test_create_rejects_same_coin(monkeypatch):
    bridge._price_cache.clear()
    monkeypatch.setattr(easybit, "_request", _make_stub({}))
    with pytest.raises(bridge.BridgeError):
        asyncio.run(
            bridge.create(send="SOL", receive="SOL", amount="1",
                          receive_address="SoLAddr1111111111111111111111")
        )
