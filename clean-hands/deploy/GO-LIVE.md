# 🚀 GO-LIVE — phased runbook (site + Telegram Mini App in sync)

Do the phases in order. Each ends with a check. `app.cleanhands.fun` = your domain.

---

## Phase 1 — DNS

Add the `app` A/AAAA record → your server (see `DNS.md`). Open ports 80+443.
✅ Check: `dig +short app.cleanhands.fun` shows your IP.

## Phase 2 — Server + secrets

```bash
git clone https://github.com/zentoshi69/clean-hands.git && cd clean-hands
cp .env.example .env && nano .env
```

Fill `.env`:

- `TG_BOT_TOKEN` / `TG_SCANNER_TOKEN` / `TG_COMMUNITY_TOKEN`, `TG_ADMIN_IDS`
- `DEFAULT_TOKEN_MINT` = your $CLEAN mint (the pump.fun CA), `DEFAULT_TOKEN_DECIMALS=6`
- `STAKE_ENV=prod`, `STAKE_SERVER_SECRET=$(openssl rand -hex 32)`,
  `STAKE_ADMIN_TOKEN=$(openssl rand -hex 32)`
- `SOLANA_RPC_URL` = a paid RPC (Helius/Triton)
- `STAKE_CORS_ORIGINS=https://cleanhands.fun,https://www.cleanhands.fun` (your site)
- `MINIAPP_BOT_USERNAME`, `MINIAPP_SHORT_NAME=app`
- `MINIAPP_SWAP_RPC` = a browser-safe RPC (NOT the paid one)
- optional: `TG_ALERTS_CHAT`, `TG_ALERTS_TOKEN`; `TG_NOTIFY_TOKEN`

```bash
chmod 600 .env
```

✅ Check: `STAKE_ENV=prod` + secret + mint set (the API fails fast otherwise).

## Phase 3 — Install + run all services

```bash
# bots venv
python3 -m venv venv && venv/bin/pip install -r requirements.txt
# staking-api venv (FastAPI + crypto)
python3 -m venv staking-api/venv && staking-api/venv/bin/pip install -r staking-api/requirements.txt
# (optional) mini-app points demo venv if you run it: miniapp/venv

sudo bash install-systemd.sh   # starts every service that has a venv
```

✅ Check: `systemctl --no-pager status 'degen-*'` all active; the staking API is
listening on `127.0.0.1:8090`.

## Phase 4 — Edge / HTTPS

Edit `deploy/Caddyfile` (your domain) → install → restart Caddy.
✅ Check: `bash deploy/healthcheck.sh app.cleanhands.fun` is all green.

## Phase 5 — @BotFather

Follow `BOTFATHER.md`: privacy off, register the Mini App URL
`https://app.cleanhands.fun/`, allow groups, then `python configure.py`, then
`/setup` in the group.
✅ Check: typing `/` in the group shows the command menus; the bot's menu button
opens the app.

## Phase 6 — Verify the loop (on a phone)

- Open the Mini App → **connect wallet** (Phantom/Solflare/Backpack) → **stake** →
  see APR; **burn** (paste a burn tx or in-app if enabled) → APR rises.
- **Leaderboard** + **invite link** work.
- **Claim** → an entry appears in `python pay.py list`. Pay it from the treasury,
  then `python pay.py mark <id> <tx>`.
- `python reconcile.py` → "no drift".

## Phase 7 — Put it on the website (sync)

Embed `site-sdk/clean-staking.js`, point it at `https://app.cleanhands.fun`, and
make sure your site origin is in `STAKE_CORS_ORIGINS`. Now web + Telegram show
identical state. (See `site-sdk/README.md`.)

---

### Daily ops

- Logs: `journalctl -u degen-staking -u degen-community -f`
- Restart one: `sudo systemctl restart degen-staking`
- Payouts: `python pay.py list` → pay → `python pay.py mark <id> <tx>`
- Integrity: `degen-reconcile.timer` runs `reconcile.py` daily (alerts on drift)
- Rotate a leaked token: `/revoke` in @BotFather → update `.env` → restart
