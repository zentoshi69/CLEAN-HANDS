# CLEAN HANDS DIRTY MONEY — Glove Bot 🧤

A Telegram bot that adds **light blue transparent medical gloves** to every
visible human hand in an image — and changes **nothing else**. No face edits,
no background repaints, no extra fingers, no meme soup. This is a
glove-inpainting sniper.

User uploads chaos. Bot returns same chaos, but medically gloved.

## How it works

```
photo → EXIF normalize + bounded resize → MediaPipe hand detection
      → per-hand skeleton masks (feathered, wrist-extended, object-avoiding)
      → masked edit via provider (strict glove prompt)
      → quality gate (outside-mask diff + SSIM + glove-effect check)
      → retry once with strict prompt → deliver or fail safely
```

If anything is uncertain — no hands, suspicious mask coverage, provider
output that touched pixels outside the mask — the bot preserves the original
image and fails gracefully instead of returning cursed output.

## Setup

### 1. Create the Telegram bot

1. Open Telegram, talk to [@BotFather](https://t.me/BotFather).
2. `/newbot`, pick a name and username.
3. Copy the token BotFather gives you.

### 2. Configure

```bash
cp .env.example .env
# edit .env:
#   TELEGRAM_BOT_TOKEN=123456:ABC-...   (required)
#   IMAGE_PROVIDER=mock                 (start here)
#   ADMIN_TELEGRAM_IDS=12345678         (your Telegram user id, for /debug)
```

### 3. Run locally

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m src.bot.main
```

### 4. Run with Docker

```bash
docker compose up --build -d
docker compose logs -f bot
```

## Choosing an image provider

Set `IMAGE_PROVIDER` in `.env`:

| Provider | What it does |
|---|---|
| `mock` | Offline glove compositor (alpha-blends #9ED8FF over the hand mask). No API key needed. Great for development and demos. |
| `generic_http` | POSTs `image` + `mask` + `prompt` + `negative_prompt` as multipart form data to `IMAGE_PROVIDER_ENDPOINT` with `Authorization: Bearer $IMAGE_PROVIDER_API_KEY`. Accepts raw image bytes or JSON base64 responses. Works with most inpainting APIs. |
| `future` | Placeholder slot for the next integration. |

To add a real provider, implement `ImageEditProvider.edit_image()` in
`src/image_pipeline/providers/` and register it in
`src/image_pipeline/providers/__init__.py`. Keys are always read from `.env`,
never hardcoded.

## Commands

| Command | Behavior |
|---|---|
| `/start` | Explains the bot. |
| `/meme` | Main command. Reply to a photo with `/meme`, or send a photo with `/meme` as the caption. Adds gloves only. |
| `/retry` | Re-runs your last image with the strict preservation prompt. |
| `/soft` | Sets (and applies, when replying to a photo) the subtle natural transparent glove mode. |
| `/hard` | Sets (and applies, when replying to a photo) the visible glossy glove mode. |
| `/debug` | Admin-only (`ADMIN_TELEGRAM_IDS`). Returns the last hand mask, diff heatmap, confidence scores, and pipeline debug data. |

## Testing with sample images

Drop test images into `samples/` (see `samples/README.md`), then run the
pipeline directly without Telegram:

```bash
python -m src.image_pipeline.pipeline samples/your-image.jpg --mode balanced
```

Outputs land next to the input: `*_gloved.png`, `*_mask.png`, `*_diff.png`.

Run the unit tests:

```bash
pytest tests/ -v
```

## Quality gate

Every provider result is verified before delivery:

- **Dimensions** must match (same-aspect rescales are normalized, aspect
  drift is rejected).
- **Outside the hand mask** the mean pixel delta must stay tiny and SSIM
  high — this single check protects faces, backgrounds, clothes, and held
  objects, because the mask covers hand skin only.
- **Inside the mask** there must be a visible glove effect.

A rejected result is retried once with a stricter prompt; if that also
fails the user gets a clean failure message and their original image is
never replaced with soup.

## Privacy

- Images are stored only under `/tmp/clean-hands-bot/` and deleted after
  `DELETE_FILES_AFTER_HOURS` (default 24h) by a background cleanup job.
- Logs contain only: timestamp, Telegram user id, image hash, success or
  failure, detected hand count, and processing time. No image URLs, no keys.

## Known limitations

- **Very blurry hands** — detection confidence drops; the bot fails safely
  rather than guessing.
- **Tiny hands in the background** — may fall below the minimum mask
  coverage and be skipped.
- **Hands fully hidden by objects** — nothing visible means nothing to
  glove; the bot will not invent hands.
- **Extreme low light** — the contrast-normalization fallback helps, but
  severely underexposed hands may not be found.
- **Heavy occlusion** — only the visible part of a hand is gloved; a hand
  that is 90% behind a coat pocket gets a 10% glove.
- **Already-gloved hands** — may receive a light enhancement rather than
  being skipped (provider-dependent).

CLEAN HANDS. DIRTY MONEY.
