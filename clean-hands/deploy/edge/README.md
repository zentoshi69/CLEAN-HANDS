# Shared edge — every project on its own, none can knock another down

This directory is the permanent fix for the recurring **"cleanhands.fun 502s
whenever someone works on fine-traders/co-muse"** outage.

## What actually breaks (confirmed 2026-07-03)

The box runs several products behind **one** Caddy edge that owns 80/443. That
edge lived **inside fine-traders' compose** (`/root/FINEtrader-app/deploy`), and
cleanhands/co-muse were wired into it by hand. So:

- `docker compose up` on fine-traders **recreates the edge**; it comes back on
  fine-traders' network only and **loses `cleanhands-prod-net`** →
  `dial tcp: lookup cleanhands-api ... server misbehaving` → **502**, while
  `cleanhands-api` sits there perfectly healthy.
- One shared Caddyfile means one edit can clobber another project's block.

Guests get evicted every time the landlord redecorates. The cure is to stop
making anyone a guest.

## Target architecture

1. **The edge is its own stack** (`docker-compose.edge.yml`, deploy in
   `/root/edge/`). No product's `up`/`down` touches it.
2. **One shared external network `edge-net`.** The edge is on it permanently;
   **every app attaches _itself_** via `external: true` in its own compose
   (`edge-net.snippet.yml`). A recreate re-attaches automatically — the manual
   `docker network connect` that evaporated on recreate is gone for good.
3. **One file per project** under `sites/` (`sites/cleanhands.caddy`), pulled in
   by `import /etc/caddy/sites/*.caddy`. Editing one project's file can never
   touch another's. **This is "cleanhands out of fine-traders' Caddy."**
4. **`edge-guard`** (`edge-guard.sh` + timer) polls every domain every 60s and
   pages you the instant one drops — and says whether it's the edge or the app.

```
Internet :443
      │
   edge-caddy  (its own /root/edge stack, on edge-net)
      │  imports sites/*.caddy — one file per project, each project owns its own
      ├─ sites/cleanhands.caddy   → cleanhands-api:8090 · cleanhands-site:80
      ├─ sites/finetrades.caddy   → deploy-server-1:8080
      ├─ sites/co-muse.caddy      → co-muse:3000
      └─ sites/legacy.caddy       → nginx:8443 (internal)
   every upstream is a member of edge-net, attached by its OWN compose
```

## Files here

| File | Goes to (box) | Purpose |
|------|---------------|---------|
| `docker-compose.edge.yml` | `/root/edge/` | tenant-neutral edge stack |
| `Caddyfile` | `/root/edge/Caddyfile` | globals + `import sites/*.caddy` only |
| `sites/cleanhands.caddy` | `/etc/caddy/sites/` (mounted) | cleanhands' OWN routes |
| `edge-net.snippet.yml` | merge into cleanhands compose | app self-attaches to `edge-net` |
| `edge-guard.sh` | `/usr/local/bin/` | read-only domain monitor |
| `edge-guard.domains` | `/etc/edge-guard/domains` | every domain to watch |
| `edge-guard.{service,timer}` | `/etc/systemd/system/` | run it every 60s |

## Install the monitor first (zero-risk, do it today)

Catching the next drop in 60s is worth more than anything else and touches
nothing:

```bash
install -m744 edge-guard.sh /usr/local/bin/edge-guard.sh
install -Dm644 edge-guard.domains /etc/edge-guard/domains
printf 'TG_BOT_TOKEN=...\nTG_CHAT_ID=...\n' > /etc/edge-guard/env && chmod 600 /etc/edge-guard/env
install -m644 edge-guard.service /etc/systemd/system/edge-guard.service
install -m644 edge-guard.timer   /etc/systemd/system/edge-guard.timer
systemctl daemon-reload && systemctl enable --now edge-guard.timer
systemctl start edge-guard.service && journalctl -u edge-guard.service -n20 --no-pager
```

## Migration — do it under `.claude/skills/vps-deploy-safety/SKILL.md`

Every step is backup → change → **validate → reload (never restart)** → curl-sweep
**all** domains. Roll back on any neighbor regression.

**Phase 0 — snapshot (mandatory).**
```bash
mkdir -p /root/edge/sites
cp /root/FINEtrader-app/deploy/Caddyfile /root/edge/Caddyfile.snapshot.$(date +%s)
docker inspect deploy-caddy-1 > /root/edge/deploy-caddy-1.inspect.json   # records its volumes/nets
```

**Phase 1 — shared network + self-attachment (this alone kills the recurring drop).**
```bash
docker network create edge-net 2>/dev/null || true
# attach the edge + every current upstream to edge-net (additive, safe):
for c in deploy-caddy-1 cleanhands-api cleanhands-site-prod-3e94c39 deploy-server-1 co-muse; do
  docker network connect edge-net "$c" 2>/dev/null || true
done
# give the static site a stable alias the Caddyfile can trust:
docker network disconnect edge-net cleanhands-site-prod-3e94c39 2>/dev/null || true
docker network connect --alias cleanhands-site edge-net cleanhands-site-prod-3e94c39
```
Then bake `edge-net` (external) into each app's compose using `edge-net.snippet.yml`
so it survives recreates, and re-`up` each app once to confirm.

**Phase 2 — split the config so projects can't clobber each other.**
Extract each project's block from the live Caddyfile **verbatim** into
`sites/<project>.caddy` (use `sites/cleanhands.caddy` here as the cleanhands
reference), point upstreams at `edge-net` names, then make the top Caddyfile just
`import /etc/caddy/sites/*.caddy`. `caddy validate` → `caddy reload` → curl-sweep.

**Phase 3 — relocate the edge off fine-traders (the real decoupling).**
Bring up `/root/edge/docker-compose.edge.yml` **reusing the existing certs
volume** (so no Let's Encrypt re-issue), then retire the old edge:
```bash
# reuse certs: mount deploy-caddy-1's data volume as caddy_data (see inspect.json)
cd /root/edge && docker compose -f docker-compose.edge.yml up -d
for d in cleanhands.fun app.cleanhands.fun finetrades.io co-muse.xyz; do
  printf '%-22s -> %s\n' "$d" "$(curl -s -m10 -o /dev/null -w '%{http_code}' https://$d/)"
done
# only when the sweep is all-green, stop the OLD edge and remove it from fine-traders' compose:
# docker rm -f deploy-caddy-1   (and delete its service block in /root/FINEtrader-app/deploy)
```
Retire the orphan too: `docker rm -f cleanhands-caddy-test` (8-day-old stray).

**Rollback (any phase):** the old `deploy-caddy-1` + snapshot Caddyfile bring the
previous edge back; `edge-net` attachments are additive and harmless to leave.

**Finish:** update `/root/INFRA.md` — new edge owner, `edge-net`, per-site files,
and move the old inline-in-fine-traders edge to RETIRED.
