# 🧤 CLEAN Mini App — points DEMO (not the production staking app)

> ⚠️ **This is a standalone demo with its own database and points economy.**
> The production Mini App — the one the website shares its backend with — is
> bundled inside `../staking-api/webapp/` and served by the staking API at `/`.
> `deploy/Caddyfile` routes ONLY to the staking API, so this server is never
> reachable in the standard deployment. `install-systemd.sh` skips it unless
> you pass `WITH_MINIAPP=1`.

A Telegram Mini App with an **off-chain points economy**:

- **Points** — daily claim + welcome bonus.
- **Staking** — lock points for a period (7d/30d/90d) to earn a fixed yield.
- **Leaderboard** — top point holders, with your rank.
- **Referrals** — share your link; you and your friend both earn points.

> **Off-chain by design.** These are app points, not on-chain tokens. That's what
> makes it safe and instant to run. Turning points into a real on-chain $CLEAN
> staking program (handling actual funds) is a separate, audited smart-contract
> build — don't ship that without an audit.

Security: every API call is authenticated with Telegram `initData`, verified by
HMAC against the bot token, so users can only act as themselves and points can't
be spoofed.

---

## Run it

```bash
cd bots/miniapp
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

export TG_COMMUNITY_TOKEN="123456:ABC..."   # SAME bot that will own the mini app
python server.py                             # serves on :8080
```

It serves the frontend (`/`) and the JSON API (`/api/*`) from one origin.

## Expose it over HTTPS (Mini Apps require HTTPS)

**Fastest (no domain needed) — Cloudflare Tunnel:**

```bash
# install cloudflared, then:
cloudflared tunnel --url http://localhost:8080
# prints an https://something.trycloudflare.com URL — that's your MINIAPP_URL
```

**Production — your own domain + Caddy** (auto-HTTPS):

```
app.yourdomain.com {
    reverse_proxy localhost:8080
}
```

## Register the app with @BotFather

1. `/newapp` → pick your **Community** bot.
2. Title, description, upload an icon (the glove works great).
3. **Web App URL** = your HTTPS URL from above.
4. **Short name** = `app` (or set `MINIAPP_SHORT_NAME` to match what you choose).

## Wire the bot to it

In `bots/.env` set:

```
MINIAPP_URL=https://your-https-url
MINIAPP_BOT_USERNAME=your_community_bot     # no @
MINIAPP_SHORT_NAME=app
```

Restart the community bot. Now:

- `/app` and `/stake` show an **Open App** button,
- `/invite` returns the member's referral link,
- the bot's **menu button** opens the app in private chat.

## Run it 24/7

A ready unit is at `../systemd/degen-miniapp.service` (it expects a venv in
`miniapp/venv` and reads `../.env`). Install it the same way as the bots:

```bash
sudo cp ../systemd/degen-miniapp.service /etc/systemd/system/
# edit the youruser/paths first, then:
sudo systemctl daemon-reload && sudo systemctl enable --now degen-miniapp
```

---

## Tune the economy (env vars, all optional)

| Var                     | Default | Meaning                      |
| ----------------------- | ------- | ---------------------------- |
| `MINIAPP_DAILY_CLAIM`   | 100     | points per daily claim       |
| `MINIAPP_WELCOME_BONUS` | 200     | points on first open         |
| `MINIAPP_REF_REFERRER`  | 500     | points to the inviter        |
| `MINIAPP_REF_REFEREE`   | 250     | points to the invited friend |
| `MINIAPP_PORT`          | 8080    | listen port                  |

Staking tiers (days → total return) live in `STAKE_TIERS` at the top of
`server.py`: `7d → +5%`, `30d → +25%`, `90d → +100%`. Edit to taste.

Data is stored in `clean.db` (SQLite, git-ignored). Back it up if it matters.

---

## API (all POST, all take `{ "initData": "<from Telegram>" }`)

| Endpoint           | Does                                               |
| ------------------ | -------------------------------------------------- |
| `/api/auth`        | upsert user, apply referral, return profile        |
| `/api/claim`       | daily points claim (429 if on cooldown)            |
| `/api/stake`       | `{amount, days}` → lock points                     |
| `/api/stakes`      | list your active stakes                            |
| `/api/unstake`     | `{id}` → claim a matured stake (principal + yield) |
| `/api/leaderboard` | top 50 + your rank                                 |
| `/api/referrals`   | your referral code + count                         |
