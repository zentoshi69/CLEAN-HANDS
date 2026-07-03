---
name: vps-deploy-safety
description: MANDATORY pre-flight and verification protocol before ANY change on a shared multi-project VPS — deploys, proxy/edge edits, port bindings, service or container restarts, systemd changes, cert operations. Use whenever touching nginx, Caddy, docker, systemd, ports, DNS, or TLS on a server that hosts more than one product, or when shipping a new product to an existing box. Exists so that building something NEW never breaks something OLD.
---

# VPS Deploy Safety — never break a neighbor

## The Law

A shared box is a city, not a sandbox. **Before any change runs, name its blast
radius** (which ports, which shared files, which networks, which domains).
**After any change runs, prove every neighbor still breathes** (full-domain
sweep, not just your URL). No exceptions — especially not for "tiny" changes.
The outage this skill is built from started as a one-line CSP tweak.

## The incident this skill is built from (2026-07-03, srv1505584)

One box hosted ~8 products behind ONE dockerized Caddy with ONE shared
Caddyfile. Over two weeks, independent agent sessions each "improved" their own
project and, step by step, produced a recurring whole-box outage:

1. cleanhands was migrated systemd/SQLite → docker/postgres, but the old stack
   was left running (enabled systemd unit, `.env` mutated to
   `STAKE_HOST=172.19.0.1`, repo scripts still reinstall it).
2. A later Caddyfile edit for ANOTHER project silently dropped the cleanhands
   blocks — cleanhands traffic died days before anyone noticed.
3. A "csp-fix" edit + container recreate restarted the shared edge at night.
4. Recovery attempts trusted the repo docs (which described the RETIRED
   architecture) and a "last-good" backup that did not actually contain the
   victim's config — each fix broke a different subset of products.
5. Three api containers, two proxies configured for 80/443, and zero records
   of what was actually live made every diagnosis start from zero.

Every rule below removes one link of that chain.

## Anti-assumptions — each of these lies caused real downtime

1. **"The repo docs describe production."** They describe history. The BOX is
   the only truth: `ss -ltnp`, `docker ps`, `systemctl list-units`, and the
   registry (below) outrank every README, deploy script, and your memory.
2. **"The port is probably free."** Check `ss -ltnp` before binding anything.
   Two things configured for the same port = a time bomb that detonates at the
   next reboot, when the boot race picks a random winner.
3. **"I'm only touching my project's config."** Not true when the file is
   shared. A shared Caddyfile/nginx.conf has the blast radius of EVERY domain
   inside it. Enumerate them all before editing; verify them all after.
4. **"The old stack is off."** Migrations leave corpses: enabled systemd units,
   deploy scripts that reinstall them, mutated `.env` files. A corpse that can
   restart WILL restart. Retire explicitly: stop + disable + `--restart=no` +
   patch the scripts that resurrect it — same day, in code.
5. **"Restart is basically reload."** Restarting a shared edge drops every
   domain on the box. Validate (`nginx -t` / `caddy validate`), then RELOAD.
6. **"My URL works, so the change worked."** The change worked when EVERY
   domain on the box returns its expected status. Sweep them all (below).
7. **"This backup is last-good."** A backup is only "last-good" for YOUR
   product if you grep it and see your product's config inside. Verify before
   restoring — restoring the wrong backup took a whole box down.
8. **"A healthy container is the serving container."** Three healthy api
   clones ran for days; only the one wired into the edge served anyone. One
   container name per role; wire the edge to the NAME, not an IP.
9. **"Both datastores are probably the same."** Before switching traffic
   between stacks, check which store the traffic actually went to (file
   mtimes, request logs, db activity). Serving a stale store = silent money
   corruption in a staking product.
10. **"127.0.0.1 means the same thing everywhere."** Inside a container,
    `127.0.0.1` is the container. Host-published loopback ports are NOT
    reachable from containers. Containers reach each other by name on a shared
    docker network; hosts reach containers via published ports.
11. **"grep -r searched those configs."** `sites-enabled/` is symlinks and
    lowercase `-r` SKIPS symlinked files — it reports nothing and you conclude
    "no matches". Use `grep -R`, or `cat` the files.
12. **"I can paste anything into a root shell."** Interactive bash
    history-expands `!!` INSIDE double quotes — it mangled a rollback command
    mid-incident. Paste-blocks: no `!` characters, `set -x`, one purpose per
    block, print state before mutating it, end with verification.

## Mandatory pre-flight — run BEFORE planning any change

```bash
ss -ltnp | grep -E ':80 |:443 '     # who IS the edge (do not assume)
docker ps --format 'table {{.Names}}\t{{.Ports}}\t{{.Status}}'
systemctl list-units --type=service --state=running --no-pager | grep -iv -E 'systemd|dbus|ssh|cron|getty|journal'
grep -RhoE 'server_name [^;]+' /etc/nginx/sites-enabled/ 2>/dev/null | sort -u
docker ps -q --filter publish=80 --filter publish=443
cat /root/INFRA.md 2>/dev/null      # the registry — reconcile it FIRST if it disagrees with the above
```

If reality and `/root/INFRA.md` disagree, updating the registry IS the first
task — you cannot plan a safe change from a wrong map.

## The registry: /root/INFRA.md

Single source of truth ON the box. Contains: (a) who owns 80/443, (b) the
domain → upstream table with expected status codes, (c) retired stacks that
must never be started, (d) the port ledger of host-published ports.
**Every infra change updates the registry in the same session.** An agent that
changes infrastructure without updating the registry has not finished.

