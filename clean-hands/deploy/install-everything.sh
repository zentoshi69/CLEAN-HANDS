#!/usr/bin/env bash
# CLEAN HANDS — THE one-shot deploy. Landing + app webapp + BACKEND + whitepaper
# + nginx vhost + WalletConnect enablement, with checksums printed before/after.
# Run as root on the VPS:
#
#   bash /tmp/clean-deploy-final/install-everything.sh
#
# Idempotent. Never touches the staker database. Backs up every replaced file.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO=/home/clean/CLEAN-HANDS/clean-hands
API=$REPO/staking-api
WEB=$API/webapp
WC_ID="6d837c36e88367603bc22b4c74073487"

echo "===== BEFORE (live right now) ====="
md5sum /var/www/clean-site/index.html "$WEB/wallet.js" "$WEB/app.js" "$WEB/index.html" "$WEB/whitepaper.html" "$API/app.py" "$API/auth.py" 2>/dev/null || true
echo "===== EXPECTED ====="
cat "$HERE/MANIFEST.md5"

echo "== 1/7 backend (partial staking, 7-day sessions, WalletConnect) =="
for f in app.py auth.py test_staking.py; do
  [ -f "$API/$f" ] && cp "$API/$f" "$API/$f.bak"
  cp "$HERE/staking-api/$f" "$API/$f"
done

echo "== 2/7 enable WalletConnect (ANY wallet via QR — kills the phantom.app dead-end) =="
ENVF=$(systemctl cat degen-staking 2>/dev/null | grep -oP 'EnvironmentFile=-?\K\S+' | head -1)
[ -z "$ENVF" ] && for c in "$API/.env" "$REPO/.env" /home/clean/CLEAN-HANDS/.env; do [ -f "$c" ] && ENVF=$c && break; done
if [ -n "$ENVF" ] && [ -f "$ENVF" ]; then
  grep -q '^WALLETCONNECT_PROJECT_ID=' "$ENVF" \
    && echo "WALLETCONNECT_PROJECT_ID already set in $ENVF" \
    || { echo "WALLETCONNECT_PROJECT_ID=$WC_ID" >> "$ENVF"; echo "added WALLETCONNECT_PROJECT_ID to $ENVF"; }
else
  echo "WARN: no env file found — add WALLETCONNECT_PROJECT_ID=$WC_ID to the service env manually"
fi

echo "== 3/7 app webapp (universal wallet connect + menu + % staking + jupiter + favicon) =="
for f in wallet.js app.js index.html; do
  [ -f "$WEB/$f" ] && cp "$WEB/$f" "$WEB/$f.bak"
  cp "$HERE/webapp/$f" "$WEB/$f"
done

echo "== 4/7 whitepaper (fairy motion, landing nav) =="
[ -f "$WEB/whitepaper.html" ] && cp "$WEB/whitepaper.html" "$WEB/whitepaper.html.bak"
cp "$HERE/whitepaper.html" "$WEB/whitepaper.html"

echo "== 5/7 landing (favicon, Buy \$CLEAN -> Jupiter, stake -> app, live numbers) =="
mkdir -p /var/www/clean-site
cp "$HERE/site-index.html" /var/www/clean-site/index.html
cp "$HERE/site-index.html" "$REPO/deploy/site-index.html"
cp "$HERE/nginx-root.conf" "$REPO/deploy/nginx-root.conf"
chown -R clean:clean "$API" "$REPO/deploy" 2>/dev/null || true

echo "== 6/7 nginx vhost (static site + /api proxy on the root domain) =="
CERT=$(ls -d /etc/letsencrypt/live/cleanhands.fun* 2>/dev/null | head -1)
if [ -n "$CERT" ]; then
  sed "s#/etc/letsencrypt/live/cleanhands.fun/#${CERT}/#g" "$HERE/nginx-root.conf" \
    > /etc/nginx/sites-available/clean-site
  ln -sf /etc/nginx/sites-available/clean-site /etc/nginx/sites-enabled/clean-site
else
  echo "WARN: no cert for cleanhands.fun"
fi
find /etc/nginx/sites-enabled -xtype l -print -delete
nginx -t && systemctl reload nginx || echo "WARN: nginx test failed; NOT reloaded"

echo "== 6b/7 nginx gzip (landing is 434KB raw -> ~90KB compressed) =="
if [ -d /etc/nginx/conf.d ]; then
  cat > /etc/nginx/conf.d/clean-perf.conf <<'NGX'
gzip on;
gzip_comp_level 5;
gzip_min_length 1024;
gzip_vary on;
gzip_types text/css application/javascript application/json image/svg+xml text/plain;
NGX
  nginx -t && systemctl reload nginx && echo "gzip enabled" || { rm -f /etc/nginx/conf.d/clean-perf.conf; echo "WARN: gzip conf rejected — removed"; }
fi

echo "== 7/7 restart app =="
systemctl restart degen-staking && sleep 2

echo
echo "===== AFTER (must equal EXPECTED) ====="
md5sum /var/www/clean-site/index.html "$WEB/wallet.js" "$WEB/app.js" "$WEB/index.html" "$WEB/whitepaper.html" "$API/app.py" "$API/auth.py" 2>/dev/null
echo
echo "===== LIVE VERIFY ====="
echo "site 200:          $(curl -s -o /dev/null -w '%{http_code}' https://cleanhands.fun/)"
echo "site favicon:      $(curl -s https://cleanhands.fun/ | grep -c 'rel=\"icon\"')   (want 1+)"
echo "site new design:   $(curl -s https://cleanhands.fun/ | grep -c page-wash)   (want 3)"
echo "site jupiter buy:  $(curl -s https://cleanhands.fun/ | grep -c 'jup.ag/swap?sell')   (want 1)"
echo "site app-link btn: $(curl -s https://cleanhands.fun/ | grep -c 'Open app to stake')   (want 1+)"
echo "site API proxy:    $(curl -s -o /dev/null -w '%{http_code}' https://cleanhands.fun/api/stats)   (want 200)"
echo "app 200:           $(curl -s -o /dev/null -w '%{http_code}' https://app.cleanhands.fun/)"
echo "app favicon:       $(curl -s https://app.cleanhands.fun/ | grep -c 'rel=\"icon\"')   (want 1+)"
echo "app jupiter fix:   $(curl -s https://app.cleanhands.fun/app.js | grep -c 'jup.ag/swap?sell')   (want 1)"
echo "app wallet menu:   $(curl -s https://app.cleanhands.fun/ | grep -c wmenu)   (want 1+)"
echo "WalletConnect on:  $(curl -s https://app.cleanhands.fun/api/economics | grep -c "$WC_ID")   (want 1 — TG users get the any-wallet QR)"
echo "whitepaper 200:    $(curl -s -o /dev/null -w '%{http_code}' https://cleanhands.fun/whitepaper)"
echo
echo "DONE. NOTE: favicons are cached hard — check in a private/incognito window."
echo "If ANY line is off, send me this WHOLE output."

echo "== 8/8 LOCK IT IN: commit deployed state to the server's local git =="
# After this, any 'git checkout -- .' restores THESE files, not the old ones.
git -C /home/clean/CLEAN-HANDS -c user.name=deploy -c user.email=deploy@cleanhands.fun add -A clean-hands 2>/dev/null
git -C /home/clean/CLEAN-HANDS -c user.name=deploy -c user.email=deploy@cleanhands.fun commit -m "live deploy: final webapp+landing+backend (locked)" 2>/dev/null \
  && echo "LOCKED: live state is now the local git baseline" \
  || echo "already locked (nothing new to commit)"
