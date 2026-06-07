# 💸 Hosting — cheapest + easiest (decision pinned)

**Run everything on ONE small VPS. ~$5/month. Done.**

The whole backend — 3 bots + the staking API (which serves the Mini App **and** the
JSON API) + alerts + notifier + Caddy (auto-HTTPS) + SQLite — fits comfortably on a
single 2 vCPU / 4 GB box. That's exactly what `deploy/deploy.sh` + the systemd units
target.

## Cost to launch

| Item                | Pick                                                    | /mo                   |
| ------------------- | ------------------------------------------------------- | --------------------- |
| VPS (2 vCPU / 4 GB) | **Hetzner CX22** (~€4.5) — or DigitalOcean / Vultr ($6) | **~$5**               |
| Database            | **SQLite** on the VPS                                   | $0                    |
| Domain              | you have `cleanhands.fun`                               | $0                    |
| Solana RPC          | **Helius free tier** to start                           | $0 (→ $0–50 at scale) |
| **Total**           |                                                         | **~$5**               |

## How it all connects

```
Telegram users ─┐
                ├─→ Caddy (HTTPS @ app.cleanhands.fun) ─→ staking-api :8090
Website users ──┘        (Mini App at /, API at /api/*)
   cleanhands.fun (Hostinger) embeds site-sdk/clean-staking.js → app.cleanhands.fun/api
        same VPS also runs: guardian · scanner · community · alerts · notifier
                                   └─→ Solana RPC (read-only)
```

- Your **marketing site** stays on Hostinger. Only **`app.cleanhands.fun`** points
  to the VPS (one DNS A record — see `DNS.md`).
- One database (SQLite, on the box) = one source of truth = site + app always in
  sync.

## Setup effort

~10 minutes on the VPS:

```bash
git clone https://github.com/zentoshi69/clean-hands.git && cd clean-hands
cp .env.example .env && nano .env          # tokens + secrets + mint
sudo DOMAIN=app.cleanhands.fun bash deploy/deploy.sh
```

Then DNS (1 record) + @BotFather (`BOTFATHER.md`). Full sequence in `GO-LIVE.md`.

## When to upgrade (only when you actually need it)

A single VPS handles a launch and into the tens of thousands of users. Outgrow it
(very high concurrent writes, multi-region, or you want zero server admin)? The
code **already supports it with env only — no rewrite**:

- `DATABASE_URL=postgresql://…` → switch SQLite → Postgres
- `REDIS_URL=…` → shared nonces + rate limits across workers
- run multiple `uvicorn` workers / instances behind the proxy

## Don't-want-to-touch-servers alternative

Railway / Render / Fly.io will auto-deploy this same code + a managed Postgres for
**~$20–40/mo** — easier ops, higher cost. For **cheapest + easiest at launch the
$5 VPS wins**; migrate later without code changes.

**Verdict:** start on one Hetzner/DO VPS. Scale to managed + Postgres + Redis only
when the numbers force it.
