# 🛡️ Degen Community Setup Blueprint — Secure + Anonymous

A step-by-step build for a crypto/trading community that keeps scammers out, hides
you as the owner, and stops admin impersonators. Work top to bottom.

---

## 0. The core architecture decision (do this first)

Don't run one big group. Run the **"Channel + Chat" pattern** that every serious
crypto community uses:

| Surface                       | Purpose                                   | Who can post     |
| ----------------------------- | ----------------------------------------- | ---------------- |
| **Announcement Channel**      | Alpha, contract addresses, official calls | You only         |
| **Discussion Group** (linked) | Member chat, the "degen pit"              | Verified members |
| **Optional VIP Group**        | Token-gated inner circle                  | Holders only     |

Why: the channel becomes the **single source of truth**. Members learn that any
"contract address" or "claim link" not in the channel is a scam. That one habit
kills most rug/drainer attempts. Link the group to the channel via the channel's
_Discussion_ setting.

---

## 1. Hide yourself as owner (anonymity)

Telegram leaks owner identity in three places. Close all three:

1. **Use a dedicated account.** Create a separate Telegram account on a number that
   isn't tied to your real identity (a fresh eSIM / number, not your personal SIM).
   This account owns everything. Never post from your personal account.
2. **Enable Anonymous Admin.** In group → Administrators → your admin entry → toggle
   **"Remain anonymous."** Your messages then post as the group name, not your handle.
   The channel already posts anonymously as the channel by default.
3. **Lock your privacy settings** on the owner account: Settings → Privacy & Security →
   set _Phone Number_, _Last Seen_, _Profile Photo_, and _Forwarded Messages_ to
   **Nobody** (or My Contacts). Turn on **"Restrict messages from non-contacts."**

> Reality check: anonymity holds against members and casual snoops. It does **not**
> defeat a subpoena or a determined chain-analysis adversary. If your threat model is
> that serious, separate your finances and identity at the wallet/legal layer too.

---

## 2. Impersonation protection (the #1 attack on members)

Scammers clone your admins — same name, same pic — then DM members "support."
Defenses, layered:

- **Make the real admin list visible & pinned.** Pin a message listing every admin's
  exact @username. Add the rule: _"Admins NEVER DM first. Ever."_
- **Apply for the official verified badge** if your project qualifies
  (Telegram's verification program), so the real channel carries the checkmark.
- **Anonymous admins can't be cloned** — another reason to enable #1.2 above.
- **The Guardian bot** auto-flags any joiner whose name/username resembles
  an admin's, before they can work the room.
- **Disable "Add Members" for non-admins** so scammers can't mass-add their alts.

---

## 3. The anti-scam stack

Layer these — no single bot is enough.

### Layer A — Entry gate (CAPTCHA + mute)

- **Shieldy** (`@shieldy_bot`) or the included **Guardian bot** — new members are
  muted and must solve a button/math CAPTCHA or get kicked. Stops join-and-spam bots.

### Layer B — Moderation + analytics

- **Rose** (`@MissRose_bot`) — flexible rules, blocklists, warns, flood control.
  _Tune blocklists carefully:_ words like "investment" or "airdrop" are normal in
  crypto, so prefer pattern + behavior rules over blunt keyword bans.
- **Combot** — mature anti-spam + analytics; good as the second layer in big groups.

### Layer C — Link / file threat scanning

- **Dr.Web bot** — scans shared files and URLs for malware/phishing. Useful once you
  have file sharing on.

### Layer D — Token gating (vetted entry)

- **Collab.Land** (`@collablandbot`) — 40+ chains, mature, kicks members who sell
  below the threshold automatically. Best default.
- **Guild.xyz** — flexible multi-requirement gating if you want roles/tiers.
- Setup pattern: set Group → _Chat History for New Members_ to **Visible** (this
  upgrades it to a supergroup so the bot works), add the gating bot as admin, define
  the token contract + minimum balance, then share **only the bot-generated invite link.**

> ⚠️ Verify the gating bot's exact @username. `@collablandbot` is real; lookalikes
> like `@collab1andbot` are impostors that phish wallet connections.

### Layer E — Custom Guardian bot (included)

The `guardian_bot.py` here covers entry CAPTCHA, newbie link/forward blocking,
scam-phrase deletion ("DM me," "seed phrase," "connect wallet," etc.), and admin
impersonation flagging. Run it alongside Rose for defense-in-depth.

---

## 4. Group hardening checklist

- [ ] Slow mode ON (10–30s) to blunt raids and flood spam.
- [ ] "Add Members" → **Admins only.**
- [ ] New members **can't** send media/links until verified (Guardian handles 24h window).
- [ ] Pin: admin list + "admins never DM first" + official contract address.
- [ ] Disable saving/forwarding from the channel if your alpha is paid.
- [ ] Every admin uses 2FA (Settings → Privacy → Two-Step Verification) + a strong
      cloud password. A hijacked admin account is game over.
- [ ] Keep a written **incident playbook**: if an admin is compromised → demote
      immediately, rotate the invite link, pin a warning.

---

## 5. Deploy the Guardian bot

```bash
pip install "python-telegram-bot[job-queue]"

export TG_BOT_TOKEN="123456:ABC..."        # from @BotFather
export TG_ADMIN_IDS="11111111,22222222"    # your numeric Telegram user id(s)
python guardian_bot.py
```

Then in Telegram:

1. **@BotFather** → `/newbot` → grab the token.
2. **@BotFather** → `/setprivacy` → **Disable** (so it can read group messages to scan).
3. Add the bot to your **discussion group** → promote to admin with **Delete messages +
   Ban users + Restrict members**.
4. Run `/refreshadmins` once so it learns who the real admins are.

Host it on any cheap always-on box (a $5 VPS, Fly.io, Railway, a Raspberry Pi).
Swap the in-memory state for Redis before you scale past a few thousand members.

---

## Suggested build order

1. Channel + linked discussion group, owner anonymity (Sections 0–1).
2. Pin admin list + anti-impersonation rules (Section 2).
3. Shieldy/Guardian gate + Rose moderation (Section 3 A–B, E).
4. Token gating if you're holder-gated (Section 3D).
5. Harden + write the incident playbook (Section 4).
