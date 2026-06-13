# No Stains Bridge — white-label EasyBit (in-app swap)

The bridge tab lets users swap/bridge any coin across chains **inside the app
and on the website**, instead of bouncing them to an external exchange page.

It's a true white-label: the EasyBit API key lives **only on the server**. The
browser/Mini App talks exclusively to our own `/api/bridge/*` endpoints, which
proxy to EasyBit. The user never sees EasyBit, and the key never reaches the
client.

The swap is **non-custodial** — funds go *user → EasyBit → the user's receive
address*. This app never holds funds and never has a private key.

---

## Two modes (chosen automatically)

| `EASYBIT_API_KEY` | `bridgeMode` | Behaviour |
|---|---|---|
| **set** | `api` | The real in-app swap form (quote → deposit address → live status). |
| unset | `link` | The previous branded launch-card / iframe to `MINIAPP_BRIDGE_URL`. Unchanged. |

Leaving the key unset preserves the old behaviour exactly — nothing breaks.

---

## The fee model — flat $5, paid to your reserve wallet

EasyBit pays partners a **per-order commission** expressed as a percentage. To
make that commission a flat **$5**, the server computes, per order:

```
fee_pct = clamp( BRIDGE_FEE_USD / order_value_usd * 100,  MIN_PCT, MAX_PCT )
```

and passes it as EasyBit's per-order fee override, so the quoted *receive*
amount the user sees already includes it. Because `fee = order_usd * pct/100`,
the effective fee is a flat **$5** for any order at or above the minimum; the
percentage simply shrinks as the order grows.

The defaults are mutually consistent:

| Setting | Default | Why |
|---|---|---|
| `BRIDGE_MIN_ORDER_USD` | `55` | minimum order |
| `BRIDGE_FEE_USD` | `5` | flat fee |
| `BRIDGE_EXTRA_FEE_MAX_PCT` | `10` | at the $55 min, $5 = 9.09% — must stay below the cap |

> If you raise the fee or lower the minimum, keep
> `BRIDGE_EXTRA_FEE_MAX_PCT ≥ FEE/MIN×100` or the flat $5 gets clamped on the
> smallest orders. `config.py` warns you at startup if they're inconsistent.

### Where the money actually lands

The commission accrues to **your EasyBit partner balance**, which EasyBit pays
out to the affiliate/payout address you set **in your EasyBit dashboard**. Set
that to your **corporate reserve wallet**, and set `BRIDGE_RESERVE_WALLET` here
to the same address (it's recorded for display/reconciliation only — it is
**not** exposed to clients).

---

## USD valuation (for the minimum + fee)

The order's USD value is taken from **EasyBit's own rate to USDT** (stablecoins
short-circuit to face value), cached briefly — single-vendor, and it reflects
the same liquidity the swap will actually use. If a coin can't be priced and
`BRIDGE_REQUIRE_USD=1` (default), the order is blocked with a "try again
shortly" message so the $ minimum is never silently bypassed.

---

## Configuration

See `.env.example` for the full list. The essentials:

```bash
EASYBIT_API_KEY=...            # turns on API mode
BRIDGE_RESERVE_WALLET=...      # your reserve wallet (also set EasyBit payout to it)
# Optional tuning:
BRIDGE_FEE_USD=5
BRIDGE_MIN_ORDER_USD=55
BRIDGE_EXTRA_FEE_MAX_PCT=10
BRIDGE_REQUIRE_USD=1
BRIDGE_BRAND="No Stains Bridge"
```

If the live EasyBit spec ever differs from the defaults, every path/header is
overridable without touching code (`EASYBIT_API_BASE`, `EASYBIT_API_KEY_HEADER`,
`EASYBIT_PATH_*`, `EASYBIT_EXTRA_FEE_PARAM`).

---

## API surface (all same-origin; key stays server-side)

| Method | Path | Purpose | Rate bucket |
|---|---|---|---|
| GET | `/api/bridge/currencies` | supported coins + networks (cached) | `bridge` |
| POST | `/api/bridge/quote` | estimate + fee + min check | `bridge` |
| POST | `/api/bridge/validate-address` | check a destination address | `bridge` |
| POST | `/api/bridge/order` | open an order, returns deposit address | `bridge_order` (strict) |
| GET | `/api/bridge/order/{id}` | live status for polling | `bridge_status` |

`/api/economics` additionally returns `bridgeMode`, `bridgeMinUsd`,
`bridgeFeeUsd`, `bridgeBrand` so the UI shows the same rules the server enforces.

Every endpoint 503s when API mode is off, so the client cleanly falls back to
link mode.

---

## Record-keeping

Every opened order is logged to the `bridge_orders` table (added in schema v6):
the pair, networks, amounts (as exact decimal strings — never floats), the fee
charged, the order's USD value, status, and a **salted HMAC of the client IP**
(abuse forensics without storing a raw IP). The swap works even if logging
fails — persistence never blocks a swap.

---

## Security model (summary)

- **Key isolation** — server-side only; never in any response or the config endpoint.
- **No SSRF** — the EasyBit base URL/paths come from config, never user input; redirects are not followed.
- **Input validation** — coin/network/amount/address/tag/order-id are all regex- and length-bounded before they leave the process; the crypto amount is forwarded as a verified decimal string (no float drift).
- **No DOM injection** — all external/dynamic values render via `textContent`; no new inline scripts or external script/connect sources (CSP-clean).
- **Server-authoritative limits** — the $55 minimum and the fee are recomputed on the server at order time; a tampered client quote can't bypass them.
- **Rate limiting** — per-IP buckets on quotes, order creation (strict), and status polling.
- **Clean errors** — provider messages are surfaced to the user; our own auth/config failures are masked as "temporarily unavailable".
