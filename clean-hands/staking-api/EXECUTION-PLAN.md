# 🧭 CLEAN backend — Master Execution Plan (Phases 2–3)

Operating doctrine ("god-dev, no cascade"):

1. **One sub-phase per commit.** Each is self-contained, reversible, and leaves
   `main` green.
2. **No regressions ("no cascade").** The full `test_staking.py` suite must pass
   before _and_ after every sub-phase. Schema changes are **additive migrations**
   (guarded by `PRAGMA user_version`) — never `DROP`/recreate, never data loss.
3. **Forensic audit gate per sub-phase.** Before committing a sub-phase I run a
   focused audit (the checklist below) and add tests that prove the fix.
4. **End-to-end thinking.** Sequenced by dependency to avoid rework: money
   representation is fixed _first_ so Postgres and payouts are built on the final
   shape.
5. **Solana-only, soft-staking invariant:** the server never custodies user funds
   or keys for staking; only the treasury payout (P3.2) ever moves tokens, from a
   treasury account, idempotently.

---

## Dependency-ordered sequence

### P3.1 — Integer base-unit accounting ⟵ first domino

Money is stored/accrued in **integer base units** (10^decimals), not floats, to
kill rounding drift. Economics keeps operating in human-token space (coarse
thresholds), accrual arithmetic is integer and floored. API responses still
return human-readable amounts (frontend unchanged → no cascade).

- **Audit:** floor/round correctness, 64-bit overflow headroom, migration
  idempotency + value preservation, conservation (no value created/destroyed),
  no double-count.
- **Acceptance:** suite green; new conservation + migration tests.

### P2.PG — Postgres adapter (zero SQLite regression)

A dialect shim in `db.py`: SQLite by default (byte-identical), **Postgres** when
`DATABASE_URL` is set. Translates `?`→`%s` and `INSERT OR IGNORE`→
`ON CONFLICT DO NOTHING`; dialect-specific DDL. Built on the P3.1 integer schema.

- **Audit:** translation can't enable SQL injection (params still bound),
  idempotency `rowcount` semantics identical on PG, connection lifecycle,
  `DATABASE_URL` never logged.
- **Acceptance:** SQLite suite unchanged (proves no regression); translation unit
  tests; PG path import-guarded + documented as needs-live-PG.

### P3.2 — Treasury payout state machine (the only token-moving path)

`/api/claim` becomes a **two-step, idempotent** state machine backed by a
`claims` table: `requested → sending → sent(sig) → confirmed | failed`. Pluggable
`PAYOUT_MODE`:

- `manual` (default) — records a debt, an operator/cron pays (no key on server).
- `transfer` — server signs an SPL transfer from a treasury keypair.
- **Audit (highest):** no double-pay (atomic state transitions + idempotency key),
  treasury key handling (env/KMS, never logged), partial-failure recovery
  (sent-but-unconfirmed reconciled, never re-sent blindly), concurrent-claim
  reentrancy, amount integrity vs `accrued`.
- **Acceptance:** suite green; double-claim test; crash-between-states test.
- **Decision required from you:** `manual` vs `transfer`, and treasury custody
  (hot key vs KMS/multisig) for `transfer`.

### P3.3 — Reconciliation & invariants

A `reconcile` job/endpoint: assert `Σ ledger == state` and `Σ claims.sent ==
claimed_total`; flag drift; export a report. Cron-friendly.

- **Audit:** read-only, no PII leak, alerts on mismatch.
- **Acceptance:** detects an injected drift in tests.

---

## Per-sub-phase forensic checklist (applied each time)

- Auth/authz: can a caller act as another wallet/user? (must be no)
- Money: can value be created, double-counted, or double-paid? (must be no)
- Idempotency: are retries/concurrent dupes safe?
- Injection: all SQL parameterized; no string interpolation of input.
- Migration: idempotent, additive, preserves existing rows.
- Failure modes: RPC/DB/crash mid-operation leaves consistent state.
- Secrets: keys/URLs never logged or returned.
- Regression: full suite green.

Status is tracked in `staking-api/README.md` ("Hardening status").
