# CLEAN-HANDS — agent ground rules

## ⚠️ Before ANY deploy / VPS / proxy / port / systemd / docker work

Read `.claude/skills/vps-deploy-safety/SKILL.md` FIRST and follow its
pre-flight. Non-negotiable. The production VPS is a SHARED multi-project box;
a 2026-07-03 outage was caused by agents changing one product's infra and
breaking the others.

Hard rules (details + rationale in the skill):

- **The box is truth, docs are history.** Never act on README/deploy scripts
  without verifying live state (`ss -ltnp`, `docker ps`, `/root/INFRA.md`).
- **One edge per box.** Never install/start a second proxy; never bind 80/443.
- **Shared config = shared blast radius.** Backup → edit only your block →
  validate → reload (never restart) → curl-sweep EVERY domain, not just yours.
- **Retire corpses in code.** `deploy.sh`, `redeploy.sh`, `install-systemd.sh`
  are legacy and guarded with `FORCE_LEGACY=1` — do not bypass on production.
- **Update `/root/INFRA.md` on the box in the same session as any infra change.**

## Current production topology (2026-07-03)

- cleanhands runs DOCKERIZED: container `cleanhands-api`
  (image `cleanhands-api-prod:*`, net `cleanhands-prod-net`, data
  `/opt/cleanhands-api/data`, host debug port `127.0.0.1:18090`) +
  `cleanhands-postgres-prod` + `cleanhands-redis-prod` +
  `cleanhands-site-prod-*` (static site).
- The edge is the box's single shared proxy (see `/root/INFRA.md` on the VPS
  for the live owner of 80/443) reaching containers **by name**, not IP.
- RETIRED — never start: systemd `degen-staking` (SQLite stale since
  2026-06-24), host-installed Caddy, nginx-as-edge for cleanhands.
