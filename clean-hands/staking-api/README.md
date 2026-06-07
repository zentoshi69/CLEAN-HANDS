# 🏦 CLEAN soft-staking API — the shared source of truth

One backend that **your website and the Telegram Mini App both call**, so they
always show identical numbers. Soft staking (no lock), with amount/loyalty/referral
boosters and **burn-to-boost** yield, on **Solana**, with **wallet-signature**
login (the same wallet the site uses).

```
        ┌────────────┐         ┌──────────────────┐
        │  Website    │──┐     │  Telegram Mini    │
        │ (wallet)    │  │     │  App (wallet+TG)  │
        └────────────┘  │     └──────────────────┘
                        ▼              ▼
                ┌───────────────────────────┐
                │   THIS API (source of truth)│
                │  economics · sessions · db  │
                └───────────────────────────┘
                        │ reads
                        ▼
                ┌───────────────────────────┐
                │  Solana RPC (balances,      │
                │  burn verification)         │
                └───────────────────────────┘
```

## Why a shared backend (not on-chain-only)

On-chain only knows balances & burns — it has **no idea** about your base APR,
loyalty/referral boosters, or burn multipliers. Those are _your rules_, computed
here. This is also the **fastest UX** (reads come from the DB/cache, not slow RPC)
and stays secure because every write is **wallet-signature-gated** and burns are
**verified on-chain** before crediting.

## The economics (defaults — all tunable, see `economics.py`)

```
effectiveAPR = baseAPR · (1 + amountBoost + loyaltyBoost + referralBoost) + burnBonusAPR
reward(dt)   = effectiveStaked · effectiveAPR · dt / year
effectiveStaked = min(enrolled, current on-chain balance)   # anti-gaming
```

| Booster       | Default                                                 |
| ------------- | ------------------------------------------------------- |
| base APR      | 40%                                                     |
| amount tier   | ≥100k +10% · ≥1M +25% · ≥10M +50% (highest wins)        |
| loyalty       | +5% per 30 days staked, cap +50%                        |
| referral      | +2% per actively-staking referral, cap +30%             |
| burn-to-boost | +5% APR **permanent** per 100k $CLEAN burned, cap +200% |

Override any of these with env vars (`STAKE_BASE_APR`, `STAKE_BURN_APR_PER_UNIT`, …).

> **Soft staking** = tokens never leave the user's wallet. "Stake" snapshots the
> wallet's balance and accrues yield on it; if they move tokens out, earnings
> drop to what they still hold. Rewards accrue in the DB; wire your treasury /
> on-chain payout into the `/api/claim` handler (marked with a TODO).

