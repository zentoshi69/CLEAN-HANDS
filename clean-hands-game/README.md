# Clean Hands, Dirty Money — game (standalone deliverable)

A self-contained, mobile-first tap + idle launderer built to the final design.
**It does not touch the existing repo.** It runs on its own so we can review the
look/feel and the foundation, then decide how to integrate tomorrow.

```
clean-hands-game/
  index.html     # structure + the Dubai scene (CSS/SVG)
  game.css       # the whole look (glassmorphism, scene, animations)
  game.js        # the engine (tap, idle, heat, perks, bribes, rep, escape, save/sync)
  server.py      # foundational backend (serves the game + save/load/leaderboard)
  requirements.txt
```

## Run it

```bash
cd clean-hands-game
python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/uvicorn server:app --reload --port 8001
# open http://localhost:8001  (use a phone viewport / DevTools device mode)
```

The game also runs with **no backend at all** — just open `index.html`. Progress
falls back to `localStorage`; the leaderboard/cloud-save light up only when
`server.py` is reachable.

## What's in it (matches the screenshot)

- **Top pills:** LEVEL · 📍 city · GOAL (escape target).
- **HEAT bar** with live subtitle ("local authorities are watching." → "RAID
  INCOMING"). Tapping builds heat; idle cools it; **100% → BUSTED** (lose a cut).
- **Big counter** + "laundering $X/s".
- **$CLEAN chip** with BUY (posts `{type:'clean:buy'}` to a parent host when
  embedded; opens Jupiter standalone).
- **Tap scene:** gloved hands + cash over a Dubai skyline (sky/skyline/lagoon/
  marble/mullions/palms in CSS+SVG), with `+$` floaters, riffle + pop feedback.
- **Featured upgrade** card (auto-picks your best-value buy) + **stats row**
  ($/s · ×mult · perks · taps).
- **Bottom nav:** LAUNDER · PERKS · BRIBES · REP (x/8) · ESCAPE.
- **PERKS** (×2 tap, ×2 idle, −heat), **BRIBES** (instant heat cuts, scaling
  cost), **REP** (8 badges), **ESCAPE** (prestige to a richer city for a
  permanent ×1.75) + a **Most Wanted** leaderboard.
- Offline earnings, confetti on badges/escape, haptics, reduced-motion support.

All tunables (heat rates, costs, multipliers, city list, upgrades) live at the
top of `game.js`.

## Backend foundation

`server.py` is a tiny FastAPI app:

| Route | Method | Purpose |
|---|---|---|
| `/api/load` | POST `{player}` | fetch a saved blob |
| `/api/save` | POST `{player, state, score, name}` | upsert progress (validated, size-capped) |
| `/api/leaderboard` | GET `?limit=` | top scores |
| `/` `/game.css` `/game.js` | GET | static |

Progress is an **opaque JSON blob** owned by the client + a numeric `score`
(lifetime laundered) for ranking — stored in SQLite (`game.db`).

## Integration plan (tomorrow)

This was built to drop into `staking-api` with minimal change:

1. **Identity:** replace the anonymous `player` id with the existing wallet auth
   — `wallet = _require(token)["w"]`. The client already supports a host: it
   listens for `{type:'clean:price'}` and posts `{type:'clean:buy'}`, so the
   mini app can pass the live price + wire BUY to `App.buy()`.
2. **DB:** the schema matches a `game_state(wallet, state, score, updated_ts)`
   table — add it as staking-api migration **v9** (already prototyped) and reuse
   `db.db()`.
3. **Routes:** mount `/api/game/load|save|leaderboard` on the FastAPI app and
   serve the three static files (or fold into `webapp/`). CSP already allows
   same-origin scripts/styles + `data:` images.
4. **Embed:** point the mini app's Game tab at the self-hosted `/game` instead
   of the external Vercel build.

Nothing above is wired yet — that's the call we make together tomorrow.
