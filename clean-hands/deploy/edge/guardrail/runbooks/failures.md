# Failure catalog — shared-edge / ingress class

Symptom-first index of the failure modes this guardrail exists to kill.
Each entry: how it presents · root cause · immediate fix · what prevents it
structurally. Sources: the 2026-07-03 srv1505584 outage chain and the
recurring cleanhands 502 (see `.claude/skills/vps-deploy-safety/SKILL.md`).

## 1. EADDRINUSE / bind: address already in use on 80/443

- **Presents:** a proxy or app fails to start; or worse, everything starts
  fine and the failure appears at the NEXT reboot when the boot race picks a
  different winner.
- **Cause:** two things configured for the same host port — a second proxy,
  an app compose publishing `80:80`/`443:443`, or a host nginx/caddy beside
  the docker edge.
- **Fix now:** `ss -tulpn | grep -E ':80 |:443 '` → identify the squatter →
  stop the one that is NOT the registered ingress (check `/root/INFRA.md`).
  Never "fix" it by moving the real edge.
- **Prevented by:** Law 2 (one edge) + `check-compose.sh` (refuses any
  non-ingress compose that maps 80/443, on any interface).

## 2. Silent traffic steal — wrong proxy answering

- **Presents:** a domain serves stale/wrong content or wrong TLS cert while
  every container looks healthy; often appears only after a reboot.
- **Cause:** two proxies both configured for 80/443; whichever bound first
  wins. Or: multiple healthy clones of an app exist and the edge is wired to
  the wrong one by IP.
- **Fix now:** `ss -tulpn` for the real owner; `docker ps -a` for clones;
  wire the edge to the container NAME on the shared network, remove clones.
- **Prevented by:** one edge; one container name per role; edge reaches
  upstreams by name, never IP; ALLOCATIONS.md says which name is real.

## 3. Duplicate hostname → the WHOLE Caddy config drops

- **Presents:** after a reload attempt, EVERY domain on the box 502s/times
  out, not just the app being worked on.
- **Cause:** the same site address appears in two blocks (e.g. an apex block
  and a "www redirect" block that repeats the apex, or two apps claiming one
  domain). Caddy rejects the entire config.
- **Fix now:** `caddy validate` names the duplicate; delete/merge the
  younger claim; validate → reload.
- **Prevented by:** Law 5 (grep before you write), one file per app under
  `sites/`, and `check-compose.sh`'s duplicate-hostname scan.

## 4. Edge recreate loses an app's network → 502 with healthy app

- **Presents:** `cleanhands.fun` (or any tenant) 502s the moment someone
  deploys a NEIGHBOUR; the app's containers are perfectly healthy. Edge log:
  `dial tcp: lookup <container> ... server misbehaving`.
- **Cause:** the edge lived inside one product's compose; that product's
  `up` recreated the edge, which rejoined only its own network and lost the
  victim's (manual `docker network connect` evaporates on recreate).
- **Fix now:** `docker network connect <victim-net> <edge>` + curl-sweep.
  On this box `edge-reattach.timer` does exactly this within 30s — do not
  disable it.
- **Prevented by:** tenant-neutral edge in its OWN stack; every app
  self-attaches to the shared network `external: true` in its OWN compose.

## 5. Empty cert volume → mass re-issue → Let's Encrypt rate limit

- **Presents:** after an edge recreate/relocation, some or all domains show
  TLS errors; Caddy logs show ACME failures / `too many certificates`.
  Recovery is blocked for DAYS by the rate limit, not by anything on the box.
- **Cause:** the new edge container was started with a fresh `/data` volume
  instead of the sacred one; Caddy re-requests every cert at once.
- **Fix now:** stop; find the old volume (`docker volume ls`,
  `docker inspect` the old container); mount THAT volume; reload. If already
  rate-limited, wait it out — do not thrash ACME with retries.
- **Prevented by:** Law 6 — verify the volume NAME matches the M0 inventory
  before any edge recreation; `docker-compose.edge.yml` documents reusing it.

## 6. Restart-instead-of-reload drops every domain

- **Presents:** a brief box-wide outage exactly when someone "just applied a
  config change"; repeated nightly if a script does it.
- **Cause:** `docker restart <edge>` / `systemctl restart` on a shared edge
  instead of validate + reload. A restart also re-rolls networks (see #4).
- **Fix now:** nothing — it already happened. Verify all domains, then fix
  the habit/script.
- **Prevented by:** Law 7 — `caddy validate` then `caddy reload`, encoded in
  every runbook step and deploy script.

## 7. The corpse that restarts

- **Presents:** a retired stack (old systemd unit, host caddy, legacy
  compose) is mysteriously running again — stealing a port, serving stale
  data, or double-writing.
- **Cause:** stop without disable; disable without patching the
  deploy/install scripts that re-enable it; `restart: unless-stopped` on a
  container someone `docker start`ed once.
- **Fix now:** stop + disable + `--restart=no` + grep /root for the script
  that resurrected it and guard it (`FORCE_LEGACY=1` pattern).
- **Prevented by:** migration.md steps 9–12 (retire in code, same session);
  RETIRED list in `/root/INFRA.md`; `state-snapshot.sh` making the corpse
  visible the day it reappears.

## 8. "Fixed my app, killed a neighbour"

- **Presents:** your domain works; a domain you "didn't touch" is down.
  Discovered days later.
- **Cause:** shared-file edit (one Caddyfile for all tenants), or a
  compose/name/port collision with a neighbour.
- **Fix now:** restore the neighbour's config from its `.bak`, reload,
  sweep.
- **Prevented by:** Law 3 (additive only — one file per app), and the
  mandatory post-change sweep of EVERY domain (edge-guard watches
  continuously and pages within 60s).

## 9. Loopback confusion — "it works on the host but the edge gets 502"

- **Presents:** `curl 127.0.0.1:PORT` on the host works; the edge can't
  reach the app.
- **Cause:** inside a container, `127.0.0.1` is the container. A
  host-published loopback port is invisible to the edge; `host.docker.internal`
  or gateway-IP hacks then get hardcoded and rot.
- **Fix now:** put the app on the shared edge network and point the edge at
  `container:port` by name; keep the loopback publish for host debugging
  only.
- **Prevented by:** Law 4 — edge reaches containers by name; loopback
  publishes are for humans, never for routing.

## 10. Inventory theatre — acting on docs/memory instead of the box

- **Presents:** every other failure in this file, eventually. Recovery
  attempts that trust a README describing a retired architecture make the
  outage WORSE (that is exactly how 2026-07-03 escalated).
- **Cause:** README/deploy scripts describe history; the box is the only
  truth. Also: `grep -r` on `sites-enabled/` silently skips symlinks (use
  `grep -R`), and a "last-good" backup may not contain the victim's config.
- **Fix now:** stop editing; re-run the full M0 inventory; reconcile
  `/root/INFRA.md`; only then plan.
- **Prevented by:** consolidation.md M0 gate; `state.md` regenerated daily
  and after every deploy; Law 1 and Law 9 (never fabricate inventory — if a
  command won't run, you may not be on the box you think you're on).
