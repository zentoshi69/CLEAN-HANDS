#!/usr/bin/env bash
#
# bootstrap.sh — ONE-SHOT go-live for the CLEAN stack on a fresh Ubuntu VPS.
#
#   sudo DOMAIN=app.cleanhands.fun bash deploy/bootstrap.sh
#
# What it does, in order (idempotent — safe to re-run after editing .env):
#   1. installs OS deps (git, python venv, curl, caddy)
#   2. creates the sandboxed service user `clean`
#   3. clones/updates the repo into /home/clean/CLEAN-HANDS
#   4. writes .env: auto-generates every secret it can, prompts ONLY for the
#      values that exist nowhere but your @BotFather chat (Enter = skip; a
#      skipped token just leaves that one bot off — the app still ships)
#   5. validates each token against the Telegram API and auto-fills
#      MINIAPP_BOT_USERNAME from the community token
#   6. venvs + deps, systemd units (only services whose config is complete),
#      reconcile timer, Caddy vhost with auto-HTTPS
#   7. verifies: local healthz -> public healthz -> new UI actually served
#
set -euo pipefail

DOMAIN="${DOMAIN:-app.cleanhands.fun}"
RUN_USER="${RUN_USER:-clean}"
CLONE_DIR="/home/$RUN_USER/CLEAN-HANDS"
STACK="$CLONE_DIR/clean-hands"
BRANCH="${BRANCH:-main}"

