# 🛡️ CLEAN soft-staking backend — Security Audit Scorecard

**Date:** 2026-06-07 · **Scope:** `bots/staking-api/` (+ `bots/miniapp/server.py`,
webapp). **Chain:** Solana. **Model:** off-chain soft-staking, manual payout.

**Methodology:** (1) automated security-review pass; (2) an independent
senior-engineer trace of all eight critical attack classes against the full
source; (3) the committed regression/abuse test-suite (11 groups) green at every
sub-phase. Scores use a harsh rubric — a real auditor distrusts a 10, so 10 is
reserved for "provably impossible," and known residual/trust assumptions are
deducted explicitly.

## Verdict

**0 exploitable findings at or above the ≥8/10 confidence bar.** No path found for
cross-user impersonation, value creation / double-pay, session or initData
forgery, SQL injection, DOM-XSS, admin bypass, or nonce/burn replay.

---

## Scorecard (harshest metric)

| #   | Domain                              | Score        | Why not higher                                                                                                                                                                      |
| --- | ----------------------------------- | ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Authentication & session integrity  | **9.4 / 10** | HMAC sessions unforgeable w/o secret; ed25519 + single-use nonce; constant-time compares. −0.6: sessions are stateless (no server-side revocation list).                            |
| 2   | Authorization / access control      | **9.5 / 10** | Wallet always from verified session, never request body; admin gated + closed-by-default. −0.5: single admin role, no per-action scoping.                                           |
| 3   | Money integrity & idempotency       | **9.3 / 10** | Claim CAS + burn rowcount gate + append-only ledger ⇒ no double-count/double-pay; reconciler proves invariants. −0.7: off-chain ledger trust; payout settlement is operator-driven. |
| 4   | Injection (SQLi / XSS / path / cmd) | **9.8 / 10** | All SQL parameterized incl. PG shim; XSS sinks escaped; no user input in file paths. −0.2: no automated SAST in CI yet.                                                             |
| 5   | Cryptography                        | **9.5 / 10** | ed25519 (PyNaCl), HMAC-SHA256, `secrets` nonces, finalized-commitment burns. −0.5: no HSM/KMS for signing material.                                                                 |
| 6   | Secrets management                  | **8.8 / 10** | Env/`EnvironmentFile`, never logged, fail-fast in prod, git-ignored. −1.2: hot env secrets, no KMS/Vault, no rotation automation.                                                   |
| 7   | Data integrity & auditability       | **9.5 / 10** | Append-only ledger + daily reconciliation + idempotent migrations. −0.5: no WORM/off-box log shipping.                                                                              |
| 8   | Abuse resistance                    | **8.5 / 10** | Per-IP + per-wallet rate limits; anti-gaming (earn only what you hold); Sybil costs real tokens. −1.5: `X-Forwarded-For` trust assumes a sane edge proxy.                           |
| 9   | Operational hardening               | **9.2 / 10** | systemd sandbox (NoNewPrivileges, ProtectSystem=strict, syscall filter, dropped caps), security headers, body cap, docs off in prod. −0.8: relies on operator TLS/proxy config.     |
| 10  | Supply chain                        | **8.7 / 10** | Deps pinned with bounds; minimal surface; optional deps lazy-imported. −1.3: no hash-locked lockfile / SBOM.                                                                        |
| 11  | Resilience & failure modes          | **9.2 / 10** | Malformed input → clean 4xx (no 500s); RPC hiccup keeps last balance; finalized burns only. −0.8: no chaos/partial-failure test harness.                                            |

### Overall: **9.2 / 10 — Grade A**

### Tamper-immunity (immutability) sub-score: **9.4 / 10**

Idempotent state transitions (CAS claims, signature-keyed burns) + append-only
ledger + automated reconciliation make silent tampering detectable and
double-execution structurally impossible on the money paths.

---

## Immunity matrix (attack → defense → status)

| Attack                     | Defense                                    | Status      |
| -------------------------- | ------------------------------------------ | ----------- |
| Act as another wallet      | wallet from signed session only            | ✅ immune   |
| Forge a session            | HMAC(server_secret); constant-time         | ✅ immune   |
| Spoof Telegram identity    | initData HMAC + one-TG↔one-wallet (409)    | ✅ immune   |
| Double-claim rewards       | atomic CAS on exact accrual                | ✅ immune   |
| Double-credit a burn       | INSERT-rowcount gate, PK=signature         | ✅ immune   |
| Credit someone else's burn | authority==wallet + mint match + finalized | ✅ immune   |
| Replay a login nonce       | single-use `getdel`                        | ✅ immune   |
| SQL injection (incl. PG)   | bound params; shim rewrites template only  | ✅ immune   |
| DOM-XSS in app             | `esc()`/`textContent` on all sinks         | ✅ immune   |
| Admin endpoint bypass      | token gate, closed when unset              | ✅ immune   |
| Misconfig serving bad data | fail-fast config in prod                   | ✅ immune   |
| Drift / silent corruption  | append-only ledger + daily reconcile       | ✅ detected |

---

## Residual risks the auditor must weigh (honest, by design)

These are **model/operational** choices, not code defects:

1. **Off-chain trust.** Reward accounting is centralized (soft staking). Users
   trust the operator to compute honestly (reconciler makes this auditable) and to
   fund payouts. Fully trustless rewards would need an on-chain claim program.
2. **Manual payout.** `/api/claim` records a debt; an operator/cron pays from the
   treasury. By design **no private key lives on the server** (we chose this over a
   hot-key auto-transfer).
3. **Secrets in env, not KMS.** Acceptable for launch; KMS/Vault + rotation is the
   path to 10/10 on domains 5–6.
4. **Edge dependency.** Rate-limit IP attribution and TLS assume a correctly
   configured reverse proxy (Caddy/Cloudflare).
5. **Not yet performed:** third-party pen-test, load test at 100k, and the
   **on-device wallet deeplink** round-trip (no wallet app in CI). Backend logic is
   fully unit/integration-tested; the wallet UX needs a real device pass.

## Path to 10/10

KMS-backed secrets + rotation · hash-locked lockfile + SBOM · SAST/dependency
scanning in CI · third-party pen-test + load test · on-device wallet verification ·
(optional) on-chain Merkle-claim to remove payout trust entirely.
