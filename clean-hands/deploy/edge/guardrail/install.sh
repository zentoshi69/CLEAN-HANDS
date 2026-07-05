#!/usr/bin/env bash
# Installer for the permanent ingress guardrail. Run AS ROOT ON THE BOX, only
# AFTER the consolidation cutover (runbooks/consolidation.md M0–M4) is done —
# this is M5, the part that makes the fix permanent.
#
# SAFE + IDEMPOTENT + ADDITIVE: touches nothing under any app, never restarts
# or reloads anything, never overwrites an existing ALLOCATIONS.md or an
# existing infra-guardian reference file.
#
#   sudo INGRESS_DIR=/root/edge bash install.sh
#
# INGRESS_DIR must name the ONE directory whose compose is allowed to bind
# 80/443. If unset, the installer tries to detect it (a candidate dir whose
# compose maps 80/443) and ABORTS unless exactly one candidate matches —
# it never guesses.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "== 0/6 resolve INGRESS_DIR (box is truth) =="
if [ -z "${INGRESS_DIR:-}" ]; then
  candidates=()
  for d in /root/ingress /root/edge; do
    if [ -d "$d" ] && grep -qsE '^\s*-\s*"?(80|443):' "$d"/docker-compose*.y*ml "$d"/compose*.y*ml 2>/dev/null; then
      candidates+=("$d")
    fi
  done
  if [ "${#candidates[@]}" -eq 1 ]; then
    INGRESS_DIR="${candidates[0]}"
    echo "detected INGRESS_DIR=$INGRESS_DIR"
  else
    echo "FATAL: could not unambiguously detect the ingress directory (found: ${candidates[*]:-none})." >&2
    echo "Run again with INGRESS_DIR=<dir> set to the ONE edge stack that owns 80/443." >&2
    exit 2
  fi
fi
[ -d "$INGRESS_DIR" ] || { echo "FATAL: INGRESS_DIR=$INGRESS_DIR does not exist." >&2; exit 2; }

echo "== 1/6 /root/infra: scripts + config =="
mkdir -p /root/infra
install -m755 "$HERE/check-compose.sh"  /root/infra/check-compose.sh
install -m755 "$HERE/state-snapshot.sh" /root/infra/state-snapshot.sh
if [ ! -f /root/infra/ingress.conf ]; then
  cat > /root/infra/ingress.conf <<EOF
# written by guardrail/install.sh $(date -u '+%Y-%m-%d')
INGRESS_DIR=$INGRESS_DIR
SCAN_ROOT=/root
EXCEPTIONS=/root/infra/port-exceptions
EOF
  echo "wrote /root/infra/ingress.conf"
else
  echo "/root/infra/ingress.conf already present — left untouched"
fi
if [ ! -f /root/infra/port-exceptions ]; then
  printf '# allowed non-loopback host-port publishes, one per line:\n# <compose-path-glob> <host-port>\n# 80/443 can NEVER be excepted.\n' > /root/infra/port-exceptions
fi

echo "== 2/6 registry =="
if [ ! -f /root/infra/ALLOCATIONS.md ]; then
  install -m644 "$HERE/ALLOCATIONS.template.md" /root/infra/ALLOCATIONS.md
  echo "seeded /root/infra/ALLOCATIONS.md — BACKFILL one line per live app now (see runbooks/consolidation.md M5.3)"
else
  echo "/root/infra/ALLOCATIONS.md already present — left untouched"
fi

echo "== 3/6 runbooks (fills the infra-guardian references gap) =="
mkdir -p /root/infra/runbooks
for rb in consolidation.md migration.md failures.md; do
  install -m644 "$HERE/runbooks/$rb" "/root/infra/runbooks/$rb"
done
for skilldir in /root/.claude/skills/infra-guardian "$HOME/.claude/skills/infra-guardian"; do
  if [ -d "$skilldir" ]; then
    mkdir -p "$skilldir/references"
    for rb in consolidation.md migration.md failures.md; do
      if [ ! -e "$skilldir/references/$rb" ]; then
        cp "$HERE/runbooks/$rb" "$skilldir/references/$rb"
        echo "installed $skilldir/references/$rb"
      else
        echo "$skilldir/references/$rb already exists — left untouched"
      fi
    done
  fi
done

echo "== 4/6 daily state snapshot =="
install -m644 "$HERE/state-snapshot.service" /etc/systemd/system/state-snapshot.service
install -m644 "$HERE/state-snapshot.timer"   /etc/systemd/system/state-snapshot.timer
systemctl daemon-reload
systemctl enable --now state-snapshot.timer
/root/infra/state-snapshot.sh || true

echo "== 5/6 register in /root/INFRA.md =="
if [ -f /root/INFRA.md ] && ! grep -q 'INGRESS GUARDRAIL' /root/INFRA.md; then
  cat >> /root/INFRA.md <<EOF

## INGRESS GUARDRAIL — DO NOT DISABLE
- The ONE edge (owner of 80/443): $INGRESS_DIR. No other compose may ever
  bind 80/443 or publish a non-loopback host port.
- /root/infra/check-compose.sh — run before every compose up / edge reload;
  exits non-zero on any violation. Config: /root/infra/ingress.conf.
- /root/infra/state-snapshot.sh (+ state-snapshot.timer, daily): regenerates
  /root/infra/state.md inventory. Read it first, confirm live second.
- /root/infra/ALLOCATIONS.md — append-only per-app ledger.
- Runbooks: /root/infra/runbooks/{consolidation,migration,failures}.md
- Source of truth: clean-hands repo, clean-hands/deploy/edge/guardrail/.
EOF
  echo "INFRA.md updated"
else
  echo "INFRA.md note already present (or /root/INFRA.md missing) — skipped"
fi

echo "== 6/6 run the guardrail NOW =="
/root/infra/check-compose.sh

echo
echo "done — the guardrail is standing. Add app N+1 per runbooks/consolidation.md §'permanent model'."
