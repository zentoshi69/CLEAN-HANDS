"""Market-maker liquidity booster — unit tests for the pure economics curve,
its APR integration, and the on-chain deposit parser (mocked RPC, no network)."""

import asyncio

import economics as econ
import solana


def test_liquidity_boost_curve():
    assert econ.liquidity_boost(0) == 0.0
    assert econ.liquidity_boost(econ.MM_MIN_USD - 1) == 0.0          # below the floor
    assert econ.liquidity_boost(econ.MM_MIN_USD) > 0.0              # qualifies at the floor
    # linear in dollar size: $250 -> 0.70 * 250/500 = 0.35
    assert abs(econ.liquidity_boost(250) - 0.35) < 1e-9
    assert econ.liquidity_boost(econ.MM_MAX_USD) == econ.MM_LP_CAP   # cap at max
    assert econ.liquidity_boost(10 * econ.MM_MAX_USD) == econ.MM_LP_CAP  # never exceeds cap


def test_effective_apr_includes_liquidity():
    # base 40%, no other boosts, full +0.70x liquidity -> 0.40 * (1 + 0.70) = 0.68
    a = econ.effective_apr(0, 0, 0, 0, liquidity_usd=econ.MM_MAX_USD)
    assert abs(a.liquidity_boost - econ.MM_LP_CAP) < 1e-9
    assert abs(a.effective_apr - econ.BASE_APR * (1 + econ.MM_LP_CAP)) < 1e-9
    # and it stacks additively with the other multiplier boosts + flat burn
    b = econ.effective_apr(2_000_000, 0, 0, 200_000, liquidity_usd=econ.MM_MAX_USD)
    assert abs(b.effective_apr - (econ.BASE_APR * (1 + 0.25 + econ.MM_LP_CAP) + 0.10)) < 1e-9


def test_verify_mm_deposit_parses_sol_and_clean(monkeypatch):
    WALLET = "Wa11et1111111111111111111111111111111111111"
    MM = "MMreserve111111111111111111111111111111111"
    MINT = "Mint111111111111111111111111111111111111111"
    fake_tx = {
        "meta": {
            "err": None,
            "preTokenBalances": [
                {"accountIndex": 5, "owner": MM, "mint": MINT, "uiTokenAmount": {"uiAmount": 0}}
            ],
            "postTokenBalances": [
                {"accountIndex": 5, "owner": MM, "mint": MINT, "uiTokenAmount": {"uiAmount": 1000.0}}
            ],
            "innerInstructions": [],
        },
        "transaction": {"message": {"instructions": [
            {"program": "system", "parsed": {"type": "transfer",
             "info": {"source": WALLET, "destination": MM, "lamports": 2_000_000_000}}},
        ]}},
    }

    async def fake_rpc(method, params):
        return fake_tx

    monkeypatch.setattr(solana, "_rpc", fake_rpc)
    sol, clean = asyncio.run(solana.verify_mm_deposit("sig", WALLET, MM, MINT))
    assert abs(sol - 2.0) < 1e-9        # 2 SOL to the reserve
    assert abs(clean - 1000.0) < 1e-9   # 1000 $CLEAN received by the reserve


def test_verify_mm_deposit_ignores_transfers_to_others(monkeypatch):
    WALLET = "Wa11et1111111111111111111111111111111111111"
    MM = "MMreserve111111111111111111111111111111111"
    OTHER = "Other11111111111111111111111111111111111111"
    fake_tx = {
        "meta": {"err": None, "preTokenBalances": [], "postTokenBalances": [], "innerInstructions": []},
        "transaction": {"message": {"instructions": [
            {"program": "system", "parsed": {"type": "transfer",
             "info": {"source": WALLET, "destination": OTHER, "lamports": 9_000_000_000}}},
        ]}},
    }

    async def fake_rpc(method, params):
        return fake_tx

    monkeypatch.setattr(solana, "_rpc", fake_rpc)
    sol, clean = asyncio.run(solana.verify_mm_deposit("sig", WALLET, MM, "m"))
    assert sol == 0.0 and clean == 0.0  # nothing went to the reserve


def test_verify_mm_deposit_rejects_failed_tx(monkeypatch):
    async def fake_rpc(method, params):
        return {"meta": {"err": {"InstructionError": [0, "x"]}}, "transaction": {"message": {"instructions": []}}}

    monkeypatch.setattr(solana, "_rpc", fake_rpc)
    sol, clean = asyncio.run(solana.verify_mm_deposit("sig", "w", "mm", "mint"))
    assert sol == 0.0 and clean == 0.0
