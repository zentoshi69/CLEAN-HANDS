#!/usr/bin/env python3
"""
CLEAN soft-staking economics — PURE functions, no I/O, fully unit-testable.

The single place where yield is defined. The website and the Telegram Mini App
both get identical numbers because they both ask this engine (via the API).

Model
-----
Soft staking = no lock. A wallet "stakes" by enrolling its $CLEAN balance; yield
accrues continuously on the *effective* staked amount (capped at the wallet's
current on-chain balance, so you can't stake then sell and keep earning).

    effectiveAPR = baseAPR * (1 + amountBoost + loyaltyBoost + referralBoost
                               + escapeBoost)
                   + burnBonusAPR

    reward(dt) = stakedEffective * effectiveAPR * dt / year

Every booster is config below — tune freely. All values are *fractions*
(0.40 = 40%).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict

SECONDS_PER_YEAR = 365 * 24 * 3600


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, default))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
#  CONFIG (defaults — override via env)                                        #
# --------------------------------------------------------------------------- #
BASE_APR = _f("STAKE_BASE_APR", 0.40)  # 40% base

# Amount tiers: hold/stake >= threshold (whole tokens) -> add this to the multiplier.
# Highest matching tier wins (not summed).
AMOUNT_TIERS = [
    (10_000_000, 0.50),
    (1_000_000, 0.25),
    (100_000, 0.10),
]

# Loyalty: +X per full 30 days staked, capped.
LOYALTY_PER_30D = _f("STAKE_LOYALTY_PER_30D", 0.05)
LOYALTY_CAP = _f("STAKE_LOYALTY_CAP", 0.50)

# Referral: +X per referred wallet that is actively staking, capped.
REFERRAL_PER = _f("STAKE_REFERRAL_PER", 0.02)
REFERRAL_CAP = _f("STAKE_REFERRAL_CAP", 0.30)

# Burn-to-boost: burning $CLEAN grants PERMANENT extra APR (added flat, not via
# the multiplier). +X APR per `BURN_UNIT` tokens burned, capped.
BURN_UNIT = _f("STAKE_BURN_UNIT", 100_000)
BURN_APR_PER_UNIT = _f("STAKE_BURN_APR_PER_UNIT", 0.05)
BURN_CAP_APR = _f("STAKE_BURN_CAP_APR", 2.00)

# Market-maker liquidity: a USD deposit (SOL + optional CLEAN) to the MM reserve
# grants an ADDITIVE multiplier boost (like the amount/loyalty/referral tiers),
# scaled with the dollar size — a floor (MM_MIN_USD) to qualify and a cap
# (MM_LP_CAP) reached at MM_MAX_USD. Deposit model; gated behind CLEAN_MM_WALLET.
MM_MIN_USD = _f("MM_MIN_USD", 50.0)
MM_MAX_USD = _f("MM_MAX_USD", 500.0)
MM_LP_CAP = _f("MM_LP_CAP", 0.70)

# Wallet-balance booster ("Clean Hands"): a wallet earns extra multiplier for
# simply HOLDING value — non-custodial, nothing is sent anywhere. SOL is
# mandatory; CLEAN is optional. The boost scales linearly across the USD band.
# Disable instantly with STAKE_BAL_BOOST=0.
BAL_BOOST_ENABLED = os.environ.get("STAKE_BAL_BOOST", "1").strip().lower() not in (
    "0", "false", "no", "off", "",
)
BAL_MIN_USD = _f("STAKE_BAL_MIN_USD", 50.0)         # need >= this worth of SOL to qualify
BAL_MAX_USD = _f("STAKE_BAL_MAX_USD", 500.0)        # qualifying USD is capped here
BAL_BOOST_AT_MIN = _f("STAKE_BAL_BOOST_MIN", 0.10)  # +0.10x at the floor
BAL_BOOST_AT_MAX = _f("STAKE_BAL_BOOST_MAX", 0.50)  # +0.50x at the cap

# Escape game booster: the game displays Escape as an income multiplier (×5,
# ×10, ×20, ×33). Once a Telegram-bound player reaches those milestones, staking
# power gets an additive multiplier boost. Highest matching tier wins; x33 is
# the hard cap and doubles staking-power rewards (+100%).
ESCAPE_TIERS = [
    (33.0, 1.00),
    (20.0, 0.50),
    (10.0, 0.33),
    (5.0, 0.20),
]


# --------------------------------------------------------------------------- #
#  BOOSTERS (pure)                                                             #
# --------------------------------------------------------------------------- #
def amount_boost(staked_tokens: float) -> float:
    for threshold, boost in AMOUNT_TIERS:  # tiers are ordered high -> low
        if staked_tokens >= threshold:
            return boost
    return 0.0


def loyalty_boost(seconds_staked: float) -> float:
    periods = int(max(0.0, seconds_staked) // (30 * 24 * 3600))
    return min(LOYALTY_CAP, periods * LOYALTY_PER_30D)


def referral_boost(active_referrals: int) -> float:
    return min(REFERRAL_CAP, max(0, active_referrals) * REFERRAL_PER)


def burn_bonus_apr(total_burned_tokens: float) -> float:
    units = max(0.0, total_burned_tokens) / BURN_UNIT
    return min(BURN_CAP_APR, units * BURN_APR_PER_UNIT)


def liquidity_boost(usd: float) -> float:
    """Market-maker liquidity deposit (USD) -> additive multiplier boost. Below
    MM_MIN_USD it doesn't qualify (0); above, it scales with size up to MM_LP_CAP."""
    usd = max(0.0, float(usd or 0.0))
    if usd < MM_MIN_USD or MM_MAX_USD <= 0:
        return 0.0
    return min(MM_LP_CAP, (usd / MM_MAX_USD) * MM_LP_CAP)


