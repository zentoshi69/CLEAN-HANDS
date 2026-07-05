# Ingress guardrail — the structural gate that keeps ONE edge, forever

The consolidation cutover (one tenant-neutral Caddy owning 80/443, apps as
`sites/*.caddy` files on a shared network — see `../README.md`) fixes the
recurring EADDRINUSE / double-edge / silent-traffic-steal class **once**.
This directory is what makes it **permanent**: the next project that tries to
bind 80/443 or publish a web host port gets blocked by a script, not by
hoping someone remembers the rule.

Everything here is ADDITIVE and idempotent. Nothing restarts, reloads, or
edits any app.

## Files

| File | Installs to (box) | Purpose |
|------|-------------------|---------|
| `install.sh` | run on box (root) | one-command install of everything below |
| `check-compose.sh` | `/root/infra/` | **the gate** — non-zero exit if any compose outside the ingress dir binds 80/443 (any interface) or publishes a non-loopback host port; also fails duplicate hostnames across the ingress Caddy config |
| `state-snapshot.sh` | `/root/infra/` | regenerates the full box inventory ("M0 table") into `/root/infra/state.md`; read-only |
| `state-snapshot.{service,timer}` | `/etc/systemd/system/` | daily snapshot (also run it after every deploy) |
| `ALLOCATIONS.template.md` | `/root/infra/ALLOCATIONS.md` (if missing) | append-only per-app ledger: date · app · containers · nets · ports · domains · edge config |
| `runbooks/consolidation.md` | `/root/infra/runbooks/` + infra-guardian `references/` | the M0→M5 consolidation runbook (gated, zero-downtime) |
| `runbooks/migration.md` | same | replacing a stack without leaving a corpse |
| `runbooks/failures.md` | same | symptom-first catalog of the whole failure class |

The three runbooks also fill the gap where the infra-guardian skill
references `references/consolidation.md`, `migration.md`, `failures.md` —
`install.sh` copies them into that skill's `references/` dir if the skill
exists on the box (never overwriting existing files).

## Install (on the box, AFTER the consolidation cutover)

```bash
cd <this repo>/clean-hands/deploy/edge/guardrail
sudo INGRESS_DIR=/root/edge bash install.sh   # INGRESS_DIR = the ONE edge stack dir from your M0 inventory
```

The installer refuses to guess `INGRESS_DIR` if it can't detect exactly one
candidate — the box is truth, go look (`ss -tulpn | grep -E ':80 |:443 '`).

## Wire the gate into deploys

Every deploy script runs this before any `docker compose up` or edge reload:

```bash
/root/infra/check-compose.sh || exit 1
```

Exceptions (rare, non-HTTP, genuinely-on-the-wire ports) go in
`/root/infra/port-exceptions` as `<compose-path-glob> <host-port>` lines.
Ports 80/443 can never be excepted.

## Adding app N+1 under the permanent model (the whole cost)

1. App compose: join the shared edge net (`external: true`), **no web `ports:`**.
2. `/root/infra/check-compose.sh` → OK, then `docker compose up -d`.
3. One new file: `<ingress>/sites/<app>.caddy` → `domain { reverse_proxy <container>:<port> }`.
4. `docker exec <caddy> caddy validate` → `caddy reload` (never restart).
5. `curl -sSI` the new domain + two neighbours.
6. Append the `/root/infra/ALLOCATIONS.md` line + update `/root/INFRA.md`. Done.
