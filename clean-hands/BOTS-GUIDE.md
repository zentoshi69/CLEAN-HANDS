# 🚀 Telegram Bot Fleet — Deploy Guide

> **In a hurry? Read [`QUICKSTART.md`](QUICKSTART.md)** — the ~1-hour, no-decisions
> path: the exact Telegram taps, then `bash quickstart.sh` to install + launch all
> three bots, and `sudo bash install-systemd.sh` for 24/7 uptime. This file is the
> full reference.

Three bots run your community. Run them as three separate `@BotFather` bots
(simplest), or merge them later.

| Bot file                | Job                                           | Needs admin rights    |
| ----------------------- | --------------------------------------------- | --------------------- |
| `guardian_bot.py`       | Join CAPTCHA, anti-spam, impersonation flags  | Delete, Ban, Restrict |
| `solana_scanner_bot.py` | Auto-scans contract addresses, kills rugs     | Delete                |
| `community_bot.py`      | `/price` `/stats` `/chart` `/meme` `/sticker` | none (sending only)   |

See [`BLUEPRINT.md`](BLUEPRINT.md) for the full secure-community architecture
(channel + chat pattern, owner anonymity, token gating, hardening checklist), and
[`THREAT-MODEL.md`](THREAT-MODEL.md) for an honest breakdown of what these bots do
and **don't** protect against — plus the incident playbook. Read it before launch.

Guardian also deletes wallet-drainer / phishing links (from anyone non-admin) using
[`blocklists/drainer-domains.txt`](blocklists/drainer-domains.txt) — keep that list
updated as you spot new impersonator domains.

**Mini App** — a Telegram Mini App (points · staking-for-yield · leaderboard ·
referrals) lives in [`miniapp/`](miniapp/README.md). The community bot's `/app`,
`/stake`, `/invite` and menu button open it. **Glove generator** — reply to any
photo with `/glove` to slap the $CLEAN glove on it.

---

## Step 1 — Get a place to run them

Bots must stay online 24/7, so they need an always-on host (not your laptop):

- **Cheapest:** a $4–6/mo VPS (Hetzner, DigitalOcean, Vultr, Contabo).
- **Free-ish:** Railway, Fly.io, or a Raspberry Pi at home.

SSH in and confirm Python 3.10+:

```bash
python3 --version
sudo apt update && sudo apt install -y python3-pip python3-venv
```

---

## Step 2 — Create the bots in @BotFather

In Telegram, open **@BotFather** and do this **three times** (once per bot):

1. Send `/newbot`
2. Give it a display name (e.g., "Degen Guardian")
3. Give it a username ending in `bot` (e.g., `mydegenguardian_bot`)
4. **Copy the token** — looks like `8012345678:AAH...`. Keep it secret.

Then for each bot, **turn off privacy mode** so it can read group messages:

- `/setprivacy` → pick the bot → **Disable**
  _(Required for Guardian's spam scan, Scanner's CA detection, and group commands.)_

> You'll end up with 3 tokens. Label them: `GUARDIAN`, `SCANNER`, `COMMUNITY`.

---

## Step 3 — Get your numeric user ID

The bots need your Telegram **numeric ID** to know who the admins are.
Message **@userinfobot** — it replies with your ID (e.g., `11122233`).
Collect every admin's ID, comma-separated: `11122233,44455566`.

---

## Step 4 — Upload the files and install dependencies

Copy this `bots/` folder to your server (via `scp`, git, or paste into `nano`).
Then, in that folder:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

(Optional, for nicer memes) drop an `Impact.ttf` or `Anton-Regular.ttf` font file
in this folder — the community bot auto-detects it.

---

## Step 5 — Set environment variables

```bash
cp .env.example .env
nano .env        # fill in the four tokens + admin IDs + default mint
set -a; source .env; set +a
```

| Variable             | Used by   | Purpose                           |
| -------------------- | --------- | --------------------------------- |
| `TG_ADMIN_IDS`       | all bots  | comma-separated numeric admin IDs |
| `TG_BOT_TOKEN`       | guardian  | Guardian bot token                |
| `TG_SCANNER_TOKEN`   | scanner   | Scanner bot token                 |
| `TG_COMMUNITY_TOKEN` | community | Community bot token               |
| `DEFAULT_TOKEN_MINT` | community | so `/price` works with no args    |

