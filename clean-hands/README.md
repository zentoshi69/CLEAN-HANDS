# 🧤 CLEAN HANDS

The complete community + staking stack for **$CLEAN** on Solana — Telegram bots, a
wallet-connect Mini App, a soft-staking backend (the source of truth for the app
**and** the website), and one-command deploy. Self-contained: clone, fill `.env`,
run.

> clean hands, dirty money. Mint: `6jb4XWggYJjoo3fx7irPVxhNiuFbHUyVyKR8mBL8pump`

## What's inside

| Path | What |
| --- | --- |
| `guardian_bot.py` | join CAPTCHA, anti-scam, drainer-link guard, impersonation flags, `/setup` `/lockdown` |
| `solana_scanner_bot.py` | RugCheck safety scan on every posted mint; auto-deletes high-risk |
| `community_bot.py` | `/price` `/stats` `/chart` `/trade`, `/meme` `/glove` `/sticker`, `/app` (Mini App) |
| `alerts_bot.py` | price / market-cap-milestone / heartbeat posts to the channel |
| `staking-api/` | **soft-staking backend** (FastAPI) + the bundled Mini App webapp + reconcile/payout tools |
| `site-sdk/` | drop-in JS client so your **website** uses the same API → site + app always in sync |
| `deploy/` | DNS, Caddy (auto-HTTPS), BotFather sheet, `deploy.sh`, healthcheck, **GO-LIVE** runbook, HOSTING |
| `systemd/` | sandboxed units for every service |
| `configure.py` | one-shot push of all Bot-API-settable @BotFather settings |
| `CLAUDE-OPS.md` | run it with Claude/OpenClaw safely (non-custodial, human-in-the-loop) |
| `BOTS-GUIDE.md` | the detailed Telegram-bot deploy guide |

## Go live (≈10 min on a $5 VPS)

```bash
git clone https://github.com/zentoshi69/clean-hands.git && cd clean-hands
cp .env.example .env && nano .env          # tokens, secrets, mint
sudo DOMAIN=app.cleanhands.fun bash deploy/deploy.sh
```

Then the DNS record (`deploy/DNS.md`) + @BotFather (`deploy/BOTFATHER.md`). Full
ordered runbook: **`deploy/GO-LIVE.md`**. Cheapest/easiest hosting: **`deploy/HOSTING.md`**.

## How it fits together

```
Telegram + Website ─→ Caddy (HTTPS app.cleanhands.fun) ─→ staking-api :8090
                          (Mini App at /, API at /api/*)
   website embeds site-sdk/clean-staking.js ──────────────┘  one DB = one source of truth
   same VPS also runs: guardian · scanner · community · alerts · notifier ─→ Solana RPC
```

## Soft staking, in one line

Tokens never leave the user's wallet. The backend snapshots the wallet's $CLEAN and
accrues yield from base APR, verified staking-power boosters, and burn-to-boost,
all in integer base units, with an append-only ledger + daily reconciliation.
Payout is **manual** — no private key on the server. See `staking-api/README.md`.

## Mechanics docs

- [`APP-MECHANICS.md`](APP-MECHANICS.md) — full GitHub-readable explanation of
  wallet login, soft staking, boosters, game/social verification, claims, data
  model, and operator safety controls.
- [`docs/CLEAN-STAKING-ONE-PAGER.md`](docs/CLEAN-STAKING-ONE-PAGER.md) and
  [`docs/CLEAN-STAKING-ONE-PAGER.pdf`](docs/CLEAN-STAKING-ONE-PAGER.pdf) —
  branded one-page staking explainer for users, partners, and moderators.

**Boosters & burn:** full user + dev guide in [`BOOSTERS.md`](BOOSTERS.md) — how the
additive APR stack works, the on-chain burn mechanic, the liquidity booster, and the
API hooks to plug it in.

## Security

Wallet-signature login (ed25519) + Telegram `initData` HMAC; CORS allow-list; rate
limiting; fail-fast prod config; sandboxed systemd; idempotent claims/burns; tests
in `staking-api/test_staking.py`. Audited **9.2/10** — `staking-api/SECURITY-AUDIT.md`
+ `THREAT-MODEL.md`.

## Run the tests

```bash
cd staking-api && pip install -r requirements.txt && python test_staking.py
python ../alerts_bot.py --selftest
```

---

© Bit Trading P.C. — proprietary. Defensive / community tooling; not financial advice.