## Run

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
export TG_COMMUNITY_TOKEN=...          # for Mini App initData verification
export DEFAULT_TOKEN_MINT=...          # the $CLEAN mint
export DEFAULT_TOKEN_DECIMALS=6        # for raw-burn conversion
export SOLANA_RPC_URL=https://...      # a paid RPC (Helius/Triton) in prod
export STAKE_SERVER_SECRET=$(openssl rand -hex 32)
python app.py                          # :8090
```

Test it: `python test_staking.py` (economics + signature + full API flow, no network).

## Login flow (same for site & Mini App)

1. `GET /api/nonce?wallet=<addr>` → `{ nonce, message }`
2. User signs `message` with their Solana wallet (Phantom/Backpack/etc.).
3. `POST /api/login { wallet, signature, nonce, initData?, ref? }`
   - `initData` (Mini App only) binds `telegram_id ↔ wallet`.
   - `ref` (optional) = referrer's wallet from a referral link.
   - → `{ token, profile }`. Use `token` for all other calls.

## API

| Endpoint                | Body                                      | Does                                                    |
| ----------------------- | ----------------------------------------- | ------------------------------------------------------- |
| `GET /api/nonce`        | `?wallet=`                                | login challenge                                         |
| `POST /api/login`       | wallet, signature, nonce, initData?, ref? | verify → session + profile                              |
| `POST /api/stake`       | token                                     | snapshot balance → start earning                        |
| `POST /api/unstake`     | token                                     | stop earning (keep accrued)                             |
| `POST /api/claim`       | token                                     | move pending rewards to claimed (hook payout here)      |
| `POST /api/burn`        | token, signature                          | verify on-chain burn → permanent APR boost (idempotent) |
| `POST /api/profile`     | token                                     | full live profile + APR breakdown                       |
| `POST /api/leaderboard` | token                                     | top 50 stakers                                          |
| `POST /api/referrals`   | token                                     | your referral code + active count                       |
| `GET /api/economics`    | —                                         | public rule config (so site/app render the same)        |

## Security

- **Wallet ownership** proven by ed25519 signature over a single-use nonce.
- **Telegram identity** proven by `initData` HMAC; bound to the wallet.
- **Sessions** are short-lived HMAC tokens (set `STAKE_SERVER_SECRET`).
- **Burns** are verified against the actual transaction on-chain and credited
  once (idempotent by signature).
- **Anti-gaming**: you only earn on tokens you still hold.
- The server **never holds funds or keys** — soft staking, signatures only.

## Deploy 24/7

`../systemd/degen-staking.service` (sandboxed) runs it. Put it behind HTTPS
(Caddy/Cloudflare) — the Mini App requires HTTPS and the site will call it cross-origin
(add your site origin to CORS if the site is on a different domain).

## Hardening status (forensic audit — Phase 1 done)

- **Fail-fast config** (`config.py`): set `STAKE_ENV=prod` and the API refuses to
  boot without `STAKE_SERVER_SECRET` + `DEFAULT_TOKEN_MINT` (no silent misconfig).
- **Input validation**: wallet addresses are checked as base58 32-byte keys at the
  edge before any RPC/DB use.
- **One Telegram ↔ one wallet**: linking a TG account already bound to another
  wallet returns `409` (was a 500).
- **Append-only audit ledger** (`ledger` table): every stake / unstake / claim /
  burn is recorded for reconciliation and dispute resolution — never mutated.
- **Claim uses live balance**: `/api/claim` force-refreshes the on-chain balance
  before settling, so you can't over-accrue by selling right before claiming.
- **Deep `/healthz`**: checks the DB and reports config status (`503` if DB down).
- **Burn double-credit** closed earlier (idempotent on the tx signature).

## Hardening status (Phase 2 — scale & abuse resistance)

- **Shared store** (`store.py`): login nonces + rate-limit counters use an
  in-memory backend by default, or **Redis** when `REDIS_URL` is set — so they work
  correctly across multiple workers/hosts. Sessions stay stateless (HMAC).
- **Rate limiting** (`ratelimit.py`): per-IP **and** per-wallet fixed-window limits
  on `nonce` / `login` / `burn` / write endpoints → `429`. Tunable via `RL_*`.
- **Burn finality**: `verify_burn` reads with `commitment: "finalized"`, so a burn
  that could still be rolled back is never credited.
- **Backups**: `backup.sh` makes consistent, gzip'd, rotated SQLite snapshots
  (online `.backup`, cron-friendly).
- `/healthz` now also reports the active store backend.

**Phase 3.1 (integer money) — done:** amounts are stored/accrued in integer base
units (10^decimals); accrual floors (never over-credits); an idempotent
`user_version` migration converts any legacy float rows in place. API responses
still return human amounts (frontend unchanged).

**Phase 2 (Postgres adapter) — done:** set `DATABASE_URL` to a Postgres DSN and
the same code runs on Postgres (dialect shim in `db.py`; `pip install psycopg[binary]`).
Blank = SQLite. The `burns` PK + `ON CONFLICT DO NOTHING` keep burn-credit
idempotent under concurrency on both. (PG path needs a live DB to validate.)

**Phase 3.2 (claims / payout) — done (manual mode):** `/api/claim` uses an atomic
compare-and-swap so a claim can't be double-counted/double-paid; each claim writes
a `claims` row (`requested`). Pay out from the treasury and settle via the
admin endpoints (gated by `STAKE_ADMIN_TOKEN`):

- `POST /api/admin/pending {admin_token}` → list unpaid claims.
- `POST /api/admin/mark_paid {admin_token, claim_id, tx_sig}` → mark one paid
  (idempotent). No private key ever lives on the server.

**Phase 3.3 (reconciliation) — done:** `python reconcile.py` checks the money
invariants (claimed_total == Σ claims == Σ ledger claims; total_burned == Σ ledger
burns == Σ burns) and exits non-zero on drift. Install the timer to run it daily:

```bash
sudo cp ../systemd/degen-reconcile.{service,timer} /etc/systemd/system/
sudo systemctl enable --now degen-reconcile.timer    # daily money-invariant check
```

**Payout cheat-sheet (manual mode):**

```bash
python pay.py list                  # pending claims + total to send
# …send $CLEAN from the treasury wallet…
python pay.py mark <claim_id> <tx>  # record the payout tx (idempotent)
```

**Edge hardening:** request body-size cap (413), security headers (nosniff /
Referrer-Policy / HSTS / Permissions-Policy), interactive docs disabled when
`STAKE_ENV=prod`, malformed input rejected cleanly (no 500s), deps pinned.

Optional future: **P3.2b** server-signed `transfer` payout (a treasury hot key or
KMS) if you ever want auto-payout instead of the cron — deliberately not built, to
keep no key on the server.

## Production / scale (built for ~100k users)

- **Run multiple workers behind a proxy.** `uvicorn app:app --workers N` or gunicorn.
  Two pieces of state must then be shared across workers/instances:
  - **Sessions** are already stateless (HMAC) — just set a fixed `STAKE_SERVER_SECRET`
    on every instance so tokens validate everywhere.
  - **Login nonces + rate-limit counters** — set **`REDIS_URL`** and they're shared
    across all workers/hosts automatically (`store.py`). No code change needed.
- **Database**: SQLite (WAL) is fine for a single node / moderate write rate. For
  100k users with heavy concurrent writes, move to **Postgres** — the schema in
  `db.py` ports directly; keep the burn-credit idempotency (the `burns` PK gate in
  `/api/burn`) which already prevents double-credit under concurrency.
- **RPC**: use a **paid Solana RPC** (Helius/Triton). `getTokenAccountsByOwner` /
  `getTransaction` on the public endpoint will rate-limit hard at scale. Balances
  are cached (`STAKE_BALANCE_TTL`, default 5 min) to keep reads off the RPC.
- **CORS**: set `STAKE_CORS_ORIGINS` to your website origin(s) so the browser can
  call this API. The list is explicit (no wildcard) and credentials are off (we use
  body tokens, not cookies).
- **HTTPS + a real reverse proxy** (Caddy/nginx/Cloudflare) terminating TLS, with
  request-size limits and rate limiting at the edge.

## The Mini App frontend is bundled

This server **serves the Telegram Mini App itself** at `/` (`webapp/index.html`,
`app.js`, `wallet.js`) — same origin as the API, so there's no CORS for the app.
It does multi-wallet connect (**Phantom · Solflare · Backpack**) via the encrypted
deeplink protocol, then the nonce→sign→login flow, and renders stake / boost /
leaderboard / invite against the API.

To wire it to the bot: point the community bot's `MINIAPP_URL` at **this server's
HTTPS URL** (and `MINIAPP_BOT_USERNAME` / `MINIAPP_SHORT_NAME` for referral links),
then `/newapp` in BotFather with the same URL. `/app` in the group now opens the
real staking app. The deeplink round-trip needs **on-device testing** (it can't be
exercised headlessly — no wallet app in CI).

## Wiring the clients

- **Telegram Mini App**: bundled here — just deploy + set `MINIAPP_URL` (above).
- **Website**: call the same endpoints after wallet-connect — instant parity with
  the app (same source of truth). Add your site origin to `STAKE_CORS_ORIGINS`.
