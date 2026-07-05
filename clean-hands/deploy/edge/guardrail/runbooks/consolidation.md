# Ingress consolidation runbook — one edge, forever

How to take a shared box from "two proxies, host-published web ports,
EADDRINUSE at boot, silent traffic steal" to "ONE tenant-neutral edge owns
80/443, every app is a file, a guardrail blocks regressions". Executed
milestone by milestone; **every milestone ends at a GATE — a human types the
go-word before you cross it.** Read `.claude/skills/vps-deploy-safety/SKILL.md`
first; it is the law this runbook operates under.

## The Law

1. Every identifier (port, container name, network, volume, hostname) is
   TAKEN until a live command **in this session** proves it FREE. No memory,
   no README, no registry-alone.
2. Exactly ONE proxy owns 80/443 — the app-neutral ingress stack (its own
   directory, e.g. `/root/edge`; **discover the real one in M0, never assume
   the path**). No app compose may ever bind 80/443.
3. Additive only. Fixing app N edits zero lines of the others. New file / new
   site block — never a rewrite of a neighbour.
4. Apps behind the edge publish NO host ports. They join the shared ingress
   network; the proxy reaches `container:port` by name. A genuinely-needed
   host port binds loopback only (`127.0.0.1:PORT:internal`), scanned free
   first.
5. One domain = one site block, globally. A duplicate hostname invalidates
   the ENTIRE Caddy config and drops every site. Grep before you write.
6. The cert volume is sacred. Caddy `/data` must survive every recreation —
   verify it's the SAME volume before touching Caddy. An empty `/data` means
   mass re-issue → Let's Encrypt rate limit → box-wide TLS outage.
7. Validate before load; reload, never restart. `caddy validate` must print
   "Valid configuration"; apply with `caddy reload`.
8. Every file you touch gets a `.bak-YYYYMMDD` first. Before any APPLY step,
   print the exact rollback commands. Type hostnames by hand.
9. STOP and surface if: a plan needs editing another app's config "to make
   room"; `compose up` reports recreating a container you didn't just define;
   an inventory command won't run (e.g. you are not actually on the box).
   Never fabricate inventory or pick "safe-sounding" values.

## M0 — Inventory (read-only; write nothing)

Run every command, keep raw output. This is the ground truth everything else
is checked against. (If `/root/infra/state.md` exists, read it first — then
still run the live confirm; the box outranks the snapshot.)

```bash
ss -tulpn | sort -k5                       # who REALLY holds 80/443 (incl. bare metal)
docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'   # -a: stopped containers still own names+ports
docker compose ls -a
docker network ls && docker volume ls
systemctl list-units --type=service --all | grep -iE 'nginx|caddy|apache|httpd'
grep -RnE '^[a-zA-Z0-9*][a-zA-Z0-9*.-]*\.[a-zA-Z]' /root/*/Caddyfile /root/*/sites/ /etc/caddy/ 2>/dev/null
nginx -T 2>/dev/null | grep -nE 'listen|server_name'   # -T resolves ALL includes; empty = nginx not an edge
cat /root/infra/ALLOCATIONS.md 2>/dev/null || echo "REGISTRY MISSING"
cat /root/INFRA.md 2>/dev/null || echo "INFRA.md MISSING"
```

For the running Caddy container, capture the **two sacred facts**:

```bash
CADDY=$(docker ps --format '{{.Names}}' | grep -i caddy | head -1); echo "caddy=$CADDY"
docker inspect "$CADDY" --format '{{json .Mounts}}' | tr ',' '\n' | grep -i data   # cert volume — memorize its NAME
docker inspect "$CADDY" --format '{{json .NetworkSettings.Networks}}'              # shared ingress network — memorize its NAME
```

Produce the app table: container · internal port · domain(s) · how reached
today (host publish? second proxy? Caddy?) · on the ingress network yes/no.

Classify any second edge, exactly one of:

- **(A)** host nginx proxies Docker apps → its routes migrate into per-app
  Caddy site files, then nginx is retired (M4).
- **(B)** host nginx serves static files → replace with Caddy `file_server`
  or a container behind Caddy; then retire nginx (M4).
- **(C)** nginx is not an edge (`nginx -T` empty / not on 80/443) → nothing
  to retire; note it, skip M4.

**GATE-0:** print the app table, cert-volume name, shared-network name, and
the A/B/C classification. Wait for the go-word.

## M1 — Target lock (read-only; decide, write nothing)

- The one edge: the tenant-neutral ingress stack from M0 (for this box:
  `docker-compose.edge.yml` in its own directory — see
  `clean-hands/deploy/edge/`), sole owner of 80/443, on the **exact network
  the running Caddy is already on** (do not invent a new name; apps join it
  `external: true`, no app ever `create`s it), with the **exact cert volume
  from M0**.
- Per-app plan (additive): the new `sites/<app>.caddy` block
  (`domain { encode zstd gzip; reverse_proxy <container>:<port> }`) and the
  exact compose edit (drop web `ports:`, add the shared network). A
  www-redirect is its own block, never a second apex block.
- The second-edge retirement plan per A/B/C.
- Rollback posture: `.bak-YYYYMMDD` on every file; each app's cutover
  independently reversible.

