# $CLEAN — Design-Finish pass (HTML 6 language)

`clean-hands.html` is the backbone (`clean-hands-HTML69.html`) brought from *reskinned* to
*finished*: the `clean-design-system.css` shell was already applied; this pass builds the audit's
**P0–P2 screens**, fixes the ticker, and repairs a class-name mismatch that was silently hiding
three overlays. **Engine logic, economy and balancing are untouched** — every change is CSS, markup
re-templating against the engine's own selectors, or a small, explicitly-listed behaviour hook.

Verified headless (Playwright + Chromium): **13 surfaces boot with zero JS/console errors**, the
core loop still works (tap-earns, buy-deducts), and it's clean at **360 px** and under
**prefers-reduced-motion**.

## Coverage — what this pass completed

| Surface | Engine selector(s) | HTML 6 component | Status |
|---|---|---|---|
| **Flee confirmation** | native `confirm()` → `#modal` | designed modal (🛂, passport chip, trade copy, FLEE/Stay) | ✅ built (P0) |
| **Federal-heat meter** | `#fedstatus` / `#fedFill` | distinct "FEDERAL HEAT" bar + milestone ticks + escalating banner | ✅ built (P0) |
| **FBI raid overlay** | `#raid/.rt/.rs/#bailBtn` | 🚨 siren, red Fraunces seize amount, POST BAIL primary + lie-low | ✅ built (P0) |
| **Prestige / passport hero** | `#paneEscape` (kept `#ppFill/#escInfo/#fleeBtn`) | `.escape-hero` count + `.ec` benefit cards | ✅ built (P1) |
| **Achievements** | `#paneAch` rows | 2-up `.ach-grid` with locked/`.got` cards | ✅ built (P1) |
| **Locale relocate** | `setLocale()` / flee | "✦ Relocated to {city}" chip + scene cross-fade | ✅ built (P1) |
| **Frenzy state** | `.frenzy` on `#scene` | golden scene glow during ×N | ✅ built (P2) |
| **Offline return** | `applyOffline()` | 🌙 "While you were away" card + Collect | ✅ built (P2) |
| **Bribe type tags** | `#paneBribes` rows | DUMP / COOL / SLOW / FED-PASS tinted tags | ✅ built (P2) |
| **Ticker label** | `#ticker` | content fix `$WRP → $CLEAN` | ✅ |
| Shell · hero · heat · scene · tabs · sheet · ops/perks rows · FX · toast | (own selectors) | HTML 6 | ✅ (pre-existing) |

## Behaviour hooks touched (the only JS, all minimal & reversible)

1. **`.on` vs `.show` (CSS-only fix).** The engine toggles `.on` for `#cmodal`, `#modal` and
   `#raid`, but the applied design system only styled `.show`. Result: the reset confirm, the
   invite/trade modal **and the raid overlay never displayed** (no console error — just invisible;
   the bottom sheet survived via a `body.sheet-open` fallback). Fixed by making the CSS respond to
   the engine's real `.on` class. No logic changed.
2. **`doPrestige()`** split into `doPrestige()` (opens the designed flee modal) + `_commitPrestige()`
   (the original wipe, verbatim) — replaces the native `confirm()`.
3. **`applyOffline()`** now shows `showOfflineCard(...)` instead of a plain toast.
4. **`render()`** re-templates the federal-heat block, populates the passport hero + raid seize, and
   toggles `.frenzy`; the `#paneAch`/`#paneEscape`/`#paneBribes` builders emit HTML 6 markup. All
   engine IDs/handlers preserved.
5. Content: reset-modal title `&amp;` (shown literally via `textContent`) → `&`.

## Assets (you'll supply — hands/money sprites come later)

Scene falls back to the embedded villa when photos are absent. Engine expects, under `assets/`:
`bg0.jpg` + `loc_{amsterdam,dubai,monaco,caribbean,swiss,island}.jpg` (7 locales) ·
`f00–f03.webp` (main riffle, 4 frames) · `h0–h2.webp` (helper hands) · `sfx_*.mp3` + `music_*.mp3`.
The 4-frame riffle system is wired and ready for the new sprite sets.

## Re-run the QA (the durable design loop)

```bash
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers NODE_PATH=$(npm root -g)
# point the harness at the game (FILE in each script), then:
node design-qa/verify.js   # 13 surfaces → screenshots + console-error report
node design-qa/dod.js      # 360px + reduced-motion + tap/buy gameplay smoke
```
