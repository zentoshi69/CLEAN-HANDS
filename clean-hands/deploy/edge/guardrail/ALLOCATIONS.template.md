# ALLOCATIONS — per-app ledger for this box

Rules (non-negotiable):

- **Append-only.** One line per app (or per meaningful change to an app).
  On retirement, ~~strike the line through~~ — never delete it. Deleted
  history is how the next agent re-allocates a "free" name that isn't.
- **Every identifier is TAKEN until a live command proves it FREE** — this
  ledger narrows the search, it never replaces `ss -tulpn` / `docker ps -a`.
- **Every infra change appends here AND updates `/root/INFRA.md` in the same
  session.** An agent that changed infra without updating the registries has
  not finished.
- Host ports: web-facing = NONE, ever (the ingress owns 80/443; it reaches
  containers by name). A genuinely-needed debug/non-HTTP port is
  `127.0.0.1:PORT` only, scanned free first, and recorded here.

| date | app | container(s) | networks | host ports | domains | edge config |
|------|-----|--------------|----------|------------|---------|-------------|
| YYYY-MM-DD | (example) cleanhands | cleanhands-api, cleanhands-postgres-prod, cleanhands-redis-prod, cleanhands-site-prod-* | cleanhands-prod-net, <shared-edge-net> | 127.0.0.1:18090 (debug) | cleanhands.fun, www.cleanhands.fun, app.cleanhands.fun | sites/cleanhands.caddy |
