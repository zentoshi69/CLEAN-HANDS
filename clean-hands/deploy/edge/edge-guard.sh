#!/usr/bin/env bash
# edge-guard — watch EVERY domain on the shared box and alert the instant one
# drops. It is strictly READ-ONLY: it never touches the edge or any container.
# Its whole job is to turn "cleanhands was down for hours and nobody noticed"
# into "a phone alert within 60 seconds", and to tell you WHICH failure it is.
#
# Why this exists: the box runs several products behind ONE shared Caddy edge.
# When one product's compose is recreated, a cross-product route can drop while
# the app container stays perfectly healthy — so nothing looks wrong except the
# public URL. A single poll catches that.
#
# Domains file — one entry per line:  <domain> <expected_codes> [upstream_container]
#   expected_codes    comma list, e.g.  200   or   200,301,302
#   upstream_container optional; when the domain is DOWN its docker state + nets
#                      are printed so you can tell "edge lost the route" (app is
#                      running) from "app crashed" (container down) instantly.
#   blank lines and #comments are ignored.
#
# Alerts (optional, fired only on an up<->down TRANSITION so it never spams):
#   Telegram:  export TG_BOT_TOKEN=...  TG_CHAT_ID=...
#   Webhook:   export ALERT_WEBHOOK=https://...   (receives JSON {"text": "..."})
#
# Usage:  edge-guard.sh /etc/edge-guard/domains
# Exit:   0 = all domains healthy, 1 = at least one domain down.
set -uo pipefail

STATE_DIR="${EDGE_GUARD_STATE:-/var/lib/edge-guard}"
TIMEOUT="${EDGE_GUARD_TIMEOUT:-10}"

notify() {
  local msg="$1"
  printf '%s\n' "$msg"
  if [ -n "${TG_BOT_TOKEN:-}" ] && [ -n "${TG_CHAT_ID:-}" ]; then
    curl -s -m 10 -o /dev/null \
      --data-urlencode "chat_id=${TG_CHAT_ID}" \
      --data-urlencode "text=${msg}" \
      "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" || true
  fi
  if [ -n "${ALERT_WEBHOOK:-}" ]; then
    local payload
    payload=$(python3 -c 'import json,sys; print(json.dumps({"text": sys.argv[1]}))' "$msg" 2>/dev/null \
              || printf '{"text":"edge-guard alert (payload encode failed)"}')
    curl -s -m 10 -o /dev/null -H 'Content-Type: application/json' \
      --data "$payload" "$ALERT_WEBHOOK" || true
  fi
}

main() {
  local file="${1:-}"
  if [ -z "$file" ] || [ ! -r "$file" ]; then
    echo "usage: edge-guard.sh <domains-file>   (unreadable: '${file}')" >&2
    exit 2
  fi
  mkdir -p "$STATE_DIR"
  local any_down=0

  while read -r domain codes container _rest; do
    case "$domain" in ''|\#*) continue;; esac
    codes="${codes:-200}"

    local got ok=0
    # curl already emits %{http_code} (000 on any connect/TLS failure) even when
    # it exits non-zero — so don't add a second `|| echo 000` (that doubled it to
    # "000000"). Just default an empty result (curl absent) to 000.
    got=$(curl -s -m "$TIMEOUT" -o /dev/null -w '%{http_code}' "https://${domain}/" 2>/dev/null); got=${got:-000}
    local IFS_SAVE="$IFS"; IFS=','
    local want c
    for c in $codes; do [ "$got" = "$c" ] && ok=1; done
    IFS="$IFS_SAVE"

    local statefile="${STATE_DIR}/${domain}.state"
    local prev; prev=$(cat "$statefile" 2>/dev/null || echo unknown)

    if [ "$ok" = 1 ]; then
      [ "$prev" = down ] && notify "✅ RECOVERED ${domain} (HTTP ${got})"
      echo up > "$statefile"
    else
      any_down=1
      local detail=""
      if [ -n "${container:-}" ]; then
        local st nets
        st=$(docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null); st=${st:-missing}
        nets=$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$container" 2>/dev/null)
        detail=$(printf '\n   upstream %s: state=%s nets=[%s]' "$container" "$st" "$nets")
        if [ "$st" = running ]; then
          detail="${detail}"$'\n'"   → app is UP but the edge can't reach it: the EDGE lost the route (reattach net + reload edge), this is NOT an app crash."
        fi
      fi
      [ "$prev" != down ] && notify "$(printf '🚨 DOWN %s (HTTP %s, expected %s)%s' "$domain" "$got" "$codes" "$detail")"
      echo down > "$statefile"
    fi
  done < "$file"

  return $any_down
}

main "$@"
