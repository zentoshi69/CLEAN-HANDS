#!/usr/bin/env bash
#
# install-systemd.sh — make all three bots run 24/7 and survive reboots.
#
# Run this AFTER quickstart.sh has created the venv and .env in this folder.
# It generates systemd units pointed at THIS directory and THIS user, installs
# them, and starts the bots. Re-run after code changes to restart cleanly.
#
# Usage:
#   sudo bash install-systemd.sh
#
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
# The user who should own the services (the invoking user, even under sudo).
RUN_USER="${SUDO_USER:-$(whoami)}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo:  sudo bash install-systemd.sh" >&2
  exit 1
fi
if [ ! -x "$DIR/venv/bin/python" ]; then
  echo "No venv found. Run 'bash quickstart.sh --setup' first." >&2
  exit 1
fi
if [ ! -f "$DIR/.env" ]; then
  echo "No .env found. Run 'bash quickstart.sh --setup' first." >&2
  exit 1
fi

# Map each service to the python interpreter its unit expects. A service is only
# enabled if that interpreter exists, so the Mini App / staking API (which use
# their own venvs) are plugged in automatically once you've set them up.
declare -A VENV=(
  [guardian]="$DIR/venv/bin/python"
  [scanner]="$DIR/venv/bin/python"
  [community]="$DIR/venv/bin/python"
  [alerts]="$DIR/venv/bin/python"
  [miniapp]="$DIR/miniapp/venv/bin/python"
  [staking]="$DIR/staking-api/venv/bin/python"
  [notifier]="$DIR/staking-api/venv/bin/python"
)

enabled=()
for svc in guardian scanner community alerts miniapp staking notifier; do
  src="systemd/degen-$svc.service"
  [ -f "$src" ] || continue
  dst="/etc/systemd/system/degen-$svc.service"
  echo "==> Installing $dst"
  sed -e "s#/home/youruser/bots#$DIR#g" \
      -e "s#User=youruser#User=$RUN_USER#g" \
      "$src" > "$dst"
  if [ -x "${VENV[$svc]}" ]; then
    enabled+=("degen-$svc")
  else
    echo "    (degen-$svc installed but NOT started — no venv at ${VENV[$svc]};"
    echo "     set it up: cd $(dirname "${VENV[$svc]%/bin/python}") && python3 -m venv venv && venv/bin/pip install -r requirements.txt)"
  fi
done

echo "==> Reloading systemd and starting services…"
systemctl daemon-reload
if [ "${#enabled[@]}" -gt 0 ]; then
  systemctl enable --now "${enabled[@]}"
  echo
  echo "==> Status:"
  systemctl --no-pager --lines=0 status "${enabled[@]}" || true
  echo
  echo "Live logs:   journalctl -f $(printf -- '-u %s ' "${enabled[@]}")"
fi
echo "Restart one: sudo systemctl restart degen-community"
echo "Done. Everything set up now runs 24/7 and restarts on reboot."
