# CLEAN Hands — 10k User Launch Readiness Gate

**Date:** 2026-06-23  
**Scope:** `staking-api` production Mini App and API

This is the release gate for putting the staking app in front of ~10,000 people. Do not treat passing local tests as launch approval. The live Mini App must pass this checklist on the production domain and on real phones.

## Hard no-go conditions

Do not launch if any of these are true:

- `GET https://<app-domain>/readyz` is not HTTP 200 with `"ok": true`.
- `STAKE_ENV=prod` is not set.
- `REDIS_URL` is missing.
- `SOLANA_RPC_URL` points at the public Solana RPC.
- `STAKE_SERVER_SECRET` or `STAKE_ADMIN_TOKEN` is shorter than 32 characters.
- `TG_COMMUNITY_TOKEN`, `DEFAULT_TOKEN_MINT`, `MINIAPP_URL`, `MINIAPP_BOT_USERNAME`, or `MINIAPP_SHORT_NAME` is missing.
- Payout operator cannot access the treasury wallet and cannot run `python staking-api/pay.py list`.
- No one has tested connect → stake → payout setup → claim error/success states on a real iPhone and Android Telegram webview.

## Required production env

Minimum launch posture:

```bash
STAKE_ENV=prod
STAKE_SERVER_SECRET=<openssl rand -hex 32>
STAKE_ADMIN_TOKEN=<openssl rand -hex 32>
DEFAULT_TOKEN_MINT=<real CLEAN mint>
DEFAULT_TOKEN_DECIMALS=6
SOLANA_RPC_URL=<paid RPC>
REDIS_URL=redis://127.0.0.1:6379/0
STAKE_HOST=127.0.0.1
STAKE_PORT=8090
TG_COMMUNITY_TOKEN=<bot token>
MINIAPP_URL=https://<app-domain>/
MINIAPP_BOT_USERNAME=<bot username without @>
MINIAPP_SHORT_NAME=app
STAKE_CORS_ORIGINS=https://<site-domain>,https://www.<site-domain>
```

SQLite is acceptable only for a single-node canary. For a full 10k push, prefer Postgres via `DATABASE_URL`.

## Deploy checklist

### Pre-deploy

- [ ] Commit and push the release.
- [ ] Back up the current DB.
- [ ] Run `python staking-api/test_staking.py`.
- [ ] Run `node staking-api/webapp/test_wallet_flow.mjs`.
- [ ] Run `pip-audit -r staking-api/requirements.txt`.
- [ ] Run `bandit -q -r staking-api -x staking-api/test_staking.py -s B101`.
- [ ] Confirm `deploy/healthcheck.sh <app-domain>` passes on staging.
- [ ] Confirm `/whitepaper` shows the true vesting/forfeiture/manual-payout terms.
- [ ] Confirm Telegram BotFather Mini App URL points to the production app domain.

### Production canary

- [ ] Deploy to production.
- [ ] Run `bash clean-hands/deploy/healthcheck.sh <app-domain>`.
- [ ] Confirm `/readyz` includes Redis store ok and RPC ok.
- [ ] Open production Mini App on iPhone Telegram.
- [ ] Open production Mini App on Android Telegram.
- [ ] Connect wallet, refresh app, confirm session survives.
- [ ] Stake a small wallet.
- [ ] Confirm locked claim state is honest.
- [ ] Confirm payout setup asks for a fresh wallet signature.
- [ ] Confirm leaderboard/stats use effective stake, not recorded ghost stake.

### First 30 minutes after announcement

Watch:

- HTTP 5xx rate
- HTTP 429 rate
- `/readyz` status
- Solana RPC latency/errors
- Redis health
- pending claim count and oldest pending age
- Telegram connect/sign error logs
- support messages about wallet loops or payout setup

## Rollback / freeze triggers

Use the freeze switch before trying heroic live debugging:

```bash
curl -sS https://<app-domain>/api/admin/set_flag \
  -H 'content-type: application/json' \
  -d '{"admin_token":"<token>","key":"halt_claims","value":true}'
```

Available flags:

- `halt_all`
- `halt_staking`
- `halt_claims`
- `halt_burns`
- `halt_payout_setup`

Freeze immediately if:

- claim creation returns unexplained 5xx errors;
- Solana RPC degraded and users are trying to claim;
- payout destination mismatch is suspected;
- referral or leaderboard numbers look inflated;
- 5xx rate stays above 1% for 5 minutes;
- users report a widespread wallet sign-in loop.

Rollback the deployment if:

- `/readyz` remains 503 after one restart;
- the Mini App cannot load on either iPhone or Android Telegram;
- wallet login fails for two different wallets on real devices;
- static assets or `/api/economics` serve stale/wrong terms.

## Payout operating rule

Never mark a claim paid from a screenshot, explorer memory, or pasted text alone.

Use:

```bash
python staking-api/pay.py list
python staking-api/pay.py mark <claim_id> <tx_sig>
```

`mark` verifies the finalized token transfer increased the snapshotted payout destination by at least the claim net amount before changing DB state.

## What remains outside code

- Independent security review.
- Legal review of reward/fee/vesting language.
- Real treasury payout canary with a tiny amount.
- Real-device Telegram QA recording for iOS and Android.
- Postgres/Redis production load test if pushing beyond a single-node canary.
