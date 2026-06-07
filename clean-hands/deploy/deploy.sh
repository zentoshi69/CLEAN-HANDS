#!/usr/bin/env bash
#
# deploy.sh — one-shot server bring-up for the CLEAN stack (run on the VPS).
# Does Phases 3-4 of GO-LIVE.md: venvs + deps, systemd services, and the Caddy
# vhost (auto-HTTPS). DNS (Phase 1), .env secrets (Phase 2) and @BotFather
# (Phase 5) are still yours to do — this won't touch secrets.
#
# Usage (from the repo's bots/ dir, as a sudoer):
#   sudo DOMAIN=app.cleanhands.fun bash deploy/deploy.sh
#
set -euo pipefail
cd "$(dirname "$0")/.."                 # -> bots/
BOTS_DIR="$(pwd)"
DOMAIN="${DOMAIN:-app.cleanhands.fun}"
RUN_USER="${SUDO_USER:-$(whoami)}"

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
[ -f "$BOTS_DIR/.env" ] || { echo "Create $BOTS_DIR/.env first (cp .env.example .env; fill secrets)." >&2; exit 1; }

echo "==> [1/4] Virtualenvs + dependencies"
sudo -u "$RUN_USER" bash -lc "
  cd '$BOTS_DIR'
  python3 -m venv venv && venv/bin/pip -q install --upgrade pip && venv/bin/pip -q install -r requirements.txt
  python3 -m venv staking-api/venv && staking-api/venv/bin/pip -q install --upgrade pip && staking-api/venv/bin/pip -q install -r staking-api/requirements.txt
"

echo "==> [2/4] systemd services"
bash "$BOTS_DIR/install-systemd.sh"

echo "==> [3/4] Caddy reverse proxy for $DOMAIN"
if ! command -v caddy >/dev/null 2>&1; then
  echo "    installing caddy…"
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl >/dev/null
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  apt-get update -y >/dev/null && apt-get install -y caddy >/dev/null
fi
sed "s/app\\.cleanhands\\.fun/$DOMAIN/g" "$BOTS_DIR/deploy/Caddyfile" > /etc/caddy/Caddyfile
systemctl restart caddy

echo "==> [4/4] Health check"
sleep 3
bash "$BOTS_DIR/deploy/healthcheck.sh" "$DOMAIN" || true

echo
echo "Done. Remaining (yours): DNS A record for $DOMAIN, @BotFather (deploy/BOTFATHER.md),"
echo "and embedding site-sdk/clean-staking.js on the website."