def wallet_balance_boost(sol_usd: float, clean_usd: float) -> float:
    """Extra multiplier for HOLDING value in your wallet (non-custodial).

    Rules:
      * SOL is MANDATORY — you need >= BAL_MIN_USD worth of SOL or you get
        nothing. You can qualify on SOL alone, but NEVER on CLEAN alone.
      * CLEAN is OPTIONAL — it only counts once it's itself >= BAL_MIN_USD, and
        its contribution is capped at BAL_MAX_USD.
      * Qualifying USD = SOL + counted CLEAN, clamped to BAL_MAX_USD.
      * Boost scales linearly from BAL_BOOST_AT_MIN (at BAL_MIN_USD) up to
        BAL_BOOST_AT_MAX (at BAL_MAX_USD).
    """
    if not BAL_BOOST_ENABLED:
        return 0.0
    sol_usd = max(0.0, float(sol_usd or 0.0))
    clean_usd = max(0.0, float(clean_usd or 0.0))
    if sol_usd < BAL_MIN_USD:  # SOL mandatory — never a CLEAN-only booster
        return 0.0
    counted_clean = clean_usd if clean_usd >= BAL_MIN_USD else 0.0
    counted_clean = min(counted_clean, BAL_MAX_USD)
    qualifying = min(sol_usd + counted_clean, BAL_MAX_USD)
    span = BAL_MAX_USD - BAL_MIN_USD
    frac = 1.0 if span <= 0 else (qualifying - BAL_MIN_USD) / span
    frac = max(0.0, min(1.0, frac))
    return BAL_BOOST_AT_MIN + frac * (BAL_BOOST_AT_MAX - BAL_BOOST_AT_MIN)


def escape_score_from_state(state: str | dict | None) -> float:
    """Derive the staking Escape score from the saved game state.

    Important: game_state.score is the leaderboard's lifetime-laundered value,
    not the Escape multiplier. Real rewards must use the actual prestige/escape
    multiplier shown in-game.
    """
    if not state:
        return 0.0
    try:
        data = json.loads(state) if isinstance(state, str) else state
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0.0
    if not isinstance(data, dict):
        return 0.0
    s = data.get("S") if isinstance(data.get("S"), dict) else data

    # Forward-compatible explicit fields if the game later saves the multiplier
    # directly.
    for key in ("escape_score", "escapeScore", "escape_multiplier", "escapeMultiplier"):
        try:
            val = float(s.get(key))
        except (TypeError, ValueError, AttributeError):
            continue
        if val > 0:
            return max(0.0, val)

    try:
        prestige = float(s.get("prestige") or 0)
    except (TypeError, ValueError, AttributeError):
        prestige = 0.0
    if prestige < 0:
        return 0.0
    # Matches webapp/play.html: escMult() = 1 + 0.75 * prestige.
    return 1.0 + 0.75 * prestige if ("prestige" in s or prestige > 0) else 0.0


def escape_boost(escape_score: float) -> float:
    score = max(0.0, float(escape_score or 0.0))
    for threshold, boost in ESCAPE_TIERS:  # tiers are ordered high -> low
        if score >= threshold:
            return boost
    return 0.0


# VIP: a verified market-maker deposit permanently locks the wallet to at least
# this total multiplier (and adds it to the VIP airdrop list). Default a clean 3x.
VIP_MULT = _f("STAKE_VIP_MULT", 3.0)


@dataclass
class Apr:
    base: float
    amount_boost: float
    loyalty_boost: float
    referral_boost: float
    liquidity_boost: float
    escape_score: float
    escape_boost: float
    burn_bonus_apr: float
    effective_apr: float
    wallet_boost: float = 0.0
    vip_boost: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        # also expose a friendly percentage for the UI
        d["effective_apr_pct"] = round(self.effective_apr * 100, 2)
        return d


def effective_apr(
    staked_tokens: float,
    seconds_staked: float,
    active_referrals: int,
    total_burned_tokens: float,
    liquidity_usd: float = 0.0,
    sol_usd: float = 0.0,
    clean_usd: float = 0.0,
    vip: bool = False,
    escape_score: float = 0.0,
) -> Apr:
    ab = amount_boost(staked_tokens)
    lb = loyalty_boost(seconds_staked)
    rb = referral_boost(active_referrals)
    qb = liquidity_boost(liquidity_usd)
    es = max(0.0, float(escape_score or 0.0))
    eb = escape_boost(es)
    bb = burn_bonus_apr(total_burned_tokens)
    wb = wallet_balance_boost(sol_usd, clean_usd)
    mult = 1 + ab + lb + rb + qb + wb + eb
    # A VIP deposit permanently tops the multiplier up to VIP_MULT (a clean 3x).
    vipb = max(0.0, VIP_MULT - mult) if vip else 0.0
    eff = BASE_APR * (mult + vipb) + bb
    return Apr(
        base=BASE_APR,
        amount_boost=ab,
        loyalty_boost=lb,
        referral_boost=rb,
        liquidity_boost=qb,
        escape_score=es,
        escape_boost=eb,
        burn_bonus_apr=bb,
        effective_apr=eff,
        wallet_boost=wb,
        vip_boost=vipb,
    )


def accrue(staked_effective: float, apr: float, dt_seconds: float) -> float:
    """Rewards earned over dt_seconds at the given APR. Never negative."""
    if staked_effective <= 0 or apr <= 0 or dt_seconds <= 0:
        return 0.0
    return staked_effective * apr * (dt_seconds / SECONDS_PER_YEAR)


def effective_staked(recorded: float, current_balance: float) -> float:
    """Anti-gaming: you only earn on what you still hold on-chain."""
    return max(0.0, min(recorded, current_balance))
