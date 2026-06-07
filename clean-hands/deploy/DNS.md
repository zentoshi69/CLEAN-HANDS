# 🌐 DNS — point the app subdomain at your server

You only need **one new record** for the Mini App / API. Your main website's
DNS (root + www) stays wherever it already is.

| Type | Host (name) | Value               | TTL | Purpose                                             |
| ---- | ----------- | ------------------- | --- | --------------------------------------------------- |
| A    | `app`       | `<your bot VPS IP>` | 300 | Mini App + staking API (Caddy serves it over HTTPS) |
| AAAA | `app`       | `<VPS IPv6 if any>` | 300 | same, over IPv6                                     |

> At **Hostinger → Add Record**: Type `A`, Name `app`, Value `<VPS IP>`, TTL 300.
> You already have `A @ → 2.57.91.91`; if the bots run on **that same box**, use
> `2.57.91.91` for `app` too. If they run on a **separate always-on VPS**
> (recommended), use that VPS's IP. Leave your existing `@` and `www` records alone.

So `app.cleanhands.fun` → your VPS. That host becomes:

- the **Telegram Mini App URL** you give BotFather: `https://app.cleanhands.fun/`
- the **API** the website + app call: `https://app.cleanhands.fun/api/...`

### Steps

1. Add the `A` (and `AAAA`) record above at your DNS provider.
2. Wait for it to resolve: `dig +short app.cleanhands.fun` should show your IP.
3. Make sure the server's firewall allows **80 + 443** (Caddy needs 80 to issue
   the TLS cert, 443 to serve): `sudo ufw allow 80,443/tcp`.
4. Start Caddy (see `Caddyfile`) — it auto-provisions a Let's Encrypt cert.
5. Verify: `curl -I https://app.cleanhands.fun/healthz` → `HTTP/2 200`.

> Keep the API process bound to `127.0.0.1:8090` (localhost) so only Caddy can
> reach it; never expose 8090 publicly.
