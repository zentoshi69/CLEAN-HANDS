# CLEAN HANDS — Production Upgrade Plan (→ 9/10 ship-ready)

> **Status:** awaiting approval. No implementation code has been written yet.
> **Branch:** `claude/cool-tesla-vut940` (fast-forwarded to current `main` `ec36484`, so it
> already contains #62–#65).
> **Scope chosen:** whole platform. **Game direction chosen:** keep the *live* game
> (`clean-hands/staking-api/webapp/play.html`) and **add the missing features** to it.
> **Mode:** plan-first — this doc lands, you approve, then I execute phase-by-phase,
> each phase its own CI-gated draft PR.

---

## 0. Where we actually are (verified against `origin/main`, not the stale branch)

The previous session's audit was run on a branch cut before #62–#65 merged. Re-audited
against live `main`:

**Already fixed on `main` (do NOT re-do):** heat-per-tap `2.2`, no passive heat gain,
cooldown `3.0/s`, bust = lose 18% cash & always recoverable (no game-over wipe), heat
resets to 35 — all landed in **#64** (`play.html`) and **#63** (`standalone.html`).

**Still broken / missing on live `main` (this plan's target):**

| ID | Item | Type | Phase |
|----|------|------|-------|
| G1 | Ghost Protocol label "+5% crit" but code gives +3% (`0.018 + ghost*0.03`) | Bug | 1 |
| G2 | Hand Sanitizer label "−12% heat/count" but code subtracts flat `0.012` | Bug | 1 |
| G3 | `standalone.html` & `play.html` are hand-synced and already drifted | Maintenance | 1 |
| G4 | Game POSTs `/api/track` + GETs `/api/ref` → neither exists server-side (404, silent) | Dead feature | 2 |
| G5 | No cloud save — `localStorage` only; progress lost on reinstall / new device | Missing feature | 2 |
| G6 | No "Most Wanted" leaderboard (backbone had it) | Missing feature | 2/3 |
| G7 | Escape panel is vanity: `floor(total/1000)` score, fake "Next City", one-shot `perTap×1.15` | Missing feature | 3 |
| W1 | **Mobile wallet connect doesn't sign** (PR #51 open, unmerged) | **Prod blocker** | 4 |
| B1 | `/meme` hard-timeout hardening (PR #60 open) | PR cleanup | 5 |
| B2 | `/meme` Gemini engine swap (PR #55 draft) | PR cleanup | 5 |
| D1 | Third game lineage `clean-hands.html` (PR #53 draft) competes with live game | Decision | 6 |

---

## 1. Guiding principles (non-negotiable)

1. **Never break a working money path.** Staking, payments, bridge, MM all have tests
   (`test_staking.py`, `test_bridge.py`, `test_mm.py`) + a security audit. Every phase
   that touches `staking-api` must keep all of them green.
2. **One game.** We converge on `play.html`. `game.js` (backbone) stays as the
   reference spec only. PR #53's lineage is closed (good ideas salvaged as notes).
3. **Additive & reversible.** New DB tables/routes are additive migrations; new game
   features degrade gracefully when the backend is unreachable (offline-first, exactly
   like today).
4. **CI is the gate.** Local run of the full `ci.yml` matrix before every push:
   `pytest test_staking.py test_bridge.py`, `py_compile` all, `node --check`,
   `test_wallet_flow.mjs`, `test_tg_e2e.mjs`, `/meme` tests, glove-bot tests,
   `pip-audit`, gitleaks.
5. **Per-phase draft PRs.** Small, reviewable, independently revertable. Nothing
   force-pushed to `main`.

---

## 2. Phased plan

### Phase 0 — Stabilize workspace ✅ (done, zero behavior change)
- **0.1** Fast-forwarded `claude/cool-tesla-vut940` onto `origin/main` → now has #62–#65.
- **0.2** Re-audited live state (section 0 above).
- **0.3 (on execute):** capture a green CI baseline locally before any change.

---

### Phase 1 — Game correctness & de-duplication (low risk)
**Goal:** fix the two label bugs and kill the double-file hazard. No economy/feel change.

- **1.1 Ghost Protocol label (G1).** Decide the source of truth, then make label and
  code agree:
  - *Recommended:* keep code (+3%/level) and change the displayed `per` for `ghost`
    from `5` → `3` in `perkRow()` (`play.html` ~L1466).
  - *(If you'd rather the perk actually give +5%, change `critChance()` `0.03→0.05` —
    note this also makes crits ~67% more frequent; balance impact.)*
- **1.2 Hand Sanitizer label (G2).** The effect is a flat `−0.012` off the `0.05`
  per-tap heat base. Replace the misleading "−12% heat/count" with an honest label,
  e.g. "cools tap-heat" or a true per-level % computed from the real numbers. Align
  `perkRow()` `per` value + the `eff` template.
- **1.3 Single-source the game (G3).** Make `play.html` canonical; regenerate
  `standalone.html` from it via `build_standalone.py` (or reduce `standalone.html` to a
  generated artifact). **Add a CI step** that fails if the two diverge, so this never
  rots again.
- **Verify:** `node --check` on inlined scripts; headless boot smoke (no console
  errors); the two files byte-match per the new CI guard.
- **Risk:** trivial. **Rollback:** revert the PR.

---

### Phase 2 — Wire the game's backend (make dead features real)
**Goal:** the calls the game already makes should *work*; add cloud save; lay the table
the leaderboard needs. All server-side, additive.

- **2.1 DB migration v9 (`db.py`).** Add, guarded by `PRAGMA user_version` (current max
  = 8 → bump to 9), idempotent:
  ```sql
  CREATE TABLE IF NOT EXISTS game_state (
    wallet      TEXT PRIMARY KEY,
    state       TEXT NOT NULL,      -- opaque client JSON blob (size-capped)
    score       INTEGER NOT NULL,   -- lifetime laundered, for ranking
    updated_ts  INTEGER NOT NULL
  );
  ```
  (This is the schema the backbone README already prototyped as "v9".)
- **2.2 Routes (`app.py`), Telegram-`initData`/token authed via existing `_require`:**
  - `POST /api/game/save  {token, state, score}` → upsert, validate, **size-cap** the
    blob (reject > N KB), clamp score ≥ 0.
  - `POST /api/game/load  {token}` → return `{state, score}` or empty.
  - `GET  /api/game/leaderboard?limit=` → top N by `score` (laundered), with
    `username`/short-wallet, mirroring the existing `/api/leaderboard` shape.
  - **Implement `/api/track`** (G4): accept the analytics beacon, write to a lightweight
    table or log sink, return 204. (Cheap; stops silent 404s.)
  - **Fix `/api/ref`** (G4): either add a `GET /api/ref` alias that records the referral,
    or repoint the client's `fetch('/api/ref?...')` to the existing `POST /api/referrals`.
    *Recommended:* repoint client → one real endpoint, less surface.
- **2.3 Client identity bridge (`app.js` ↔ `play.html`).** The game runs framed and does
  **not** currently read `initData`. Reuse the backbone's existing `postMessage` seam:
  - Host `app.js` `loadGame()` posts `{type:'clean:auth', token}` (the session token it
    already holds) into the iframe once loaded.
  - `play.html` listens, stores the token, and uses it for `/api/game/save|load|leaderboard`.
  - **Graceful fallback:** no token (opened directly at `/play`, not in Telegram) →
    leaderboard is read-only and save stays `localStorage`-only, exactly like today.
- **2.4 Client save/load (`play.html`).** On boot: `localStorage` first (instant), then
  `/api/game/load`; take the **most-recently-updated** of the two (same merge rule the
  backbone used via `lastSeen`). Debounced `/api/game/save` on the existing autosave +
  on `visibilitychange`.
- **Verify:** new `app.py` tests (save/load round-trip, size-cap rejection, score clamp,
  leaderboard order, unauth rejection) added to `test_staking.py`; `node --check`;
  framed-vs-direct identity smoke. All existing tests stay green.
- **Risk:** medium (touches `app.py`/`db.py`). Mitigation: additive only, no change to
  staking/payment paths; migration is `CREATE TABLE IF NOT EXISTS`.
- **Rollback:** routes + table are isolated; revert PR, table lies dormant & harmless.

---

### Phase 3 — Real Escape / prestige + Most Wanted (the headline missing feature)
**Goal:** replace the vanity escape with a true prestige loop and surface the leaderboard,
**reusing the backbone's exact model**, adapted to `play.html`'s level/city system.

- **3.1 Decouple lifetime from run-total (engine).** Add `S.lifetime` (all-time, **never
  reset**), incremented alongside `S.cash`/`S.total` on every earn. `S.total` stays the
  level/city driver; `S.lifetime` drives the leaderboard score + "Escape Score". This
  mirrors backbone, where `lifetime` survived prestige.
- **3.2 Prestige multiplier (engine).** Add `S.prestige` (count). In `recompute()`,
  multiply final `perTap` **and** `perSec` by `escMult = 1 + 0.75 * S.prestige`
  (backbone ×1.75/level). Show `escMult` in the strip/escape card.
- **3.3 Escape action (replaces the one-shot `perTap×1.15`).** Gate: `S.total ≥
  CITIES[cityIdx].goal` **and** `S.heat < 40` (keep the heat gate — it's a nice tension
  beat). Effect: `prestige++`, reset `cash/total/heat/bribes/upgrades` to fresh, **keep**
  `lifetime`, apply `escMult`, advance the prestige city label, confetti + toast
  "Relocated — ×N forever".
  - **DECISION D-A (flagged):** do perks & claimed REP reset on escape, or persist?
    Backbone reset upgrades+bribes but kept perks one-time. *Recommended:* keep perks &
    REP, reset upgrades/bribes/cash/heat (most player-friendly, least grindy).
- **3.4 Fix the panel semantics (G7).** "Escape Score" → `fmt(S.lifetime)`; "Next City"
  shows the real prestige destination; CTA reflects the prestige gate. Remove the
  spammable `perTap×1.15`.
- **3.5 Most Wanted (G6).** Render the `/api/game/leaderboard` top-N inside the Escape
  panel (backbone's "Most Wanted" block), with the player's own rank if authed.
- **Verify:** sim-test progression (prestige 0→1→2: multiplier applies, lifetime
  persists, total resets, city advances); leaderboard renders framed; `node --check`.
- **Risk:** medium (core engine: `recompute`, earn paths, `resetRun`). Mitigation: all
  changes are additive fields + one multiply; existing save blobs upgrade safely
  (missing `lifetime`/`prestige` default to derived/0 via the existing `safe()`/defaults).
- **Rollback:** revert PR; old saves still load (extra fields ignored).
- **OPTIONAL 3.6 — economy retune toward backbone curve.** Live game has 5 upgrades vs
  backbone's 9, different cost growth, bribe-as-shield vs instant-cut. You asked to *add
  missing features*, not retune — so this is **opt-in**. If you want it, it's a separate
  sub-PR with a progression sim before/after. *Default: skip.*

---

### Phase 4 — Mobile wallet connect (PR #51) — the real production blocker (W1)
**Goal:** mobile users can actually connect & sign. This is the biggest 9/10 gap.

- **4.1** Rebase PR #51's branch (`claude/cool-dijkstra-cdv08v`) onto current `main`;
  resolve conflicts (it predates several merges).
- **4.2** Re-review the change: client `loginTg()/pollTg()` driving the server
  `/api/tg/start → connect → sign → poll` handshake, the `renderWallets` primary path,
  boot recovery, `freshInitData()`, the `[login]`/`[ratelimit]` logging, and the
  `nonce`→`tg` rate-limit bucket move.
- **4.3** Verify: `test_staking.py` (incl. `tg server handshake`), `test_mm.py`,
  `test_bridge.py`, `test_wallet_flow.mjs`, `test_tg_e2e.mjs` all green; `node --check
  app.js`; bump cache-bust `?v=21`.
- **DECISION D-B (flagged):** ship #51 as-is after rebase, or fold it into this program's
  branch. *Recommended:* rebase & finish #51 on its own branch, merge first (it's the
  highest-value fix), then continue.
- **Risk:** high-value, medium-risk (auth path). Mitigation: it's already test-backed and
  was a forensic fix; we only rebase + re-verify, not rewrite.

---

### Phase 5 — Bot `/meme` consolidation (B1, B2)
**Goal:** one coherent `/meme` path; no overlapping/competing PRs.

- **5.1 PR #60 (hard timeouts).** Branched *after* #58 merged, so it's complementary
  (request timeouts + `wait_for` guard on `send_photo`). Rebase, confirm it doesn't
  re-introduce the spinner #58 removed, run `test_meme_timeout.py`, merge or close with
  rationale.
- **5.2 PR #55 (Gemini engine).** **DECISION D-C (flagged):** swap `/meme` to Gemini 2.5
  Flash Image (≈4–5× cheaper, faster) or stay on the current engine?
  - If yes: rebase onto `main`, reconcile with #58's anti-stuck guarantees, run the
    extended timeout suite, set `GEMINI_API_KEY` in deploy notes.
  - If no: close #55 with a note.
- **Risk:** low (bot-only, isolated, well-tested). **Rollback:** revert; local-stamp
  fallback always works.

---

### Phase 6 — Converge the game lineage (D1 / PR #53)
**Goal:** stop maintaining three games.

- **6.1** Close PR #53. Mine it first for any feature worth porting into `play.html`
  (e.g. flee-confirm modal, dramatic raid overlay, achievements grid, offline-return
  card) and log them as backlog items — implement only the ones you greenlight.
- **DECISION D-D (flagged):** any #53 elements you specifically want pulled into the live
  game? Default: none beyond what Phase 3 already adds.

---

### Phase 7 — Hardening & the 9/10 gate
**Goal:** prove production-ready, ship cleanly.

- **7.1** Full `ci.yml` matrix green locally + on each PR.
- **7.2** `pip-audit` clean across all three requirement sets; gitleaks clean.
- **7.3** Manual mini-app smoke: Game / Wallet / Stake / Bridge tabs; reduced-motion;
  360px width; framed (in-Telegram) and direct `/play`.
- **7.4** Deploy hygiene: cache-bust versions bumped, `deploy/redeploy.sh` + systemd
  notes updated, env additions documented (`GEMINI_API_KEY` if D-C=yes; nothing new
  required for the game backend beyond existing `TG_COMMUNITY_TOKEN`).
- **7.5** Each phase merged as its own PR; final pass confirms `main` is releasable.

---

## 3. Decisions still needed (so execution doesn't stall)

| Tag | Question | My recommendation |
|-----|----------|-------------------|
| **D-A** | On Escape/prestige, reset perks & REP too, or persist them? | Persist perks+REP; reset cash/upgrades/bribes/heat |
| **D-B** | Finish wallet PR #51 on its own branch & merge first? | Yes — highest-value, merge before the rest |
| **D-C** | Swap `/meme` to Gemini (PR #55)? | Your call — cheaper/faster but adds a Google dep |
| **D-D** | Pull any PR #53 UI bits into the live game? | Default none; name any you want |
| **D-E** | Economy retune to backbone curve (opt-in 3.6)? | Default skip — you asked for features, not rebalancing |

I'll proceed on the *recommended* defaults for any you don't override.

---

## 4. Definition of "9/10 production-ready"

- ✅ Mobile **and** desktop wallet connect + sign works (W1 closed).
- ✅ Game: labels honest, one canonical file, cloud-save + leaderboard live, real
  prestige loop, offline earnings intact.
- ✅ No silent 404s from the client (track/ref wired).
- ✅ All money paths (stake/bridge/MM/payments) still fully test-green and untouched.
- ✅ Full CI matrix green; pip-audit + gitleaks clean.
- ✅ Open PRs resolved (merged, rebased-and-finished, or closed with rationale).
- ✅ Deploy notes accurate; redeploy is a documented one-liner.

The remaining 1/10 is deliberately out of scope: real on-chain $CLEAN staking (needs a
separate smart-contract audit) and net-new game content/art.

---

## 5. Branch & PR strategy

- Work continues on `claude/cool-tesla-vut940` for game/back-end phases (1–3), each
  phase a focused draft PR into `main`.
- Wallet (Phase 4) = rebase/finish PR #51 on its own branch, merge first.
- Bot (Phase 5) = PR #60 / #55 on their existing branches.
- This `PLAN.md` is committed first as the contract; phases check off against it.
