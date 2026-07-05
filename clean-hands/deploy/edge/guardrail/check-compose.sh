#!/usr/bin/env bash
# check-compose.sh — structural ingress guardrail for a shared multi-project box.
#
# THE LAW it enforces (see runbooks/consolidation.md):
#   1. Exactly ONE stack — the tenant-neutral ingress (INGRESS_DIR) — may bind
#      host ports 80/443. Any other compose that maps 80 or 443 on ANY
#      interface (loopback included: the bind still collides at boot) FAILS.
#   2. Apps behind the edge publish NO host ports. A genuinely-needed non-HTTP
#      host port binds 127.0.0.1 only, or is whitelisted in the exceptions
#      file. Anything else (0.0.0.0, ::, bare "8080:80", ephemeral "- 3000")
#      FAILS.
#   3. One domain = one site block, globally. A duplicate hostname across the
#      ingress Caddy files invalidates the ENTIRE config and drops every site,
#      so duplicates FAIL here before they ever reach `caddy reload`.
#
# Run it before every `docker compose up` / edge reload; wire it into deploy
# scripts. Exit 0 = clean, 1 = violations (printed), 2 = misconfigured.
#
# Config (env vars override the conf file):
#   /root/infra/ingress.conf      sets INGRESS_DIR (required), SCAN_ROOT,
#                                 EXCEPTIONS
#   INGRESS_DIR                   the ONE directory allowed to bind 80/443
#   SCAN_ROOT                     where compose files are searched (default /root)
#   EXCEPTIONS                    file of allowed non-loopback publishes,
#                                 lines: "<compose-path-glob> <host-port>"
#                                 (80/443 can NEVER be excepted)
set -euo pipefail

_env_ingress="${INGRESS_DIR:-}"; _env_root="${SCAN_ROOT:-}"; _env_exc="${EXCEPTIONS:-}"
CONF="${CHECK_COMPOSE_CONF:-/root/infra/ingress.conf}"
# shellcheck disable=SC1090
[ -f "$CONF" ] && . "$CONF"
INGRESS_DIR="${_env_ingress:-${INGRESS_DIR:-}}"
SCAN_ROOT="${_env_root:-${SCAN_ROOT:-/root}}"
EXCEPTIONS="${_env_exc:-${EXCEPTIONS:-/root/infra/port-exceptions}}"

if [ -z "$INGRESS_DIR" ]; then
  echo "FATAL: INGRESS_DIR is not set (create $CONF or export INGRESS_DIR)." >&2
  echo "Refusing to guess which stack is the edge — the box is truth, go look." >&2
  exit 2
fi

fail=0
violation() { printf 'VIOLATION %s\n' "$1"; fail=1; }

# ---------------------------------------------------------------------------
# 1+2: host-port publishes in every compose outside INGRESS_DIR
# ---------------------------------------------------------------------------

# Emits one line per published port: "<lineno>|<host_ip>|<host_port>"
# host_port may be "ephemeral" (docker picks a random host port — still a
# publish) or a range like "8000-8010". Handles short syntax ("IP:HOST:CONT",
# "HOST:CONT", "CONT", inline flow lists) and long syntax (published:/host_ip:).
scan_compose_ports() {
  awk '
    function emit_short(s, nr,   n, p, hostip, hostport) {
      sub(/[ \t]*#.*$/, "", s); gsub(/["'"'"']/, "", s); gsub(/[ \t]/, "", s)
      if (s == "") return
      sub(/\/(tcp|udp)$/, "", s)
      hostip = ""
      if (s ~ /^\[/) {                       # IPv6 host ip, e.g. [::1]:80:80
        match(s, /^\[[^\]]*\]/)
        hostip = substr(s, 2, RLENGTH - 2)
        s = substr(s, RLENGTH + 2)           # drop "[...]:"
        n = split(s, p, ":")
        hostport = (n >= 2) ? p[1] : "ephemeral"
      } else {
        n = split(s, p, ":")
        if (n >= 3)      { hostip = p[1]; hostport = (p[2] == "") ? "ephemeral" : p[2] }
        else if (n == 2) { hostport = p[1] }
        else             { hostport = "ephemeral" }
      }
      printf "%d|%s|%s\n", nr, hostip, hostport
    }
    function flush_item() {
      if (item_active && item_published != "")
        printf "%d|%s|%s\n", item_line, item_hostip, item_published
      item_active = 0; item_published = ""; item_hostip = ""; item_line = 0
    }
    function handle_kv(s, nr,   v) {
      if (s ~ /^published:/) {
        v = s; sub(/^published:[ \t]*/, "", v); sub(/[ \t]*#.*$/, "", v); gsub(/["'"'"']/, "", v)
        item_published = v; if (item_line == 0) item_line = nr
      } else if (s ~ /^host_ip:/) {
        v = s; sub(/^host_ip:[ \t]*/, "", v); sub(/[ \t]*#.*$/, "", v); gsub(/["'"'"']/, "", v)
        item_hostip = v; if (item_line == 0) item_line = nr
      }
    }
    BEGIN { in_ports = 0; ports_indent = -1; item_active = 0; item_published = ""; item_hostip = ""; item_line = 0 }
    {
      raw = $0; sub(/\r$/, "", raw)
      t = raw; gsub(/^[ \t]+/, "", t)
      if (t == "" || t ~ /^#/) next
      ind = 0; while (substr(raw, ind + 1, 1) == " ") ind++
      if (!in_ports) {
        if (t ~ /^ports:[ \t]*$/) { in_ports = 1; ports_indent = ind; next }
        if (t ~ /^ports:[ \t]*\[/) {
          s = t; sub(/^ports:[ \t]*\[/, "", s); sub(/\][ \t]*(#.*)?$/, "", s)
          n = split(s, arr, ","); for (i = 1; i <= n; i++) emit_short(arr[i], NR)
        }
        next
      }
      if (ind <= ports_indent) {
        flush_item(); in_ports = 0
        if (t ~ /^ports:[ \t]*$/) { in_ports = 1; ports_indent = ind }
        next
      }
      if (t ~ /^-/) {
        flush_item()
        s = t; sub(/^-[ \t]*/, "", s)
        if (s ~ /^(published|target|host_ip|protocol|mode|name):/) { item_active = 1; handle_kv(s, NR) }
        else if (s == "") { item_active = 1; item_line = NR }
        else emit_short(s, NR)
      } else if (item_active && t ~ /^[a-z_]+:/) {
        handle_kv(t, NR)
      }
    }
    END { flush_item() }
  ' "$1"
}

is_loopback() {
  case "$1" in
    127.*|localhost|::1) return 0 ;;
    *) return 1 ;;
  esac
}

# does host-port spec (number or "lo-hi" range) cover 80 or 443?
covers_edge_port() {
  local hp="$1" lo hi
  case "$hp" in
    80|443) return 0 ;;
    [0-9]*-[0-9]*)
      lo="${hp%%-*}"; hi="${hp##*-}"
      case "$lo$hi" in *[!0-9]*) return 1 ;; esac
      { [ "$lo" -le 80 ] && [ "$hi" -ge 80 ]; } || { [ "$lo" -le 443 ] && [ "$hi" -ge 443 ]; }
      return $? ;;
    *) return 1 ;;
  esac
}

is_excepted() {
  local f="$1" p="$2" pat port _rest
  [ -f "$EXCEPTIONS" ] || return 1
  while read -r pat port _rest; do
    case "$pat" in ''|'#'*) continue ;; esac
    [ "$port" = "$p" ] || [ "$port" = '*' ] || continue
    # shellcheck disable=SC2254
    case "$f" in $pat) return 0 ;; esac
  done < "$EXCEPTIONS"
  return 1
}

