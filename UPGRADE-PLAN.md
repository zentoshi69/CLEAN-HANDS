# CLEAN-HANDS — Path to an *Honest* 9/10

> **Direction chosen: A — Honest utility.** Relabel the financial fiction, harden
> security, make it observable + scalable. **No smart contract / no audit-firm spend.**
> Target: a defensible 9/10 as a polished, honest memecoin community app + loyalty
> system — *not* a DeFi protocol it isn't.
>
> Each phase = its own CI-gated PR, audited by a named skill before merge
> (`/security-review`, `/code-review`, `/review`, `/verify`, `/simplify`).

## Baseline scorecard (4 adversarial audits, this repo, today)

| Dimension | Now | Target |
|---|:--:|:--:|
| Is the money real? | 2 | *honest* (relabel, not faked) |
| Economic soundness | 2 | 7 (honest, funded-or-labelled) |
| Brand / legal risk (10=safe) | 3 | 8 |
| Scalability | 3 | 7 |
| Game quality | 5 | 6 (P2) |
| Ops / observability | 5 | 8 |
| Test coverage | 5 | 8 |
| Security | 6.5 | 9 |
| Architecture (money integrity) | 6 | 8 |
| Mini-app UX | 7 | 8 |

The gap between "tests pass / features shipped" and "sound product" is the whole point:
the prior "9/10" measured task-completion, not product reality.

## Priorities (do in this order — earlier phases gate later ones)

### Phase 0 — Freeze & baseline ✅ (this doc)
- Freeze game-feature work until P0 done. Capture green-CI baseline + this scorecard.

### Phase 1 — Stop the legal/honesty bleeding 🔴 P0  *(legal 3→7, money-honesty 2→6)*
- **1.1** Relabel: "staking"→"Loyalty Boost"; drop "APR %", "VIP 3×", "airdrop list"
  (securities-flavored language). `app.js` / `index.html` / `whitepaper.html`.
- **1.2** Put the satire + "no guaranteed value/returns; rewards discretionary"
  disclaimer **where money moves** (the mini-app/stake/claim/MM screens — currently zero).
- **1.3** MM deposit (`/api/mm/add`): remove, or hard "one-way, non-refundable" consent gate.
- **1.4** Label `pending_rewards` as estimated/discretionary/manual, or fund a published reserve.
- *Audit:* `/security-review` + `/verify` (screenshot live disclaimers). **Needs brand sign-off.**

### Phase 2 — Security: fail-open → fail-closed 🔴 P0  *(security 6.5→8.5)*  ⟵ in progress
- **2.1** Force `STAKE_ENV=prod` in the systemd unit + flip `.env.example` default → prod
  (missing/weak secret now REFUSES to boot instead of a random per-process key).
- **2.2** Bot admin gates deny-by-default on empty `ADMIN_IDS` (`/memetest`).
- **2.3** `html.escape()` attacker-controlled token name/symbol/risks in the scanner bot.
- **2.4** *(follow-up)* relay binding/auth, session-revocation list, Redis rate-limit + XFF hardening.
- *Audit:* `/security-review` on the branch + re-run the adversarial agent + a regression test per fix.

### Phase 3 — Glove-bot consent & abuse 🔴 P0  *(reputational)*
- Require an explicit command (no auto-glove of real people); fix the bare-photo
  rate-limit bypass (uncapped paid FLUX calls); add NSFW/consent friction.

### Phase 4 — Production readiness 🟠 P1  *(ops 5→8, scale 3→6)*
- Observability (Sentry + Prometheus; replace `print()`); move blocking SQLite off the
  event loop; pull RPC/price `await`s out of open DB write locks; Helius RPC + `gather`
  the balance reads; uvicorn workers + Redis, or honestly document the single-box ceiling.

### Phase 5 — Test & CI integrity 🟠 P1  *(tests 5→8)*
- Fix the `test_staking.py` `__main__` ordering bug; add bot tests + failure injection;
  make the Postgres path real in CI or delete it.

### Phase 6 — Product depth 🟢 P2 *(optional for 9/10)*
- Real city art/events; meta-progression beyond the linear multiplier; make the
  referral real or remove the fake client-side 2×.

### Phase 7 — 9/10 gate
- Re-run all 4 adversarial audits (every dimension ≥ target) + `/review` +
  `/security-review` on the full diff + pip-audit/gitleaks + a load test + counsel review of copy.

## The honest definition of done (9/10, Direction A)
- No securities-flavored claims; disclaimers where money moves; no undisclosed one-way deposits.
- Fail-closed security defaults; bot abuse paths capped; consent on the image bot.
- Observable, non-blocking, load-tested to a stated ceiling.
- The great game + wallet UX stay; the product is *honest about what it is*.
