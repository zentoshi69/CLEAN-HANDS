# ⏰ Cron — the recurring Claude/automation jobs

Two ready scripts (both support `--dry-run` to preview without posting):

- **`bots/clean-gm.py`** — daily "GM" post to the channel ($CLEAN price + stake CTA).
  Reads the public `/api/price`; posts via the Bot API. No keys, no DB.
- **`bots/staking-api/ops-digest.py`** — DMs **you** a daily ops digest: pending
  payouts, active stakers, reconcile status. Runs on the VPS (reads the DB).

## Option A — system cron (simplest)

```cron
# crontab -e  (as your bot user; assumes the venvs from deploy.sh)
# 09:00 daily GM to the channel
0 9 * * *  cd /home/youruser/bots && CLEAN_API=https://app.cleanhands.fun \
  TG_ALERTS_CHAT=@yourchannel venv/bin/python clean-gm.py >> gm.log 2>&1
# 08:30 daily ops digest DM to you
30 8 * * * cd /home/youruser/bots/staking-api && OWNER_CHAT=<your_tg_id> \
  venv/bin/python ops-digest.py >> ../ops.log 2>&1
```

(Tokens/chat come from the systemd `.env` if you `set -a; source ../.env` first,
or add them inline. `reconcile.py` already runs daily via `degen-reconcile.timer`.)

## Option B — OpenClaw Cron Jobs (Claude runs them)

In OpenClaw → **Cron Jobs → Add**, point each at the command above (or have the
agent shell out to it). Then let the agent do the _written_ jobs on top:

| Schedule    | Job                                              | Who                       |
| ----------- | ------------------------------------------------ | ------------------------- |
| Daily 09:00 | `clean-gm.py` (price + CTA)                      | script                    |
| Daily 08:30 | `ops-digest.py` (DM you)                         | script                    |
| Weekly Mon  | "Leaderboard recap + biggest burners" post       | Claude (judgment/writing) |
| On demand   | announcements / AMA answers, drafted for your OK | Claude                    |

Keep Claude **read-only + propose-only** (see `../CLAUDE-OPS.md`): it can post
content and draft actions, but never bans or payouts.

## Preview now (no posting)

```bash
venv/bin/python clean-gm.py --dry-run
staking-api/venv/bin/python staking-api/ops-digest.py --dry-run
```