To make these permanent, put them in each systemd unit (Step 7) rather than your shell.

---

## Step 6 — Add the bots to your group + set permissions

For **each** bot:

1. Open your **discussion group** → Add Members → search the bot's @username → add.
2. Group → **Administrators** → Add Admin → select the bot.
3. Grant the rights from the table at the top:
   - Guardian: **Delete messages + Ban users + Restrict members**
   - Scanner: **Delete messages**
   - Community: no admin rights needed (you can still make it admin, harmless).
4. For Guardian, run `/refreshadmins` once in the group so it learns the admin list.

> Test now: start each bot (Step 7), then in the group send `/help` (Community),
> paste a contract address (Scanner replies), and join with a second account
> (Guardian gates it).

---

## Step 7 — Keep them running with systemd (production)

Ready-made unit files are in [`systemd/`](systemd/). Edit the `youruser` paths and
the `EnvironmentFile` location, then install each:

```bash
sudo cp systemd/degen-community.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now degen-community
sudo systemctl status degen-community      # check it's running
journalctl -u degen-community -f           # live logs
```

Repeat for `degen-guardian.service` and `degen-scanner.service`.

The units use `EnvironmentFile=/home/youruser/bots/.env` so all your secrets live
in one file. (Prefer per-unit `Environment=` lines if you want them split.)

**Quick test alternative (no systemd):** run inside `tmux`/`screen`:

```bash
python guardian_bot.py
```

---

## Step 8 — Register the command menu (autocomplete)

So users see commands when they type `/`, message **@BotFather**:

- `/setcommands` → pick the **Community** bot → paste:

```
price - Quick price + 24h change
stats - Full token readout
chart - Live DexScreener chart
meme - Reply to a photo: /meme top | bottom
sticker - Reply to a photo to make a sticker
help - Show commands
```

---

## Step 9 — Stickers: create your community pack

The bot turns images into sticker files; **@Stickers** owns the actual pack:

1. In the group, **reply to any image with `/sticker`** → the Community bot sends
   back a 512px `.webp` sticker file.
2. Open **@Stickers** → `/newpack` → name it → **send it the `.webp` file** → assign
   an emoji → repeat → `/publish`.
3. To add more later: `/addsticker` → pick your pack → send the new `.webp`.

Now share the pack link in your channel so members can add it.

---

## Step 10 — Token gating (optional, from the blueprint)

If you're holder-gating entry, add **@collablandbot** as a group admin, connect it
at collab.land, set your token mint + minimum balance, and share **only** the
bot-generated invite link. Full steps are in [`BLUEPRINT.md`](BLUEPRINT.md).

---

## Daily-driver cheat sheet

| You want to…                          | Do this                                 |
| ------------------------------------- | --------------------------------------- |
| See your coin's price                 | `/price` (or `/price <mint>`)           |
| Full stats                            | `/stats`                                |
| Check if a token is a rug             | `/scan <mint>` or just paste it         |
| Make a meme                           | reply to a photo: `/meme top \| bottom` |
| Make a sticker                        | reply to a photo: `/sticker`            |
| Refresh admin anti-impersonation list | `/refreshadmins`                        |
| Restart a bot                         | `sudo systemctl restart degen-<name>`   |
| Read a bot's logs                     | `journalctl -u degen-<name> -f`         |

---

## Common gotchas

- **Bot ignores commands in the group** → privacy mode still ON. Re-do
  `/setprivacy → Disable`, then remove & re-add the bot.
- **Can't delete/ban** → bot isn't admin, or missing that specific right.
- **`/price` says no data** → token has no DexScreener pair yet (too new/illiquid).
- **Scanner says "couldn't find a report"** → RugCheck has no data for that mint yet.
- **Stickers rejected by @Stickers** → send the `.webp` as a _file_, not a compressed photo.
- **Bot dies on reboot** → you used `tmux`, not systemd. Use Step 7 for permanence.
- **Token leaked** → `/revoke` in @BotFather immediately and update the env var.

---

> ⚠️ **Defensive tooling.** The scanner protects members from rugs/honeypots; a
> "low risk" verdict is never a guarantee — always DYOR. Guardian's moderation
> actions (mute/kick/ban) require you to be a legitimate admin of your own group.
