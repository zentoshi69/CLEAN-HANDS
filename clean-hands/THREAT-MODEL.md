# 🔐 Threat Model — what these bots do and do NOT protect against

Read this once. Security is layers + operations, not one bot. Anyone promising
"hackers can do nothing" is selling snake oil. The realistic goal is to **close
every cheap attack vector** and make sure **no single failure drains holders or
de-anonymises you.**

---

## ✅ What this stack defends against

| Attack                                             | Defense                                                                      | Layer          |
| -------------------------------------------------- | ---------------------------------------------------------------------------- | -------------- |
| Join-spam / raid bots                              | CAPTCHA gate: muted on entry, kicked if unverified                           | Guardian       |
| Admin impersonators ("support" DMs)                | Name/username similarity flag on join + on message; pinned "admins never DM" | Guardian + ops |
| Wallet-drainer / phishing links                    | Blocklist + punycode/IDN detection; deleted from **any** non-admin           | Guardian       |
| New-account link/forward spam                      | Links/forwards blocked for first 24h after join                              | Guardian       |
| Edit-after-verify bypass                           | Edited messages are re-scanned, not just new ones                            | Guardian       |
| Scam-phrase bait ("seed phrase", "connect wallet") | Pattern delete + warn → ban at 3 strikes                                     | Guardian       |
| Rug / honeypot contract addresses                  | RugCheck scan on every posted mint; high-risk auto-deleted                   | Scanner        |
| Repeat offenders                                   | 3-strike auto-ban                                                            | Guardian       |
| Host compromise via a bad dependency               | systemd sandbox (no new privs, read-only FS, syscall filter)                 | Ops            |

---

## ❌ What it does NOT (and cannot) protect against

- **A compromised admin account.** If an attacker gets an admin's session, the
  bots see them as a legit admin. **Mitigation: every admin runs 2FA + a strong
  cloud password.** Keep the incident playbook (below) ready.
- **A determined, funded adversary** doing chain analysis or legal subpoenas.
  Bot-level anonymity stops members and snoops, not a forensic investigation.
- **Zero-day drainer domains** registered minutes ago. The blocklist is a layer,
  not a guarantee — behavioural rules (new-member gating, "admins never DM")
  catch what the list misses.
- **Members who ignore the rules** and connect their wallet to a site from a DM.
  No bot can stop a user who acts off-platform. Education (pinned rules) is the
  control here.
- **Telegram-side account takeover** of _your_ owner account (SIM-swap, phishing).
  Mitigated by a dedicated number + 2FA, not by the bots.

A green Scanner verdict is **never** financial advice or a safety guarantee. DYOR.

---

## 🧱 The operational controls that matter most (do these — they're free)

These outrank any bot feature:

1. **Channel + linked discussion group** so the channel is the single source of
   truth. Any contract/claim link not in the channel = scam. (See `BLUEPRINT.md`.)
2. **Anonymous admin** toggle on every admin → you can't be cloned or doxxed by handle.
3. **2FA on every admin account** + strong cloud password. This is the single
   highest-leverage control; a hijacked admin is game over.
4. **"Add members" = admins only** so scammers can't mass-add alts.
5. **Slow mode (10–30s)** to blunt floods.
6. **Pin** the real admin @usernames + "admins NEVER DM first" + official contract.
7. **Dedicated owner account** on a number not tied to your identity.

---

## 🚨 Incident playbook (keep this pinned for admins)

If an admin account is compromised:

1. **Demote the account immediately** (or have the owner remove it).
2. **Rotate the group invite link** (old links may be circulating).
3. **Pin a warning**: "Admin X compromised — ignore all DMs from them."
4. **Revoke any bot tokens** that account could have seen: `/revoke` in @BotFather,
   then update `.env` and `systemctl restart` the bots.
5. Review recent admin actions (bans/unbans/pins) and reverse anything malicious.

If a bot token leaks: `/revoke` in @BotFather → update `.env` → restart the unit.
Tokens live only in `.env` (git-ignored) and the systemd `EnvironmentFile`; never
commit them, never paste them in chat.

---

## Secrets handling

- Real secrets live in `bots/.env` (git-ignored) — only `.env.example` is committed.
- Lock it down on the host: `chmod 600 .env`.
- In production, prefer the systemd `EnvironmentFile=` (already wired in the units)
  so secrets never touch your shell history.
- Rotate tokens on any suspicion of exposure.