c_grn(){ printf '\033[32m%s\033[0m\n' "$*"; }
c_red(){ printf '\033[31m%s\033[0m\n' "$*"; }
c_yel(){ printf '\033[33m%s\033[0m\n' "$*"; }
die(){ c_red "✗ $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root:  sudo DOMAIN=$DOMAIN bash $0"

# If we're being run from inside a checkout, inherit its origin URL — this
# carries a PAT along automatically when the repo is private.
REPO_URL="${REPO_URL:-}"
if [ -z "$REPO_URL" ] && git -C "$(dirname "$0")" rev-parse --git-dir >/dev/null 2>&1; then
  REPO_URL="$(git -C "$(dirname "$0")" remote get-url origin 2>/dev/null || true)"
fi
REPO_URL="${REPO_URL:-https://github.com/zentoshi69/CLEAN-HANDS.git}"

echo "==> [1/7] OS packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3-venv python3-pip curl ca-certificates fonts-dejavu-core >/dev/null

echo "==> [2/7] service user '$RUN_USER'"
id "$RUN_USER" &>/dev/null || adduser --disabled-password --gecos "" "$RUN_USER"

echo "==> [3/7] code -> $CLONE_DIR (branch: $BRANCH)"
if [ -d "$CLONE_DIR/.git" ]; then
  sudo -u "$RUN_USER" git -C "$CLONE_DIR" fetch origin "$BRANCH"
  sudo -u "$RUN_USER" git -C "$CLONE_DIR" checkout -q "$BRANCH"
  sudo -u "$RUN_USER" git -C "$CLONE_DIR" reset -q --hard "origin/$BRANCH"
else
  sudo -u "$RUN_USER" git clone -q --branch "$BRANCH" "$REPO_URL" "$CLONE_DIR"
fi
[ -f "$STACK/.env.example" ] || die "unexpected repo layout — $STACK/.env.example missing"

# ---- .env -------------------------------------------------------------- #
ENVF="$STACK/.env"
set_kv() {  # set_kv KEY VALUE — replace the line or append; never logs values
  if grep -qE "^$1=" "$ENVF"; then
    sed -i "s|^$1=.*|$1=$2|" "$ENVF"
  else
    echo "$1=$2" >> "$ENVF"
  fi
}
get_kv() {  # get_kv KEY — value with any inline comment stripped; missing key = ''
  { grep -E "^$1=" "$ENVF" 2>/dev/null | head -1 | cut -d= -f2- \
      | sed -e 's/[[:space:]]\+#.*$//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'; } || true
}
ask() {  # ask PROMPT -> $REPLY ('' when non-interactive or skipped)
  REPLY=""
  if [ -e /dev/tty ]; then
    printf '%s' "$1" > /dev/tty
    IFS= read -r REPLY < /dev/tty || REPLY=""
  fi
}
tg_username() {  # tg_username TOKEN -> bot username or '' (never echoes token)
  curl -s -m 10 "https://api.telegram.org/bot$1/getMe" 2>/dev/null \
    | grep -o '"username":"[^"]*"' | head -1 | cut -d'"' -f4
}

echo "==> [4/7] secrets & config ($ENVF)"
[ -f "$ENVF" ] || cp "$STACK/.env.example" "$ENVF"
[ -n "$(get_kv STAKE_SERVER_SECRET)" ] || set_kv STAKE_SERVER_SECRET "$(openssl rand -hex 32)"
[ -n "$(get_kv STAKE_ADMIN_TOKEN)" ]  || set_kv STAKE_ADMIN_TOKEN "$(openssl rand -hex 32)"
[ -n "$(get_kv DEFAULT_TOKEN_MINT)" ] || set_kv DEFAULT_TOKEN_MINT "6jb4XWggYJjoo3fx7irPVxhNiuFbHUyVyKR8mBL8pump"
[ -n "$(get_kv DEFAULT_TOKEN_DECIMALS)" ] || set_kv DEFAULT_TOKEN_DECIMALS 6
[ -n "$(get_kv STAKE_CORS_ORIGINS)" ] || set_kv STAKE_CORS_ORIGINS "https://cleanhands.fun,https://www.cleanhands.fun"
[ -n "$(get_kv MINIAPP_URL)" ]        || set_kv MINIAPP_URL "https://$DOMAIN/"
[ -n "$(get_kv MINIAPP_SHORT_NAME)" ] || set_kv MINIAPP_SHORT_NAME "app"
set_kv STAKE_ENV prod

if [ -z "$(get_kv SOLANA_RPC_URL)" ]; then
  ask "Solana RPC URL (Helius/Triton; Enter = public mainnet RPC for now): "
  set_kv SOLANA_RPC_URL "${REPLY:-https://api.mainnet-beta.solana.com}"
fi
# Season campaign defaults: wash 5% of the supply in 60 days (pump.fun = 1B)
[ -n "$(get_kv STAKE_TOTAL_SUPPLY)" ]   || set_kv STAKE_TOTAL_SUPPLY 1000000000
[ -n "$(get_kv SEASON_END_TS)" ]        || set_kv SEASON_END_TS "$(date -d '+60 days' +%s 2>/dev/null || date -v+60d +%s)"
[ -n "$(get_kv SEASON_BURN_GOAL_PCT)" ] || set_kv SEASON_BURN_GOAL_PCT 5

prompt_token() {  # prompt_token KEY LABEL — a skip or bad token must never abort the run
  local key="$1" label="$2" tok uname
  tok="$(get_kv "$key")"
  if [ -z "$tok" ]; then
    ask "$label token from @BotFather (Enter = skip, that bot stays off): "
    tok="$REPLY"
    if [ -n "$tok" ]; then set_kv "$key" "$tok"; fi
  fi
  if [ -n "$tok" ]; then
    uname="$(tg_username "$tok" || true)"
    if [ -n "$uname" ]; then
      c_grn "    ✓ $key -> @$uname"
      if [ "$key" = "TG_COMMUNITY_TOKEN" ] && [ -z "$(get_kv MINIAPP_BOT_USERNAME)" ]; then
        set_kv MINIAPP_BOT_USERNAME "$uname"
      fi
    else
      c_yel "    ⚠ $key set but Telegram getMe failed — check it (kept anyway)"
    fi
  fi
  return 0
}
prompt_token TG_COMMUNITY_TOKEN "Community bot (/price, Mini App, notifier)"
prompt_token TG_BOT_TOKEN       "Guardian bot (anti-scam gatekeeper)"
prompt_token TG_SCANNER_TOKEN   "Scanner bot (RugCheck verdicts)"
if [ -z "$(get_kv TG_ADMIN_IDS)" ]; then
  ask "Your numeric Telegram id (@userinfobot; Enter = skip): "
  if [ -n "$REPLY" ]; then set_kv TG_ADMIN_IDS "$REPLY"; fi
fi
if [ -z "$(get_kv TG_ALERTS_CHAT)" ] && [ -n "$(get_kv TG_COMMUNITY_TOKEN)" ]; then
  ask "Alerts channel (@yourchannel; bot must be admin; Enter = skip alerts): "
  if [ -n "$REPLY" ]; then set_kv TG_ALERTS_CHAT "$REPLY"; fi
fi
chown "$RUN_USER:$RUN_USER" "$ENVF" && chmod 600 "$ENVF"

echo "==> [5/7] python venvs + dependencies"
sudo -u "$RUN_USER" bash -c "
  set -e; cd '$STACK'
  [ -x venv/bin/python ] || python3 -m venv venv
  venv/bin/pip -q install --upgrade pip
  venv/bin/pip -q install -r requirements.txt
  [ -x staking-api/venv/bin/python ] || python3 -m venv staking-api/venv
  staking-api/venv/bin/pip -q install --upgrade pip
  staking-api/venv/bin/pip -q install -r staking-api/requirements.txt
"

echo "==> [6/7] systemd + caddy"
# Enable only services whose config is complete; the rest are installed but
# left off (re-run bootstrap after adding tokens to .env to bring them up).
declare -A NEED=(
  [staking]=""                       # always on (secrets generated above)
  [notifier]="TG_COMMUNITY_TOKEN|TG_NOTIFY_TOKEN"
  [guardian]="TG_BOT_TOKEN"
  [scanner]="TG_SCANNER_TOKEN"
  [community]="TG_COMMUNITY_TOKEN"
  [alerts]="TG_ALERTS_CHAT"
)
enable_list=()
for svc in staking notifier guardian scanner community alerts; do
  sed -e "s#/home/youruser/bots#$STACK#g" -e "s#User=youruser#User=$RUN_USER#g" \
      "$STACK/systemd/degen-$svc.service" > "/etc/systemd/system/degen-$svc.service"
  ok=1
  if [ -n "${NEED[$svc]}" ]; then
    ok=0
    IFS='|' read -ra keys <<< "${NEED[$svc]}"
    for k in "${keys[@]}"; do [ -n "$(get_kv "$k")" ] && ok=1; done
  fi
  if [ "$svc" = alerts ] && [ -z "$(get_kv TG_ALERTS_TOKEN)$(get_kv TG_COMMUNITY_TOKEN)" ]; then
    ok=0  # alerts needs a token in addition to the channel
  fi
  if [ "$ok" = 1 ]; then enable_list+=("degen-$svc"); else
    systemctl disable --now "degen-$svc" >/dev/null 2>&1 || true
    c_yel "    ⚠ degen-$svc installed but OFF (missing: ${NEED[$svc]//|/ or })"
  fi
done
sed -e "s#/home/youruser/bots#$STACK#g" -e "s#User=youruser#User=$RUN_USER#g" \
    "$STACK/systemd/degen-reconcile.service" > /etc/systemd/system/degen-reconcile.service
cp "$STACK/systemd/degen-reconcile.timer" /etc/systemd/system/degen-reconcile.timer
systemctl daemon-reload
systemctl enable --now "${enable_list[@]}" degen-reconcile.timer
systemctl restart "${enable_list[@]}"

if ! command -v caddy >/dev/null 2>&1; then
  apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https >/dev/null
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  apt-get update -qq && apt-get install -y -qq caddy >/dev/null
fi
sed "s/app\.cleanhands\.fun/$DOMAIN/g" "$STACK/deploy/Caddyfile" > /etc/caddy/Caddyfile
systemctl restart caddy
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
fi

echo "==> [7/7] verification"
ok_local=0
for _ in $(seq 1 20); do
  curl -fsS -m 3 http://127.0.0.1:8090/healthz >/dev/null 2>&1 && { ok_local=1; break; }
  sleep 2
done
[ "$ok_local" = 1 ] && c_grn "    ✓ staking API healthy on :8090" \
  || { c_red "    ✗ staking API not responding — journalctl -u degen-staking -n 50"; exit 1; }
ok_pub=0
for _ in $(seq 1 30); do  # first TLS issuance can take ~30-60s
  curl -fsS -m 5 "https://$DOMAIN/healthz" >/dev/null 2>&1 && { ok_pub=1; break; }
  sleep 3
done
[ "$ok_pub" = 1 ] && c_grn "    ✓ https://$DOMAIN/healthz reachable (TLS live)" \
  || c_yel "    ⚠ public URL not up yet — DNS/TLS may still be propagating; re-check: curl https://$DOMAIN/healthz"
if curl -fsS -m 10 "https://$DOMAIN/" 2>/dev/null | grep -q Fraunces; then
  c_grn "    ✓ new sky Mini App UI is being served"
fi

echo
c_grn "──────────────────────────────────────────────────────────"
c_grn " LIVE: https://$DOMAIN/         (Mini App + /api/*)"
echo  " Enabled: ${enable_list[*]}"
echo
echo  " Remaining (only you can do these, ~2 min in Telegram):"
echo  "   1. @BotFather -> /newapp on the community bot"
echo  "      URL: https://$DOMAIN/   short name: $(get_kv MINIAPP_SHORT_NAME)"
echo  "   2. cd $STACK && venv/bin/python configure.py   (names/commands/menu)"
echo  "   3. Open the Mini App on your phone -> connect wallet -> stake."
echo
echo  " Add a skipped token later: nano $ENVF  then re-run this script."
echo  " Payouts: cd $STACK/staking-api && ../venv/bin/python pay.py list"
c_grn "──────────────────────────────────────────────────────────"
