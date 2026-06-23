#!/usr/bin/env bash
#
# healthcheck.sh — verify a deployed CLEAN stack end to end.
#   bash healthcheck.sh app.cleanhands.fun
#
set -uo pipefail
HOST="${1:-}"
[ -z "$HOST" ] && { echo "usage: bash healthcheck.sh <app-domain>"; exit 2; }
BASE="https://$HOST"
fail=0

check() { # name, url, expected-substring
  local name="$1" url="$2" want="$3"
  local body code
  body=$(curl -fsS --max-time 10 "$url" 2>/dev/null)
  code=$?
  if [ $code -ne 0 ]; then
    echo "❌ $name — request failed ($url)"; fail=1; return
  fi
  if [ -n "$want" ] && ! printf '%s' "$body" | grep -q "$want"; then
    echo "❌ $name — unexpected response"; fail=1; return
  fi
  echo "✅ $name"
}

echo "Checking $BASE …"
check "API health"      "$BASE/healthz"        '"ok"'
check "API readiness"   "$BASE/readyz"         '"ok":true'
check "Mini App served" "$BASE/"               "CLEAN"
check "Terms page"      "$BASE/whitepaper"     "Payouts are requests"
check "Price endpoint"  "$BASE/api/price"      "price_usd"
check "Economics/config" "$BASE/api/economics" "base_apr"
check "Wallet.js asset"  "$BASE/wallet.js"     "CleanWallet"

# TLS cert sanity
if curl -fsSI --max-time 10 "$BASE/healthz" | grep -qi "^HTTP/2 200\|200"; then
  echo "✅ HTTPS reachable"
else
  echo "❌ HTTPS not reachable"; fail=1
fi

echo
[ $fail -eq 0 ] && echo "🎉 All green — stack is live." || echo "⚠️  Some checks failed (see above)."
exit $fail
