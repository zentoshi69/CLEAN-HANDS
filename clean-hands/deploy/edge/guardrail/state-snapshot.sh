#!/usr/bin/env bash
# state-snapshot.sh — regenerate the box inventory (the "M0 table") into
# /root/infra/state.md so future sessions read a committed snapshot + ONE live
# confirm instead of running a full manual sweep from zero.
#
# STRICTLY READ-ONLY apart from writing the output file. Safe to run any time;
# wired to a daily timer by install.sh — also run it after EVERY deploy.
#
# ⚠️ The BOX outranks this file. A snapshot is history the moment it is
# written; re-run (or at minimum `ss -tulpn | grep -E ':80 |:443 '`) before
# acting on it.
set -uo pipefail

_env_ingress="${INGRESS_DIR:-}"
CONF="${CHECK_COMPOSE_CONF:-/root/infra/ingress.conf}"
# shellcheck disable=SC1090
[ -f "$CONF" ] && . "$CONF"
INGRESS_DIR="${_env_ingress:-${INGRESS_DIR:-}}"
OUT="${STATE_OUT:-/root/infra/state.md}"

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

section() { printf '\n## %s\n\n```\n' "$1" >> "$TMP"; }
endsec()  { printf '```\n' >> "$TMP"; }
run()     { "$@" >> "$TMP" 2>&1 || echo "(command failed or unavailable: $*)" >> "$TMP"; }

{
  echo "# Box state snapshot"
  echo
  echo "- generated: $(date -u '+%Y-%m-%d %H:%M:%S UTC') by state-snapshot.sh"
  echo "- host: $(hostname)"
  echo "- ⚠️ box-is-truth: this file is history. Confirm live before acting."
} > "$TMP"

section "Who owns 80/443 (the only fact that matters most)"
run sh -c "command -v ss >/dev/null || { echo 'ss UNAVAILABLE — cannot answer; are you on the box?'; exit 1; }; ss -tulpn | grep -E ':80 |:443 ' || echo 'nothing bound to 80/443'"
endsec

section "All host sockets (ss -tulpn)"
run sh -c "ss -tulpn | sort -k5"
endsec

section "Containers incl. stopped (stopped containers still own names+ports)"
run docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
endsec

section "Compose projects"
run docker compose ls -a
endsec

section "Docker networks / volumes"
run docker network ls
run docker volume ls
endsec

section "Bare-metal edge suspects (nginx/caddy/apache on the host)"
run sh -c "systemctl list-units --type=service --all --no-pager | grep -iE 'nginx|caddy|apache|httpd' || echo 'none'"
run sh -c "nginx -T 2>/dev/null | grep -nE 'listen|server_name' || echo 'nginx: not an edge (no config or not installed)'"
endsec

if [ -n "$INGRESS_DIR" ] && [ -d "$INGRESS_DIR" ]; then
  section "Ingress hostnames ($INGRESS_DIR — one file per app)"
  run sh -c "grep -RnE '^[a-zA-Z0-9*][a-zA-Z0-9*.-]*\.[a-zA-Z]' --include='*.caddy' --include='Caddyfile' '$INGRESS_DIR' || echo 'no hostnames found'"
  endsec
fi

section "Guardrail (check-compose.sh)"
if [ -x /root/infra/check-compose.sh ]; then
  run /root/infra/check-compose.sh
else
  echo "check-compose.sh not installed" >> "$TMP"
fi
endsec

{
  echo
  echo "## Registries"
  echo
  echo "- \`/root/INFRA.md\` — box registry (owner of 80/443, domain table, RETIRED stacks)"
  echo "- \`/root/infra/ALLOCATIONS.md\` — per-app allocation ledger"
} >> "$TMP"

mkdir -p "$(dirname "$OUT")"
mv "$TMP" "$OUT"
trap - EXIT
echo "wrote $OUT"