compose_files() {
  find "$SCAN_ROOT" \
    \( -path "$INGRESS_DIR" -o -path "$INGRESS_DIR/*" -o -name node_modules -o -name .git \) -prune -o \
    -type f \( -name 'docker-compose*.yml' -o -name 'docker-compose*.yaml' \
               -o -name 'compose.yml' -o -name 'compose.yaml' \
               -o -name 'compose.*.yml' -o -name 'compose.*.yaml' \) -print 2>/dev/null | sort
}

while IFS= read -r f; do
  [ -n "$f" ] || continue
  while IFS='|' read -r lineno hostip hostport; do
    [ -n "$hostport" ] || continue
    if covers_edge_port "$hostport"; then
      violation "$f:$lineno — maps host port $hostport (web edge port). Only $INGRESS_DIR may bind 80/443. Route via a sites/<app>.caddy file instead. NOT exceptable."
    elif [ "$hostport" = "ephemeral" ]; then
      is_excepted "$f" "$hostport" || \
        violation "$f:$lineno — publishes a container port to a RANDOM host port on all interfaces. Drop the publish (the edge reaches containers by name) or bind 127.0.0.1 explicitly."
    elif ! is_loopback "${hostip:-0.0.0.0}"; then
      is_excepted "$f" "$hostport" || \
        violation "$f:$lineno — publishes host port $hostport on ${hostip:-all interfaces}. Web traffic goes through $INGRESS_DIR; a debug/non-HTTP port must bind 127.0.0.1:$hostport. (If genuinely needed on the wire, add \"$f $hostport\" to $EXCEPTIONS.)"
    fi
  done < <(scan_compose_ports "$f")
done < <(compose_files)

# ---------------------------------------------------------------------------
# 3: duplicate hostnames across the ingress Caddy config
# ---------------------------------------------------------------------------
if [ -d "$INGRESS_DIR" ]; then
  dups="$(
    find "$INGRESS_DIR" -maxdepth 3 -type f \( -name '*.caddy' -o -name 'Caddyfile' \) 2>/dev/null | sort | while IFS= read -r cf; do
      # site-address lines start at column 0 and end with "{"
      awk -v f="$cf" '
        /^[^ \t}#]/ && /\{[ \t]*$/ {
          line = $0; sub(/\{[ \t]*$/, "", line)
          n = split(line, a, /[, \t]+/)
          for (i = 1; i <= n; i++) {
            h = a[i]
            sub(/^https?:\/\//, "", h); sub(/:[0-9]+$/, "", h)
            if (h ~ /^[a-zA-Z0-9*][a-zA-Z0-9*.-]*\.[a-zA-Z]/) print h "\t" f ":" FNR
          }
        }' "$cf"
    done | sort | awk -F'\t' '
      { count[$1]++; where[$1] = where[$1] "  " $2 }
      END { for (h in count) if (count[h] > 1) print h where[h] }'
  )"
  if [ -n "$dups" ]; then
    while IFS= read -r d; do
      violation "duplicate hostname — $d. A duplicate site address invalidates the ENTIRE Caddy config and drops EVERY domain on the box."
    done <<< "$dups"
  fi
else
  echo "WARN: INGRESS_DIR=$INGRESS_DIR does not exist on this machine — skipped the duplicate-hostname check." >&2
fi

if [ "$fail" = 0 ]; then
  echo "OK — no edge violations (scanned $SCAN_ROOT, ingress: $INGRESS_DIR)."
else
  echo "FAILED — fix the violations above before bringing anything up." >&2
  exit 1
fi
