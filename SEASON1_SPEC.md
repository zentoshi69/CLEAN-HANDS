# Clean Season 1 — Spec (source of truth)

> Status: **signed off** (pool, MM, day-0 unlock all locked) · Supersedes "Phase 1 relabel" in `UPGRADE-PLAN.md`.
> One program. 90 days. Snapshot soft-stake. Fixed reward pool. Rewards = cross-pillar access, **not** a yield.

This spec replaces the `APR`-promise / `VIP 3×` / `airdrop-list` mechanics with a finite,
fixed-pool **season** model that ties staking to the brand's three pillars
(**DIGITAL** = AI influencer / game / music · **WEB3** = memecoin / dapp / TG bots · **PHYSICAL** = fashion / items / collabs).

---

## 1. Why this model (and not "40% APR")

The legal/honesty risk was never *soft-staking* — it was **promising a rate**. A fixed daily
allocation pool is the honest version of the same dopamine:

| "40% APR" (retired) | "Per-day allocation pool" (this spec) |
|---|---|
| Implies a guaranteed yield you must fund | A fixed pot split among whoever shows up |
| Return is a *promise* → investment-contract smell | Return is an *outcome* of participation |
| Can become insolvent against the promise | Pays exactly the pool — never over-committed |

The reward is also **consumptive utility** (game/music/fashion access), not just more tokens —
which is both the safer footing and the better product.

---

## 2. The pool (locked)

- **Season 1 pool = 3% of total supply.**
- Total supply = **1,000,000,000** (`STAKE_TOTAL_SUPPLY`, pump.fun standard).
- **Pool = 30,000,000 $CLEAN.**
- **Daily emission = 30,000,000 ÷ 90 = 333,333 $CLEAN/day**, split pro-rata by each staker's
  share of total staked that day.
- Trajectory: 3%/season × 4 seasons/yr ≈ **12% of supply/yr** to stakers — sustainable.

**Prerequisite (honesty):** the 30M must be **reserved in a dedicated treasury wallet** before the
season opens, so every reward is actually payable. No reserve = no season.

---

## 3. Season 1 timeline — staged unlocks every 30 days

```
 DAY 0 ───────────── DAY 30 ──────────── DAY 60 ──────────── DAY 90
 │                   │                   │                   │
 ENTER + 🎮          🎵 MUSIC DROP       👕 FASHION          SEASON CLOSE
 GAME PACKAGE        first track / NFT   pre-sale allowlist  allocations finalize
 (beta, day-0)       for stakers         + staged discount   Pass locks · S2 announced
 daily pool starts
```

- **Day 0 — 🎮 Game package (beta):** granted on entry. Game launches with the season, so the
  entry reward is instant.
- **Day 30 — 🎵 Music drop:** first track / NFT, stakers only.
- **Day 60 — 👕 Fashion pre-sale:** allowlist + staged discount (depth scales with allocation tier).
- **Day 90 — Close:** allocations finalize, $CLEAN claimable, Season Pass locks, Season 2 announced.

**Hold incentive:** you must stay staked **continuously** to reach each checkpoint. Unstake early →
forfeit the unlocks you haven't reached yet. A whale who dumps at day 20 keeps the game but loses
music + fashion.

---

## 4. Two things you earn (kept separate)

| | Set by | Used for |
|---|---|---|
| **Allocation** (how much) | daily pool ÷ stakers, pro-rata by stake size | your $CLEAN reward **and** your *tier* (deeper fashion discount, higher allowlist priority) |
| **Unlocks** (what you get) | continuous-staking milestones (0 / 30 / 60 / 90) | gates game → music → fashion |

A small holder who stays all 90 days still gets all three unlocks (smaller allocation, lighter
discount tier). Conviction is rewarded over size.

---

## 5. Mechanic — snapshot soft-stake (no custody)

1. **Enter:** sign a message ("join Clean Season 1"). Proves the wallet, **transfers nothing.**
2. **Snapshots:** daily balance check. Hold ≥ entry amount → streak continues + allocation accrues.
   Balance drops below → streak breaks (anti-game enforcement).
3. **Accounting:** off-chain — tracks `accrued allocation` + `continuous days`. Nothing deposited →
   nothing to hack, nothing to withdraw, can't be insolvent.
4. **Claim:** unlocks delivered at checkpoints; $CLEAN allocation claimable at close.

---

## 6. Built for Season 2 from day one

Everything reads from a **Season config** object → Season 2 = new config, zero rebuild:

```jsonc
Season {
  id: 1,
  poolPct: 0.03,                 // 3% of supply
  durationDays: 90,
  checkpoints: [
    { day: 0,  pillar: "game",    reward: "package-beta" },
    { day: 30, pillar: "music",   reward: "drop-1" },
    { day: 60, pillar: "fashion", reward: "presale-allowlist + staged-discount" }
  ],
  discountTiers: [ /* allocation → discount depth + allowlist priority */ ]
}
```

Season 2 → new %, new duration, new checkpoints, new feature. Swap the config.

---

## 7. What this retires

| Current code | Becomes |
|---|---|
| `_apr_for` / `econ.effective_apr` / "40% APR" (`app.py`) | daily pool share (fixed pot, no promised rate) |
| "VIP 3×" | "Founder" entry perk (priority, no guaranteed multiplier) |
| "airdrop list" | Season Pass unlocks (consumptive utility, not token-for-deposit) |
| **MM deposit flow** (`/api/mm/*`) | **removed** (locked decision) |

API paths / DB columns / function names that aren't user-facing may stay internally to avoid a
migration; only the **mechanic + display** change.

---

## 8. The one honesty line (stake screen + whitepaper)

> *Clean Season is a discretionary loyalty program with a fixed reward pool. Rewards are ecosystem
> access (game / music / fashion), not an investment — your tokens never leave your wallet, nothing
> is guaranteed. This project is satire; don't actually launder money.*

That sentence + the fixed-pool model removes ~all of the legal exposure the audits flagged.

---

## 9. Build scope (next, after #75 merges)

1. **Season engine** — Season config loader, fixed daily pool (333,333/day), pro-rata allocation.
2. **Snapshot soft-stake** — sign-to-enter, daily balance snapshot, continuous-streak tracking.
3. **Staged unlocks** — checkpoint gates at day 0 / 30 / 60, forfeit-on-early-unstake.
4. **Kill MM** — remove `/api/mm/*` flow + UI.
5. **Honesty pass** — retire APR/VIP/airdrop copy, add the §8 line.
6. **Audit gate** — `/security-review` + `/code-review` + `/verify` on the diff before push.

Open knob for §3: fashion staged-discount tier breakpoints (allocation → discount %) — TBD, configurable.
