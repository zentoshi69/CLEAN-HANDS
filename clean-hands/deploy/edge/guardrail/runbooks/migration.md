# Migration runbook — replacing a stack without leaving a corpse

For migrations that swap one serving stack for another on a shared box:
systemd→docker, SQLite→postgres, host-proxy→containerized edge, old
container→new container. The 2026-07-03 outage chain started with a
migration that left the loser half-alive; every step here removes one link
of that chain. Operate under `.claude/skills/vps-deploy-safety/SKILL.md` and
the Law in `consolidation.md`.

## Before switching anything

1. **Inventory first** (consolidation.md M0). You are migrating on a map;
   make sure the map is the territory. If `/root/INFRA.md` disagrees with
   the box, fixing the registry IS the first task.
2. **Freshness-check BOTH datastores.** Before moving traffic, prove which
   store traffic actually hits today: file mtimes, request logs, db
   activity. Serving a stale store after cutover is silent data corruption —
   in a staking product, silent money corruption.
   ```bash
   ls -l --time-style=full-iso /path/to/old.sqlite
   docker exec <new-db> psql -U <u> -c "select max(<updated_col>) from <hot_table>;"
   ```
3. **Name the blast radius** in one sentence: which ports, which shared
   files, which networks, which domains. If the sentence contains another
   product's name, stop and re-plan additively.
4. **Prepare the rollback as a single pasteable command** and print it. No
   `!` characters (interactive bash history-expands them inside double
   quotes), one purpose per block.

## The switch

5. Bring the NEW stack up beside the old — own network, own
   `<product>-<role>` container names, own volumes, **no host ports** (edge
   reaches it by name on the shared edge network; debug ports
   `127.0.0.1:` only, scanned free first). Run
   `/root/infra/check-compose.sh` before `up`.
6. Verify the new stack internally before any traffic moves:
   `docker exec <edge> wget -qSO- http://<new-container>:<port>/health`.
7. Move traffic by **editing only your own site file**
   (`sites/<app>.caddy` upstream), then validate → **reload, never
   restart** → curl-sweep EVERY domain on the box, not just yours.
8. Watch the NEW datastore take writes (logs, row counts) before declaring
   success. "Container healthy" ≠ "container serving" — three healthy
   clones once ran for days while only one was wired to the edge.

## Retire the loser — same day, in code

A corpse that can restart WILL restart (next boot, next agent, next deploy
script). Retirement is only real when all four are done:

9.  `systemctl stop <old>` / `docker stop <old>` — and for docker,
    `docker update --restart=no <old>` before any `rm`.
10. `systemctl disable <old>` (check `systemctl is-enabled` after).
11. **Patch every script that can resurrect it** — deploy.sh, redeploy.sh,
    install-*.sh, cron entries. Guard them (`FORCE_LEGACY=1` pattern used in
    clean-hands/deploy) or delete the bring-up lines. Repo docs that
    describe the retired architecture get a RETIRED banner.
12. Update the registries in the same session: `/root/INFRA.md` (move the
    old stack under RETIRED — "never start" list) and strike-through its
    `/root/infra/ALLOCATIONS.md` line, appending the replacement line.

## After

13. Full-domain sweep against the expected codes in `/root/INFRA.md`; any
    neighbour regression = rollback now, investigate later.
14. `/root/infra/state-snapshot.sh` — commit the new reality to
    `state.md`.
15. Leave the old data volume/file in place, read-only if possible, for at
    least one verification cycle. Deleting the loser's data is a separate,
    explicitly-approved task — never part of the migration itself.

## Anti-patterns that caused real outages

- Mutating a shared `.env` (`STAKE_HOST=172.19.0.1`) so the "old" stack
  half-points at the new one — now neither stack is understandable.
- Leaving an enabled systemd unit while its docker replacement serves —
  the unit resurrects at boot and steals the port or the writes.
- "The backup is last-good": grep the backup for YOUR product's config
  before restoring it; a wrong backup restore once took the whole box down.
- Retiring by memory ("I think that's off now") instead of by
  `ss -tulpn` + `docker ps -a` + `systemctl is-enabled`.