## Protocols by action

### Editing a shared edge config (Caddyfile / nginx vhosts)
1. Timestamped backup of the file. Print the backup path.
2. Edit ONLY your product's block / vhost file. Appending a new block is safer
   than modifying the file body.
3. Validate → reload (never restart). If validation fails, restore the backup
   immediately — never leave a shared config broken on disk.
4. Full-domain sweep (below). Any neighbor regression = restore backup now,
   investigate later.

### Shipping a NEW product to the box
- Own docker network, own container names (`<product>-<role>`), own volumes.
- Publish ONLY high loopback ports found free by scanning (`ss -ltn`), or no
  host ports at all (edge reaches the container by name on its network).
- NEVER publish 80/443. NEVER install another proxy. The box has ONE edge;
  integrate by adding a block/vhost to it under the shared-edge protocol.
- Register the product in /root/INFRA.md (domains, upstreams, ports).

### Migrating / replacing a stack
1. Freshness check BOTH datastores before switching anything.
2. Switch traffic; verify.
3. Retire the loser the same day: stop + disable + `--restart=no`, AND patch
   every script that can resurrect it (deploy scripts, systemd presets).
4. Update /root/INFRA.md: add the new, list the old under RETIRED.

### Debug / experiment containers
- Name them `<product>-test-*`, run with `--restart=no`, remove within 24h.
- Never leave a debug clone able to outlive the session that created it.

## Post-change verification sweep (mandatory, blocking)

Derive the domain list from the LIVE edge config plus the registry — not from
memory — and curl every one:

```bash
for d in $(grep -RhoE 'server_name [^;]+' /etc/nginx/sites-enabled/ 2>/dev/null | sed 's/server_name //' | tr ' ' '\n' | sort -u); do
  printf '%s -> %s\n' "$d" "$(curl -s -m 10 -o /dev/null -w '%{http_code}' "https://$d/")"
done
# Caddy edge: list hostnames from the Caddyfile site blocks and curl each.
```

Compare against the expected codes in /root/INFRA.md. A regression on ANY
domain — including ones you "didn't touch" — means rollback, not "ship and
see". You are not done until the sweep matches the registry.

## Rollback discipline

- No mutation without a printed, timestamped backup path.
- Rollback must be a prepared single command, not an improvisation.
- If two rollbacks in a row don't restore service, STOP changing things and
  map the box again from scratch (pre-flight) — you are operating on a wrong
  mental model, and more edits deepen the hole.

## Repo-specific tripwires (CLEAN-HANDS)

- `clean-hands/deploy/deploy.sh`, `clean-hands/deploy/redeploy.sh`,
  `clean-hands/install-systemd.sh` are LEGACY bring-up for a fresh box. They
  are guarded with `FORCE_LEGACY=1` — do not override the guard on a box that
  already serves production. They install a host Caddy (a second proxy) and
  enable the retired systemd/SQLite staking stack.
- Production cleanhands = docker: `cleanhands-api` (+ postgres + redis + site
  containers) behind the box's single edge. The API publishes a loopback port
  for host debugging only; the edge reaches it by container name.

## Shared-edge tenancy — the recurring cleanhands 502 (READ BEFORE ANY EDGE WORK)

The single most common outage on this box: **cleanhands.fun 502s whenever
someone works on a neighbor (fine-traders / co-muse).** It is NOT a cleanhands
bug and NOT the game code — it is cross-tenant edge coupling. Root cause,
confirmed 2026-07-03:

- The ONE edge that owns 80/443 lived *inside fine-traders' compose*
  (`/root/FINEtrader-app/deploy`). A `docker compose up` on fine-traders
  **recreates the edge**, which rejoins only its own network and **loses
  `cleanhands-prod-net`**. The edge can then no longer resolve `cleanhands-api`
  (`dial tcp: lookup cleanhands-api ... server misbehaving`) → every cleanhands
  domain 502s while the app containers stay perfectly healthy.

The tenancy contract (enforced by `clean-hands/deploy/edge/`):

1. **The edge is tenant-neutral.** It must NOT live in any product's compose.
   Never bring the edge up/down as a side effect of a product deploy.
2. **Every app owns its own imported Caddy file** (`/etc/caddy/sites/<app>.caddy`
   via `import sites/*.caddy`). Never inline one product's routes into another
   product's Caddyfile — that is how blocks get clobbered.
3. **Every app self-attaches to the shared edge network** from its OWN compose
   (`external: true`). Never rely on a manual `docker network connect` — it
   evaporates on the next recreate.
4. **Recreating or relocating the edge is a full-domain-sweep event.** After it,
   curl EVERY domain in the DOMAINS table, not just the one you touched.

Standing defenses on the box — **DO NOT DISABLE, do not flag as rogue crons:**

- `edge-reattach.timer` (30s): additively re-attaches whoever owns :443 to
  `cleanhands-prod-net`. Never restarts/reloads the edge, never edits config —
  it is the safety net that auto-heals the 502 above within 30s.
- `edge-guard.timer` (60s): read-only; pages on any domain drop and says whether
  the app or the edge failed.
  Both are installed from `clean-hands/deploy/edge/install.sh` and registered
  under "STANDING DEFENSES" in `/root/INFRA.md`.

If cleanhands (or any tenant) is 502 while its container is healthy, the fix is
almost always `docker network connect <its-net> <edge>` + curl-sweep — the app
is fine, the edge lost the route. Then let the standing timers keep it attached.
