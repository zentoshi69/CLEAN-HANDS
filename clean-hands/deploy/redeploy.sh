#!/usr/bin/env bash
# ONE-COMMAND production deploy for CLEAN HANDS. Run as root on the VPS:
#
#   cd /home/clean/CLEAN-HANDS && sudo -u clean git stash --include-untracked && \
#     sudo -u clean git pull --ff-only && sudo bash clean-hands/deploy/redeploy.sh
#
# NOTE: use `git stash` (recoverable), never `git checkout -- .` — the latter
# silently discards any hotfix made on the box. `--ff-only` refuses to clobber a
# diverged local history instead of force-merging.
#
# Idempotent. Never touches .env or the staker database.
set -euo pipefail
APP=/home/clean/CLEAN-HANDS/clean-hands

echo "== website (cleanhands.fun landing) =="
mkdir -p /var/www/clean-site
cp "$APP/deploy/site-index.html" /var/www/clean-site/index.html
# standalone landing pages (pretty URLs /boost and /bridge via nginx try_files)
cp "$APP/deploy/boost.html" /var/www/clean-site/boost.html
cp "$APP/deploy/bridge.html" /var/www/clean-site/bridge.html
cp "$APP/deploy/brand.html" /var/www/clean-site/brand.html

echo "== shop product photos =="
mkdir -p /var/www/clean-site/shop
# photos are optional — products without one fall back to the SVG mockup
find "$APP/deploy/shop" -maxdepth 1 \( -name '*.jpg' -o -name '*.jpeg' -o -name '*.png' -o -name '*.webp' \) \
  -exec cp {} /var/www/clean-site/shop/ \; 2>/dev/null || true

echo "== root-domain nginx vhost (static site + same-origin API proxy) =="
# Prefer the canonical cert dir; only fall back to a renewed `-NNNN` variant.
# (A bare `cleanhands.fun*` glob could splice in an unrelated cert dir.)
if [ -d /etc/letsencrypt/live/cleanhands.fun ]; then
  CERT=/etc/letsencrypt/live/cleanhands.fun
else
  CERT=$(ls -d /etc/letsencrypt/live/cleanhands.fun-* 2>/dev/null | sort | head -1 || true)
fi
if [ -n "$CERT" ]; then
  sed "s#/etc/letsencrypt/live/cleanhands.fun/#${CERT}/#g" "$APP/deploy/nginx-root.conf" \
    > /etc/nginx/sites-available/clean-site
  ln -sf /etc/nginx/sites-available/clean-site /etc/nginx/sites-enabled/clean-site
else
  echo "WARN: no cert for cleanhands.fun yet — run: certbot --nginx -d cleanhands.fun --redirect -n --agree-tos -m you@example.com"
fi
# Drop ONLY our own symlink if it went stale — never sweep the whole dir, which
# could delete an unrelated app's vhost on a shared box.
[ -L /etc/nginx/sites-enabled/clean-site ] && [ ! -e /etc/nginx/sites-enabled/clean-site ] \
  && rm -f /etc/nginx/sites-enabled/clean-site || true
nginx -t && systemctl reload nginx || echo "WARN: nginx test failed; not reloaded"

echo "== app =="
chown -R clean:clean /home/clean/CLEAN-HANDS
systemctl restart degen-staking && sleep 2

echo "== VERIFY =="
echo "app:       $(curl -s -o /dev/null -w '%{http_code}' https://app.cleanhands.fun/)"
echo "wp(app):   $(curl -s https://app.cleanhands.fun/whitepaper | grep -oc data-act || true) acts"
echo "website:   $(curl -s -o /dev/null -w '%{http_code}' https://cleanhands.fun/)"
echo "wp(site):  $(curl -s -o /dev/null -w '%{http_code}' https://cleanhands.fun/whitepaper)"
echo "site live: $(curl -s https://cleanhands.fun/ | grep -oc lv-staked || true) stat hooks"
