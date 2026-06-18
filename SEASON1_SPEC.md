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

### 4.1 Stake weighting — cap · booster · wallet limit

Allocation is pro-rata by **weighted** stake, not raw stake. The curve is *bracketed* (like tax
brackets) so there is **no cliff** at the booster threshold:

| Stake bracket | Weight |
|---|---|
| first 1,000,000 | 1.0× |
| 1,000,000 → 10,000,000 | **1.5× (booster)** |
| above 10,000,000 | **0 (hard cap)** |

```
weight = min(stake, 1_000_000)·1.0 + clamp(stake − 1_000_000, 0, 9_000_000)·1.5
```

Examples: 500k → 0.5M · 1M → 1.0M · 2M → 2.5M · 10M → **14.5M** · 20M → 14.5M (capped).
So 10× the bag earns ~14.5× the share — bigger stakes rewarded, the very top capped.

- **Per-wallet cap:** effective stake caps at **10,000,000** — staking beyond it earns nothing more.
- **Booster:** the **1M–10M** bracket is weighted **1.5×** (multiplier is a knob; default 1.5).
- **Wallet limit:** a user (Telegram identity) may link **max 20 wallets**.

**Known interaction (surfaced, not a bug):** 20 wallets × 10M cap = **200M effective max per user**
(20% of supply). The wallet limit is *soft* — tied to Telegram login, so it raises sybil cost but a
determined user could make more accounts. Net: friction, not a hard wall. Acceptable because 200M
staked from one user is unrealistic and self-defeating (buy pressure + their own rate compresses);
no hard per-user cap is added (it would invite custody/KYC-flavored complexity for a threshold
nobody reaches).

This shifts $CLEAN toward mid/large stakers; small holders stay protected on **access** (unlocks are
milestone-gated, §4) even as their token slice shrinks.

### 4.2 Commitment ladder — burn (3×) + Founder Pass (5×)

Beyond the soft-stake bracket, two opt-in tiers reward deeper commitment. All three feed the **same
fixed pool**, so nothing here changes the bounded payout — they only re-weight the split.

| Tier | Action | Weight |
|---|---|---|
| Soft-stake | hold in wallet (reversible) | 1.0× (first 1M) · 1.5× (1M–10M) · capped at 10M |
| 🔥 **Burn** | permanently destroy tokens | **3× on burned amount** — deflationary, no custody |
| 🎟️ **Founder Pass** | one-time **purchase** of a membership | **5× multiplier** on stake weight + perks bundle |

**Founder Pass is a product, not a deposit** — that single distinction is what keeps it clear of
Howey:
- One-time, **non-refundable PURCHASE** at a fixed price → treasury **revenue**, not a deposit owed
  back. **No redemption, no withdrawal, no promised return.**
- Bundles **real consumptive perks** — Founder allowlist priority, exclusive game cosmetics,
  exclusive music/fashion drops, Founder badge — and the 5× multiplier is *one* perk, not the point.
  Copy leads with the perks/access, **never** "deposit and earn."
- **Honesty guardrail:** if the Pass were sold purely as "pay for 5× yield," a regulator could look
  through the form to the substance. So the perk bundle must carry genuine standalone value and the
  copy must say so. Limited supply (scarcity) reinforces the product framing.

**Stacking + cap (build-time knob, conservative default):** multipliers apply to the **bracketed,
capped** base (≤10M effective stake). Default: burn is **additive** (`+3×burned`), the Pass
**multiplies the stake-weight lane only** — they do *not* compound into one runaway multiplier.
Because the pool is fixed, even a dominant Pass-whale only shifts the *token* split — small holders
keep all **access** unlocks (milestone-gated, §4). Exact stacking finalized in build under `/verify`.

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
  weighting: {
    bracket: [ { upTo: 1_000_000, mult: 1.0 }, { upTo: 10_000_000, mult: 1.5 } ],
    walletCapEffective: 10_000_000,   // per-wallet effective stake cap
    maxWalletsPerUser: 20,
    burnMult: 3,                      // additive: +3 × burned
    founderPassMult: 5               // multiplies stake-weight lane (product, not deposit)
  },
  founderPass: { price: "TBD", supply: "limited", perks: ["allowlist","cosmetics","drops","badge","5x"] },
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
| **MM deposit flow** (`/api/mm/*`) | **repurposed** as the Founder Pass product-sale rail — one-way, non-refundable **purchase** (revenue), not a deposit (§4.2) |

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

1. **Season engine** — Season config loader, fixed daily pool (333,333/day), **weighted** pro-rata
   allocation (§4.1 cap + booster + 20-wallet limit).
2. **Snapshot soft-stake** — sign-to-enter, daily balance snapshot, continuous-streak tracking.
3. **Staged unlocks** — checkpoint gates at day 0 / 30 / 60, forfeit-on-early-unstake.
4. **Commitment ladder** — 🔥 burn (3× additive) + 🎟️ Founder Pass (5× perk). Repurpose `/api/mm/*`
   as the Founder Pass **purchase** rail (one-way, non-refundable, revenue not deposit) + perks bundle.
5. **Honesty pass** — retire APR/VIP/airdrop copy, add the §8 line.
6. **Audit gate** — `/security-review` + `/code-review` + `/verify` on the diff before push.

Open knob for §3: fashion staged-discount tier breakpoints (allocation → discount %) — TBD, configurable.
