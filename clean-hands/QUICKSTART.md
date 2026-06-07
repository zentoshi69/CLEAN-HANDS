# ⏱️ Live in ~1 hour — the no-decisions runbook

You do the Telegram taps (only you can — they need your account). The scripts do
everything else. Follow top to bottom. Recommended settings are pre-chosen; just
match them.

---

## Part A — In the Telegram app (~15 min)

### A1. Make 3 bots in @BotFather

Open **@BotFather**, and do this **3 times**:

| #   | Display name    | Username (must end in `bot`) | Label this token |
| --- | --------------- | ---------------------------- | ---------------- |
| 1   | Degen Guardian  | `yourname_guardian_bot`      | **GUARDIAN**     |
| 2   | Degen Scanner   | `yourname_scanner_bot`       | **SCANNER**      |
| 3   | Degen Community | `yourname_community_bot`     | **COMMUNITY**    |

For each: send `/newbot` → name → username → **copy the token** (`8012…:AAH…`).
Then send `/setprivacy` → pick the bot → **Disable** (all 3). _Privacy must be OFF
or the bots can't see group messages._

### A2. Get your numeric ID

Message **@userinfobot** → it replies with a number like `11122233`. That's your
`TG_ADMIN_IDS`. (Add other admins later, comma-separated.)

### A3. Create the group + channel (recommended structure)

1. New **Channel** (this is where YOU post calls/contracts — single source of truth).
2. New **Group**, then Channel → **Edit → Discussion → link the group**.
3. Group → **Edit → set these** (copy exactly):

| Setting                               | Set to          | Why                                   |
| ------------------------------------- | --------------- | ------------------------------------- |
| Chat history for new members          | **Visible**     | upgrades to supergroup (bots need it) |
| Slow Mode                             | **30s**         | blunts raids/flood                    |
| Who can add members                   | **Admins only** | stops scammers mass-adding alts       |
| Your own admin → **Remain anonymous** | **ON**          | you can't be cloned/doxxed by handle  |

4. **Pin a message** with: the real admin @usernames + the line
   **"⚠️ Admins NEVER DM first. Anyone who DMs you is a scammer."** + your official
   contract address.

### A4. Add the 3 bots as admins

Group → **Administrators → Add Admin** → add each bot with:

- **Guardian** → Delete messages ✅ Ban users ✅ Restrict members ✅
- **Scanner** → Delete messages ✅
- **Community** → no rights needed (add anyway, harmless)

---

## Part B — On a server (~10 min) — get it running

You need an always-on Linux box (a $5/mo VPS from Hetzner/DigitalOcean/Vultr is
perfect; a laptop works only while it's on). SSH in, then:

```bash
# 1. get the bots onto the server (pick one):
git clone https://github.com/zentoshi69/clean-hands.git && cd clean-hands
#   …or scp just the bots/ folder up.

# 2. one command — installs everything and asks for your 5 values:
bash quickstart.sh
```

`quickstart.sh` will prompt for your **admin ID**, the **3 tokens**, and an optional
**default mint**, save them to `.env`, and launch all three bots. You should see
"…starting…" lines. **Test in your group now:**

- send `/help` → Community replies
- paste any Solana contract address → Scanner posts a safety verdict
- join with a second account → Guardian gates it with a CAPTCHA button

Ctrl-C stops them (fine for testing).

### Make it permanent (survives reboots / closing SSH)

```bash
sudo bash install-systemd.sh
```

That's it — the bots now run 24/7 and auto-restart. Useful later:

```bash
journalctl -u degen-guardian -u degen-scanner -u degen-community -f   # live logs
sudo systemctl restart degen-community                                # restart one
```

---

## Part C — let the bot configure the group for you

1. In the group, type **`/setup`** (as an admin). Guardian will, in one shot:
   - lock "add members" to admins only,
   - post **and pin** the safety rules ("admins never DM", "never share your seed phrase"),
   - arm the impersonation guard, and
   - hand you a fresh invite link.

   _(For pinning to work, make sure Guardian has the "Pin Messages" admin right.)_

2. **The command menus register themselves** — you do **not** need @BotFather
   `/setcommands`. Just type `/` in the group to see them.

   _(Optional, one shot: `python configure.py` pushes every BotFather setting the
   Bot API allows — names, descriptions, command menus, default admin rights, and
   the Mini App menu button — for all three bots at once. Run it after filling
   `.env`. It prints the few items you must still set by hand.)_

3. The only owner-only toggles a bot can't set (do these by hand once):
   **Slow Mode 30s**, **Anonymous Admin ON**, and linking a **Discussion channel**.

4. **Turn on 2FA** for every admin account (Telegram → Settings → Privacy →
   Two-Step Verification). _Single most important security step — a hijacked
   admin beats every bot._

> Raid hitting you? An admin types **`/lockdown`** to mute everyone instantly,
> **`/unlock`** to lift it.

---

## Part D — the Mini App (staking · leaderboard · referrals)

Optional but it's the "cool stuff." Full guide: [`miniapp/README.md`](miniapp/README.md).
Short version:

```bash
cd miniapp && python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python server.py                         # runs on :8080
cloudflared tunnel --url http://localhost:8080   # gives you an https URL
```

Then **@BotFather → `/newapp`** → your Community bot → paste that HTTPS URL →
short name `app`. Put the URL in `bots/.env` as `MINIAPP_URL` (+ `MINIAPP_BOT_USERNAME`),
restart the community bot, and `/app` opens it. Points/staking/referrals all work
out of the box.

---

## If something's off

- **Bots ignore commands** → privacy mode still ON. `/setprivacy → Disable`, then
  remove & re-add the bot.
- **Can't delete/ban** → the bot isn't admin, or is missing that specific right.
- **`/price` says no data** → that token has no DexScreener pair yet.
- **Bot dies when you close SSH** → you skipped `install-systemd.sh`. Run it.

Full reference: [`README.md`](README.md) · security posture: [`THREAT-MODEL.md`](THREAT-MODEL.md)
· architecture: [`BLUEPRINT.md`](BLUEPRINT.md)
