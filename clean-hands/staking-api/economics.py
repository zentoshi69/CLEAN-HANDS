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

    effectiveAPR = baseAPR * (1 + amountBoost + loyaltyBoost + referralBoost)
                   + burnBonusAPR

    reward(dt) = stakedEffective * effectiveAPR * dt / year

Every booster is config below — tune freely. All values are *fractions*
(0.40 = 40%).
"""

from __future__ import annotations

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


@dataclass
class Apr:
    base: float
    amount_boost: float
    loyalty_boost: float
    referral_boost: float
    burn_bonus_apr: float
    effective_apr: float

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
) -> Apr:
    ab = amount_boost(staked_tokens)
    lb = loyalty_boost(seconds_staked)
    rb = referral_boost(active_referrals)
    bb = burn_bonus_apr(total_burned_tokens)
    eff = BASE_APR * (1 + ab + lb + rb) + bb
    return Apr(BASE_APR, ab, lb, rb, bb, eff)


def accrue(staked_effective: float, apr: float, dt_seconds: float) -> float:
    """Rewards earned over dt_seconds at the given APR. Never negative."""
    if staked_effective <= 0 or apr <= 0 or dt_seconds <= 0:
        return 0.0
    return staked_effective * apr * (dt_seconds / SECONDS_PER_YEAR)


def effective_staked(recorded: float, current_balance: float) -> float:
    """Anti-gaming: you only earn on what you still hold on-chain."""
    return max(0.0, min(recorded, current_balance))
