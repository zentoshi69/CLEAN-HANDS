# Shop product photos

Real product photography for the merch shop on cleanhands.fun. Drop image
files in this folder, reference them from the `PRODUCTS` array in
`../site-index.html` as `imgs:['shop/<filename>', …]`, commit, and redeploy
(`git pull && sudo bash clean-hands/deploy/redeploy.sh` on the VPS — it copies
this folder to `/var/www/clean-site/shop/`).

If a referenced photo is missing or fails to load, the shop automatically
falls back to the drawn SVG mockup for that product — the page never breaks.

## Filenames currently wired up in PRODUCTS

| File | Product | Shot |
|---|---|---|
| `tee-money-flat.jpg` | Dirty Money Tee | front/back flat lay (bills + crest) |
| `tee-money-pack.jpg` | Dirty Money Tee | folded in poly bag with hang tag |
| `hoodie-wash-flat.jpg` | Heavy Wash Hoodie | front/back flat lay (crest + sparkle back) |
| `hoodie-wash-pack.jpg` | Heavy Wash Hoodie | folded in poly bag with hang tag |
| `trunks-money-flat.jpg` | Money Laundering Trunks | front/back flat lay |
| `gloves-box.jpg` | CLEAN Nitrile Gloves | 100ct retail box |
| `gloves-sachets.jpg` | CLEAN Nitrile Gloves | single-use sachets |

All photos are committed here (JPEG, ≤1200px long edge, quality 82 — ~60-120KB
each). To swap one, overwrite the file, keep the name, redeploy.