**GATE-1:** print the full target (site blocks + compose diffs, unapplied)
and the ordered cutover list. Wait for the go-word.

## M2 — Prepare (write NEW files + `.bak` edits; apply NOTHING)

1. Ensure the ingress dir has a Caddy compose that binds only
   `80:80`/`443:443` (+udp), mounts the sacred `/data` volume (verified same
   as M0), mounts `./sites` as a **directory** (never single files), joins
   the shared network `external: true`. If Caddy already lives correctly
   there, leave it untouched.
2. Write every `sites/<app>.caddy` from the M1 lock; `cat` each back. Then:
   `INGRESS_DIR=<dir> /root/infra/check-compose.sh` — the duplicate-hostname
   count must be zero.
3. For each app compose: copy to `.bak-YYYYMMDD`, remove the web `ports:`
   publish, add the shared network (`external: true`). Truly-needed non-HTTP
   ports → `127.0.0.1:PORT:internal` only, scanned free first. `cat` back.
4. Apply nothing. Validate only:
   `docker exec <caddy> caddy validate --config /etc/caddy/Caddyfile`
   → must print "Valid configuration".

**GATE-2:** all files written, `.bak`s exist, duplicate-hostname check zero,
validate passed. Wait for the go-word.

## M3 — Cutover (one app at a time; never batch)

Print the app's rollback BEFORE step (a):
`mv <app>/compose.bak-YYYYMMDD <app>/compose && docker compose -f <app>/compose up -d && docker exec <caddy> caddy reload --config /etc/caddy/Caddyfile`

```
a. docker compose -f <app>/compose up -d
     → output says "Recreating <someone-else>"? STOP — name collision (Law 9).
b. docker exec <caddy> wget -qSO- http://<container>:<port>/ | head    # reachable on the net?
c. docker exec <caddy> caddy reload --config /etc/caddy/Caddyfile
d. curl -sSI https://<domain>        # first-issue cert may take ~60s — do NOT
                                     # roll back a healthy config over a cert race
e. curl -sSI two OTHER live domains  # you broke NOTHING — mandatory
f. append the ALLOCATIONS.md line: date | app | container | port | domain | sites/<app>.caddy
```

Any failure: run that app's printed rollback, restore ONLY its files, stop,
report.

**GATE-3:** all apps cut over, every domain 200/301, full-fleet sweep green
(`edge-guard.sh` domains file is the sweep list). Wait for the go-word.

## M4 — Retire the second edge (only if M0 = A or B)

Only after every app is proven on Caddy:

- **(A)** confirm no domain resolves through nginx anymore, then
  `systemctl stop nginx && systemctl disable nginx`. Re-verify single
  ownership: `ss -tulpn | grep -E ':80 |:443 '`. Leave package+config on
  disk (`.bak` the config); package removal is a later, separately-approved
  task.
- **(B)** cut the static site to Caddy `file_server` (or an internal
  container), verify it serves, then stop+disable nginx as above.

Also retire docker corpses found in M0 (stopped edges, `-test-` strays):
stop + disable + `--restart=no`, and patch any script that can resurrect
them — same session, in code.

**GATE-4:** `ss` shows a single owner of 80/443; all domains still green.
Wait for the go-word.

## M5 — Guardrail (what makes it permanent)

The cutover alone doesn't stop recurrence — the next project would
re-introduce the clash. Install the structural gate from the clean-hands
repo:

```bash
cd <repo>/clean-hands/deploy/edge/guardrail
sudo INGRESS_DIR=<ingress-dir-from-M0> bash install.sh
```

That installs and immediately runs:

1. `/root/infra/check-compose.sh` — exits non-zero if any compose outside
   the ingress dir binds 80/443 (any interface, not exceptable) or publishes
   a non-loopback host port; also fails on duplicate hostnames across the
   ingress config. Wire it into every deploy script.
2. `/root/infra/state-snapshot.sh` + daily timer → `/root/infra/state.md`
   (the M0 inventory, regenerated).
3. `/root/infra/ALLOCATIONS.md` — seeded if missing. **Backfill one line per
   live app from the M0 table now.**
4. These runbooks → `/root/infra/runbooks/` (and into the infra-guardian
   skill's `references/` if that skill exists on the box).
5. A guardrail section in `/root/INFRA.md`.

**GATE-5 (final):** single 80/443 owner confirmed · every domain green ·
`check-compose.sh` passing · `state.md` + registry written · runbooks
installed.

## The permanent model — adding app N+1 after consolidation

1. App compose: join the shared edge network (`external: true`); **no web
   `ports:`** (debug ports `127.0.0.1:` only, scanned free first).
2. `/root/infra/check-compose.sh` → must print OK. Then `docker compose up -d`.
3. One new file: `<ingress>/sites/<app>.caddy` →
   `domain { encode zstd gzip; reverse_proxy <container>:<port> }`
   (grep sites/ for the hostname first — Law 5).
4. `docker exec <caddy> caddy validate` → `caddy reload` (never restart).
5. `curl -sSI` the new domain + two neighbours.
6. Append the ALLOCATIONS.md line + update INFRA.md. Done.

No host port is ever allocated for anything web-facing again, so the whole
EADDRINUSE / double-edge / traffic-steal class is gone by construction — and
`check-compose.sh` blocks anyone who tries to bring it back.
