#!/usr/bin/env bash
# ONE-COMMAND production deploy for CLEAN HANDS. Run as root on the VPS:
#
#   cd /home/clean/CLEAN-HANDS && sudo -u clean git checkout -- . && \
#     sudo -u clean git pull && sudo bash clean-hands/deploy/redeploy.sh
#
# Idempotent. Never touches .env or the staker database.
set -u
APP=/home/clean/CLEAN-HANDS/clean-hands

echo "== website (cleanhands.fun landing) =="
mkdir -p /var/www/clean-site
cp "$APP/deploy/site-index.html" /var/www/clean-site/index.html

echo "== shop product photos =="
mkdir -p /var/www/clean-site/shop
# photos are optional — products without one fall back to the SVG mockup
find "$APP/deploy/shop" -maxdepth 1 \( -name '*.jpg' -o -name '*.jpeg' -o -name '*.png' -o -name '*.webp' \) \
  -exec cp {} /var/www/clean-site/shop/ \; 2>/dev/null || true

echo "== root-domain nginx vhost (static site + same-origin API proxy) =="
CERT=$(ls -d /etc/letsencrypt/live/cleanhands.fun* 2>/dev/null | head -1)
if [ -n "$CERT" ]; then
  sed "s#/etc/letsencrypt/live/cleanhands.fun/#${CERT}/#g" "$APP/deploy/nginx-root.conf" \
    > /etc/nginx/sites-available/clean-site
  ln -sf /etc/nginx/sites-available/clean-site /etc/nginx/sites-enabled/clean-site
else
  echo "WARN: no cert for cleanhands.fun yet — run: certbot --nginx -d cleanhands.fun --redirect -n --agree-tos -m you@example.com"
fi
find /etc/nginx/sites-enabled -xtype l -print -delete
nginx -t && systemctl reload nginx || echo "WARN: nginx test failed; not reloaded"

echo "== app =="
chown -R clean:clean /home/clean/CLEAN-HANDS
systemctl restart degen-staking && sleep 2

echo "== VERIFY =="
echo "app:       $(curl -s -o /dev/null -w '%{http_code}' https://app.cleanhands.fun/)"
echo "wp(app):   $(curl -s https://app.cleanhands.fun/whitepaper | grep -oc data-econ) live spans"
echo "website:   $(curl -s -o /dev/null -w '%{http_code}' https://cleanhands.fun/)"
echo "wp(site):  $(curl -s -o /dev/null -w '%{http_code}' https://cleanhands.fun/whitepaper)"
echo "site live: $(curl -s https://cleanhands.fun/ | grep -oc lv-staked) stat hooks"
