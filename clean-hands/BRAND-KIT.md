# CLEAN — Brand Kit

> Clean hands. Dirty money. The soft-staking memecoin on Solana.

This is the single source of truth for the CLEAN visual + verbal identity. It is
kept coherent with what actually ships in the product today:

- **Landing site** — `clean-hands/deploy/site-index.html` (cleanhands.fun)
- **Mini-app** — `clean-hands/staking-api/webapp/` (app.cleanhands.fun)

Both surfaces share the same tokens, type and voice. If you change a token here,
change it in **both** `:root` blocks.

---

## 1. Logo

| Asset | File | Use |
|---|---|---|
| Primary mark | `assets/glove.png` (320×320, circular, transparent corners) | Nav, app header, favicon, OG image. Served at `/glove.png`. |
| Small mark | `assets/glove-sm.png` (96×96) | Tiny placements, low-bandwidth favicons |
| Alt mark | `assets/glove-raw.png` (1254×1254, raised gloved fist) | Hi-res hero art, stickers, merch, print |
| Banner | `assets/banner.png` | Social / repo banner |

**Rules**
- Always render the primary mark **circular** with a hairline ring
  (`border:1.5px solid var(--line); border-radius:50%; background:#fff`).
- Minimum size 28px. Don't recolor, stretch, or drop the sparkles.
- Clear space ≥ 25% of the mark's diameter on all sides.
- `/glove.png` is proxied from the app on both domains, so updating
  `assets/glove.png` updates every surface after a redeploy.

---

## 2. Color tokens

The palette is "spotless laundromat" — soft sky blues, deep ink, a touch of
skin + Solana accents. Defined identically in both `:root` blocks.

| Token | Hex | Role |
|---|---|---|
| `--sky` | `#86BFF8` | Primary sky |
| `--sky-2` | `#A9D3FB` | Lighter sky |
| `--sky-soft` | `#D6EAFD` | Wash tint |
| `--glove` | `#EAF4FE` | Surface tint / chips |
| `--paper` | `#F4FAFF` | App background |
| `--white` | `#FFFFFF` | Cards |
| `--ink` | `#1B5DA6` | Deep brand blue |
| `--ink-2` | `#2E74C0` | **Primary action** (buttons, links, active) |
| `--ink-deep` | `#0F3E73` | Headings, display numerals |
| `--text` | `#16385C` | Body text |
| `--muted` | `#4D6E93` | Secondary text — **AA-contrast (~5:1), do not lighten** |
| `--skin` | `#E7B492` | Hand/skin accent in art |
| `--sol` | `#9945FF` | Solana purple (sparingly) |
| `--sol-2` | `#14F195` | Solana green — "live"/connected dot |
| `--fire` | `#FF7A3C` | Burn / boost accent |
| `--line` | `rgba(27,93,166,.16)` | Hairline borders |
| `--line-2` | `rgba(27,93,166,.28)` | Stronger borders |

**Accessibility:** `--muted` was darkened from `#5D7EA3` to `#4D6E93` to clear
WCAG AA (4.5:1) for small text. Keep it ≥ AA. Keyboard focus uses a 2.5px
`--ink-deep` ring at 2px offset.

---

## 3. Typography

| Family | Use | Weights |
|---|---|---|
| **Fraunces** (serif) | Display: headings, big numerals, prices | 500 / 600 / 700 |
| **Plus Jakarta Sans** (sans) | Body, UI, labels | 400–700 |
| **Caveat** (script) | Playful accents only (e.g. "soft staking", sparkle notes) | 600 / 700 |

- Numerals use `font-variant-numeric: tabular-nums`.
- Captions/labels: small (~0.62–0.78rem), often uppercase with letter-spacing.
- Line-height 1.5–1.55 for body.

---

## 4. Voice & tone

Playful, confident, a little degenerate — but never sleazy. The running gag is
**laundry / gloves / staying spotless** over crypto mechanics.

- Tagline: **"Clean hands. Dirty money."**
- Signature sparkle: **`✦`** (use as a suffix flourish: "gloves on ✦").
- Verbs: *wash, rinse, soak, spin cycle, burn to boost, stay spotless.*
- Staking = "soft staking" — **tokens never leave your wallet** (non-custodial;
  always say this where staking is mentioned — it's the core trust promise).
- Keep disclaimers honest: "A meme. Not financial advice."

---

## 5. Components (shared language)

- **Cards** — glassmorphism: `rgba(255,255,255,.66)` + `backdrop-filter:blur(8px)`,
  `1.5px var(--line)` border, radius 22px (app) / 26px (`--r`, site), soft shadow.
- **Buttons** — `.btn-solid` (ink-2 fill, white) primary; `.btn-ghost` (white/translucent,
  ink-2 text) secondary; `.btn-sm` compact utility variant. Radius 12–14px.
- **Pills/chips** — fully rounded (100px), `--glove` bg, `--line` border.
- **Sparkles (`✦`)** — animated "shining stars" FX on shop + checkout; static ✦ as
  text flourish elsewhere.
- **Live dot** — `--sol-2` green pulse = connected / live.
- **Toasts** — ink-deep pill, bottom-center, `role=status aria-live=polite`.

---

## 6. Links & identity

| Channel | Handle / URL | Status |
|---|---|---|
| Telegram | [@CLEANHANDSDIRTYMONEY](https://t.me/CLEANHANDSDIRTYMONEY) | Live |
| X / Twitter | [@Cleanhandscoin](https://x.com/Cleanhandscoin) | Live |
| DexScreener | [pair bdcc5x…1rupu](https://dexscreener.com/solana/bdcc5xbzdmyfnybnzyb4aasnyamqczpnlnqhzbn1rupu) | Live |
| Discord | — | **Coming soon** |
| Site | https://cleanhands.fun | Live |
| App | https://app.cleanhands.fun | Live |

- **$CLEAN SPL mint:** the app reads the live mint from `/api/economics`; the
  site fallback is `6jb4XWggYJjoo3fx7irPVxhNiuFbHUyVyKR8mBL8pump` (`CONFIG.CONTRACT`).
- Solana network: `mainnet-beta`, token decimals `6`.

---

## 7. Coming-soon convention

Anything not live yet uses the **coming-soon** pattern so the page never dead-ends:
- Site: `data-soon` on a link → "Link coming soon ✦" toast; or the `.soon` badge pill.
- Use it for Discord and any feature awaiting backend keys (e.g. live bridge).
