#!/usr/bin/env bash
# edge-reattach — the self-heal that guarantees cleanhands can never stay down
# because of a neighbor's deploy.
#
# The recurring outage: the box's ONE edge (whatever container owns :80/:443)
# gets recreated when a neighbor (fine-traders/co-muse) runs `docker compose up`.
# It comes back WITHOUT cleanhands-prod-net, so it can't resolve cleanhands-api
# -> every cleanhands domain 502s while the app itself is perfectly healthy.
#
# This watchdog makes that self-correct within one poll:
#   discover whoever currently owns :443  ->  ensure it is attached to
#   cleanhands' OWN network  ->  if not, attach it.
#
# It is deliberately the SMALLEST possible safe action:
#   • ADDITIVE ONLY — it just ensures cleanhands' network is present on the edge.
#   • It NEVER restarts or reloads the edge, NEVER edits any config, NEVER
#     touches another project's network. It therefore cannot affect fine-traders,
#     co-muse, or anyone else. (Caddy re-resolves upstreams per request, so the
#     attach alone restores routing with no reload.)
# Pair it with edge-guard.sh, which alerts if anything ever gets past this.
set -uo pipefail

NET="${CLEANHANDS_EDGE_NET:-cleanhands-prod-net}"
log(){ printf '%s edge-reattach: %s\n' "$(date -u +%FT%TZ)" "$*"; }

alert(){
  [ -n "${TG_BOT_TOKEN:-}" ] && [ -n "${TG_CHAT_ID:-}" ] && \
    curl -s -m10 -o /dev/null \
      --data-urlencode "chat_id=${TG_CHAT_ID}" \
      --data-urlencode "text=$1" \
      "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" || true
  [ -n "${ALERT_WEBHOOK:-}" ] && \
    curl -s -m10 -o /dev/null -H 'Content-Type: application/json' \
      --data "$(python3 -c 'import json,sys;print(json.dumps({"text":sys.argv[1]}))' "$1" 2>/dev/null || printf '{"text":"edge-reattach"}')" \
      "$ALERT_WEBHOOK" || true
  return 0
}

command -v docker >/dev/null 2>&1 || { log "docker not found — nothing to do"; exit 0; }

if ! docker network inspect "$NET" >/dev/null 2>&1; then
  log "network $NET not found — is cleanhands deployed? nothing to do"; exit 0
fi

# Discover the live edge(s) — do NOT hardcode a name; the edge may be renamed or
# relocated. Whoever publishes :443 is the edge.
mapfile -t EDGES < <(docker ps --filter publish=443 --format '{{.Names}}' | sort -u)
if [ "${#EDGES[@]}" -eq 0 ]; then
  log "no container publishes :443 — no edge running, nothing to do"; exit 0
fi

changed=0
for edge in "${EDGES[@]}"; do
  if docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$edge" 2>/dev/null | grep -qw "$NET"; then
    continue   # already attached — the healthy steady state
  fi
  if docker network connect "$NET" "$edge" 2>/dev/null; then
    changed=1
    log "REATTACHED $edge -> $NET (edge had lost cleanhands' network; cleanhands would be 502)"
    alert "🔧 edge-reattach: reconnected ${edge} to ${NET} on srv1505584 — cleanhands was about to 502 (auto-healed)."
  else
    log "FAILED to attach $edge -> $NET (manual check needed)"
    alert "⚠️ edge-reattach: could NOT attach ${edge} to ${NET} — cleanhands may be down, manual check needed."
  fi
done

[ "$changed" = 0 ] && log "ok — edge already on $NET"
exit 0
