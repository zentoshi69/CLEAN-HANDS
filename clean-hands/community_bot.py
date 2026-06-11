#!/usr/bin/env python3
"""
DEGEN COMMUNITY BOT — fun + utility commands for a crypto Telegram group.

Commands:
  /price  [mint]   — quick price, 24h change, market cap (DexScreener, no key).
  /stats  [mint]   — full readout: price, MC, FDV, liquidity, volume, changes.
  /chart  [mint]   — link to the live DexScreener chart.
  /meme   top | bottom   — REPLY to a photo with this to caption it meme-style.
  /sticker         — REPLY to a photo to convert it into a 512px sticker file.
  /help            — list commands.

If you set DEFAULT_TOKEN_MINT below, the stats commands work with no argument
(they default to YOUR coin); pass any mint to check another token.

Data: DexScreener public API (api.dexscreener.com) — free, no key.
Images: Pillow.

Requires:
    pip install "python-telegram-bot[job-queue]" Pillow httpx

Run:
    export TG_COMMUNITY_TOKEN="123456:ABC..."   # a @BotFather bot
    export DEFAULT_TOKEN_MINT="<your_coin_mint>" # optional
    python community_bot.py

@BotFather: /setprivacy -> Disable (so it can read /commands in groups), and
register the command list with /setcommands for the nice autocomplete menu.
"""

import io
import os
import glob
import math
import logging
import textwrap

import httpx
from PIL import Image, ImageDraw, ImageFont
from telegram import (
    Update,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
    MenuButtonWebApp,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------- #
#  CONFIG                                                                      #
# --------------------------------------------------------------------------- #
BOT_TOKEN = os.environ.get("TG_COMMUNITY_TOKEN", "")
DEFAULT_TOKEN_MINT = os.environ.get("DEFAULT_TOKEN_MINT", "").strip()
DEXS_BASE = "https://api.dexscreener.com/latest/dex/tokens"
# Mini App (staking / leaderboard / referrals). HTTPS URL of the deployed app.
MINIAPP_URL = os.environ.get("MINIAPP_URL", "").strip()
MINIAPP_SHORT_NAME = os.environ.get("MINIAPP_SHORT_NAME", "app").strip()

# Meme font: drop an Impact.ttf or Anton.ttf next to this file for the classic
# look; otherwise we fall back to a bundled bold font.
FONT_PATH = os.environ.get("MEME_FONT_PATH", "")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO
)
log = logging.getLogger("community")


def _find_font() -> str:
    if FONT_PATH and os.path.exists(FONT_PATH):
        return FONT_PATH
    for name in ("Impact.ttf", "Anton-Regular.ttf"):
        if os.path.exists(name):
            return name
    for pat in (
        "/usr/share/fonts/**/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/**/*Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ):
        hits = glob.glob(pat, recursive=True)
        if hits:
            return hits[0]
    return ""  # PIL default as last resort


# --------------------------------------------------------------------------- #
#  COIN STATS (DexScreener)                                                    #
# --------------------------------------------------------------------------- #
async def _best_pair(mint: str) -> dict | None:
    """Return the highest-liquidity trading pair for a mint, or None."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"{DEXS_BASE}/{mint}", headers={"User-Agent": "degen-community/1.0"}
            )
        if r.status_code != 200:
            return None
        pairs = (r.json() or {}).get("pairs") or []
        if not pairs:
            return None
        return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    except Exception as e:  # noqa: BLE001
        log.warning("dexscreener fetch failed: %s", e)
        return None


def _fmt_usd(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    a = abs(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if a >= div:
            return f"${n/div:.2f}{unit}"
    if a == 0:
        return "$0"
    if a < 1:  # memecoin sub-cent prices: keep ~4 significant figures
        decimals = min(12, max(4, 3 - int(math.floor(math.log10(a)))))
        return f"${n:.{decimals}f}"
    return f"${n:,.2f}"


def _arrow(pct) -> str:
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        return "—"
    sign = "🟢▲" if pct >= 0 else "🔴▼"
    return f"{sign} {pct:+.1f}%"


def _resolve_mint(context) -> str | None:
    if context.args:
        return context.args[0].strip()
    return DEFAULT_TOKEN_MINT or None


async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mint = _resolve_mint(context)
    if not mint:
        await update.message.reply_text("Usage: /price <mint> (or set DEFAULT_TOKEN_MINT)")
        return
    p = await _best_pair(mint)
    if not p:
        await update.message.reply_text("No trading data found for that token.")
        return
    sym = (p.get("baseToken") or {}).get("symbol", "?")
    ch = p.get("priceChange") or {}
    await update.message.reply_text(
        f"💰 <b>${sym}</b>  {_fmt_usd(p.get('priceUsd'))}\n"
        f"24h: {_arrow(ch.get('h24'))}  ·  MC: {_fmt_usd(p.get('marketCap'))}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mint = _resolve_mint(context)
    if not mint:
        await update.message.reply_text("Usage: /stats <mint> (or set DEFAULT_TOKEN_MINT)")
        return
    p = await _best_pair(mint)
    if not p:
        await update.message.reply_text("No trading data found for that token.")
        return
    base = p.get("baseToken") or {}
    sym = base.get("symbol", "?")
    name = base.get("name", "")
    ch = p.get("priceChange") or {}
    liq = (p.get("liquidity") or {}).get("usd")
    vol = (p.get("volume") or {}).get("h24")
    txt = (
        f"📊 <b>{name} (${sym})</b>\n"
        f"Price: <b>{_fmt_usd(p.get('priceUsd'))}</b>\n"
        f"Market cap: {_fmt_usd(p.get('marketCap'))}  ·  FDV: {_fmt_usd(p.get('fdv'))}\n"
        f"Liquidity: {_fmt_usd(liq)}  ·  24h vol: {_fmt_usd(vol)}\n"
        f"5m {_arrow(ch.get('m5'))}  |  1h {_arrow(ch.get('h1'))}  |  "
        f"6h {_arrow(ch.get('h6'))}  |  24h {_arrow(ch.get('h24'))}\n"
        f"<a href=\"{p.get('url')}\">Chart on DexScreener →</a>"
    )
    await update.message.reply_text(
        txt, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mint = _resolve_mint(context)
    if not mint:
        await update.message.reply_text("Usage: /chart <mint> (or set DEFAULT_TOKEN_MINT)")
        return
    p = await _best_pair(mint)
    if not p:
        await update.message.reply_text("No chart found for that token.")
        return
    await update.message.reply_text(f"📈 {p.get('url')}")


async def cmd_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick non-custodial swap links (Jupiter) — the bot never touches funds."""
    mint = _resolve_mint(context)
    if not mint:
        await update.message.reply_text("Usage: /trade <mint> (or set DEFAULT_TOKEN_MINT)")
        return
    p = await _best_pair(mint)
    sym = ((p or {}).get("baseToken") or {}).get("symbol", "token")
    jup = f"https://jup.ag/swap/SOL-{mint}"
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"🟢 Buy ${sym} on Jupiter", url=jup)],
            [
                InlineKeyboardButton("📊 Chart", url=(p or {}).get("url") or jup),
                InlineKeyboardButton("🦅 Birdeye", url=f"https://birdeye.so/token/{mint}?chain=solana"),
            ],
        ]
    )
    await update.message.reply_text(
        f"💱 Trade <b>${sym}</b> — non-custodial, your wallet, your keys.\n"
        f"<i>Always verify the mint matches the pinned official address.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
        disable_web_page_preview=True,
    )


# --------------------------------------------------------------------------- #
#  MEME GENERATOR (pure function, unit-testable)                               #
# --------------------------------------------------------------------------- #
MAX_PHOTO_BYTES = 8 * 1024 * 1024  # Telegram-compressed photos are far smaller
MAX_MEME_TEXT = 200  # per caption block


async def _download_photo(context, photo) -> bytes | None:
    """Fetch a replied-to photo, refusing anything over MAX_PHOTO_BYTES."""
    if photo.file_size and photo.file_size > MAX_PHOTO_BYTES:
        return None
    file = await context.bot.get_file(photo.file_id)
    return bytes(await file.download_as_bytearray())


def make_meme(img_bytes: bytes, top: str, bottom: str) -> bytes:
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    if img.width > 1080:  # normalise huge uploads
        ratio = 1080 / img.width
        img = img.resize((1080, int(img.height * ratio)))
    draw = ImageDraw.Draw(img)
    W, H = img.size
    font_path = _find_font()
    size = max(20, W // 10)
    font = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
    stroke = max(2, size // 12)

    def draw_block(text: str, at_top: bool):
        if not text:
            return
        text = text.upper()
        # wrap to ~ width
        avg_char = max(1, int(font.getlength("M")))
        max_chars = max(8, (W - 2 * stroke) // avg_char)
        lines = textwrap.wrap(text, width=max_chars) or [text]
        line_h = size + stroke * 2
        total_h = line_h * len(lines)
        y = (10 if at_top else H - total_h - 10)
        for ln in lines:
            w = font.getlength(ln)
            x = (W - w) / 2
            draw.text(
                (x, y), ln, font=font, fill="white",
                stroke_width=stroke, stroke_fill="black",
            )
            y += line_h

    draw_block(top, at_top=True)
    draw_block(bottom, at_top=False)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _photo_source(update: Update):
    """The photo to work on: the replied-to message, or the message itself
    when the command arrives as a photo caption (upload + '/meme ...')."""
    msg = update.message
    if msg.reply_to_message and msg.reply_to_message.photo:
        return msg.reply_to_message
    if msg.photo:
        return msg
    return None


async def _run_meme(update: Update, context: ContextTypes.DEFAULT_TYPE, photo_msg, args):
    raw = " ".join(args)
    if "|" in raw:
        top, bottom = (s.strip() for s in raw.split("|", 1))
    else:
        top, bottom = raw.strip(), ""
    top, bottom = top[:MAX_MEME_TEXT], bottom[:MAX_MEME_TEXT]
    buf = await _download_photo(context, photo_msg.photo[-1])
    if buf is None:
        await update.message.reply_text("That image is too large to meme.")
        return
    try:
        meme_png = make_meme(buf, top, bottom)
        try:
            # every meme leaves with the gloves on — brand stamp, bottom-right
            meme_png = add_glove(meme_png, "br", 0.2)
        except Exception:  # noqa: BLE001 — missing asset must not kill the meme
            pass
    except Exception as e:  # noqa: BLE001
        log.warning("meme failed: %s", e)
        await update.message.reply_text("Couldn't make that meme.")
        return
    await update.message.reply_photo(photo=io.BytesIO(meme_png))


async def cmd_meme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_msg = _photo_source(update)
    if not photo_msg:
        await update.message.reply_text(
            "Send a photo with caption  /meme top text | bottom text\n"
            "…or reply to a photo with the same command."
        )
        return
    await _run_meme(update, context, photo_msg, context.args)


# --------------------------------------------------------------------------- #
#  GLOVE SLAP (brand the image with the $CLEAN glove)                          #
# --------------------------------------------------------------------------- #
GLOVE_PATH = os.environ.get(
    "GLOVE_PATH", os.path.join(os.path.dirname(__file__), "assets", "glove.png")
)
_GLOVE_POSITIONS = {
    "br": (1.0, 1.0), "bl": (0.0, 1.0), "tr": (1.0, 0.0), "tl": (0.0, 0.0),
    "center": (0.5, 0.5), "c": (0.5, 0.5),
}


def add_glove(img_bytes: bytes, position: str = "br", scale: float = 0.45) -> bytes:
    """Overlay the transparent $CLEAN glove onto an image. position in
    {br,bl,tr,tl,center}; scale = glove height as a fraction of image height."""
    base = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    if base.width > 1280:
        r = 1280 / base.width
        base = base.resize((1280, int(base.height * r)))
    glove = Image.open(GLOVE_PATH).convert("RGBA")
    W, H = base.size
    scale = max(0.15, min(1.5, scale))
    gh = int(H * scale)
    gw = int(glove.width * (gh / glove.height))
    glove = glove.resize((max(1, gw), max(1, gh)))
    fx, fy = _GLOVE_POSITIONS.get(position.lower(), (1.0, 1.0))
    pad = int(min(W, H) * 0.02)
    x = int((W - glove.width) * fx)
    y = int((H - glove.height) * fy)
    # nudge edge placements inward by the padding
    if fx == 1.0:
        x -= pad
    elif fx == 0.0:
        x += pad
    if fy == 1.0:
        y -= pad
    elif fy == 0.0:
        y += pad
    base.alpha_composite(glove, (x, y))
    out = io.BytesIO()
    base.convert("RGB").save(out, format="PNG")
    return out.getvalue()


async def _run_glove(update: Update, context: ContextTypes.DEFAULT_TYPE, photo_msg, args):
    position, scale = "br", 0.45
    for a in args:
        al = a.lower()
        if al in _GLOVE_POSITIONS:
            position = al
        else:
            try:
                scale = float(a)
            except ValueError:
                pass
    raw = await _download_photo(context, photo_msg.photo[-1])
    if raw is None:
        await update.message.reply_text("That image is too large to glove.")
        return
    try:
        out = add_glove(raw, position, scale)
    except Exception as e:  # noqa: BLE001
        log.warning("glove failed: %s", e)
        await update.message.reply_text("Couldn't glove that image.")
        return
    await update.message.reply_photo(photo=io.BytesIO(out), caption="🧤 $CLEAN")


async def cmd_glove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_msg = _photo_source(update)
    if not photo_msg:
        await update.message.reply_text(
            "Send a photo with caption  /glove  to slap the $CLEAN glove on it "
            "(or reply to a photo).\n"
            "Options: /glove [br|bl|tr|tl|center] [scale 0.2-1.0]  e.g. /glove tl 0.6"
        )
        return
    await _run_glove(update, context, photo_msg, context.args)


# --------------------------------------------------------------------------- #
#  STICKER CONVERTER (pure function, unit-testable)                            #
# --------------------------------------------------------------------------- #
def to_sticker_webp(img_bytes: bytes) -> bytes:
    """Telegram static stickers: WEBP, one side exactly 512px, other <=512."""
    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    w, h = img.size
    scale = 512 / max(w, h)
    img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))))
    out = io.BytesIO()
    img.save(out, format="WEBP")
    return out.getvalue()


async def on_photo_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dispatch photos whose CAPTION is a command: upload + '/meme top | bottom'."""
    cap = (update.message.caption or "").strip()
    if not cap.startswith("/"):
        return
    parts = cap.split()
    cmd = parts[0][1:].split("@")[0].lower()
    args = parts[1:]
    if cmd == "meme":
        await _run_meme(update, context, update.message, args)
    elif cmd == "glove":
        await _run_glove(update, context, update.message, args)
    elif cmd == "sticker":
        await cmd_sticker(update, context)


async def cmd_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_msg = _photo_source(update)
    if not photo_msg:
        await update.message.reply_text(
            "Send a photo with caption  /sticker  (or reply to a photo) to convert it."
        )
        return
    raw = await _download_photo(context, photo_msg.photo[-1])
    if raw is None:
        await update.message.reply_text("That image is too large to convert.")
        return
    try:
        webp = to_sticker_webp(raw)
    except Exception as e:  # noqa: BLE001
        log.warning("sticker convert failed: %s", e)
        await update.message.reply_text("Couldn't convert that image.")
        return
    doc = io.BytesIO(webp)
    doc.name = "sticker.webp"
    await update.message.reply_document(
        document=doc,
        caption="✅ Sticker-ready (512px WEBP). Forward it to @Stickers → "
                "/addsticker to drop it into your community pack.",
    )


# --------------------------------------------------------------------------- #
#  MINI APP (staking / leaderboard / referrals)                               #
# --------------------------------------------------------------------------- #
async def cmd_app(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not MINIAPP_URL:
        await update.message.reply_text(
            "Mini App isn't configured yet. Set MINIAPP_URL to the deployed app URL."
        )
        return
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🧤 Open $CLEAN App", web_app=WebAppInfo(url=MINIAPP_URL))]]
    )
    await update.message.reply_text(
        "💎 Stake points, climb the leaderboard, and earn from referrals:",
        reply_markup=kb,
    )


async def cmd_stake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Same launcher, framed for staking.
    if not MINIAPP_URL:
        await update.message.reply_text("Mini App isn't configured yet (set MINIAPP_URL).")
        return
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📈 Stake in the App", web_app=WebAppInfo(url=MINIAPP_URL))]]
    )
    await update.message.reply_text("Lock points for yield in the $CLEAN App:", reply_markup=kb)


async def cmd_invite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uname = context.bot.username
    uid = update.effective_user.id
    link = f"https://t.me/{uname}/{MINIAPP_SHORT_NAME}?startapp={uid}"
    await update.message.reply_text(
        "🤝 Your referral link — every friend who opens the app from it earns you points:\n"
        f"{link}",
        disable_web_page_preview=True,
    )


# --------------------------------------------------------------------------- #
#  HELP / BOOTSTRAP                                                            #
# --------------------------------------------------------------------------- #
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Commands:\n"
        "/app — open the $CLEAN App (stake · leaderboard · invite)\n"
        "/stake — stake points for yield\n"
        "/invite — your referral link\n"
        "/price [mint] — quick price + 24h\n"
        "/stats [mint] — full readout\n"
        "/chart [mint] — live chart link\n"
        "/trade [mint] — buy/sell links (Jupiter)\n"
        "/meme top | bottom — reply to a photo\n"
        "/glove — reply to a photo to slap the $CLEAN glove on it\n"
        "/sticker — reply to a photo to make a sticker\n"
    )


async def _post_init(app):
    """Auto-register the slash-command menu (skip @BotFather /setcommands) and,
    if a Mini App URL is set, wire the chat menu button to open it."""
    cmds = [
        BotCommand("app", "Open the $CLEAN App (stake, leaderboard, invite)"),
        BotCommand("stake", "Stake points for yield"),
        BotCommand("invite", "Get your referral link"),
        BotCommand("price", "Quick price + 24h change"),
        BotCommand("stats", "Full token readout"),
        BotCommand("chart", "Live DexScreener chart"),
        BotCommand("trade", "Buy/sell links (Jupiter, non-custodial)"),
        BotCommand("meme", "Reply to a photo: /meme top | bottom"),
        BotCommand("glove", "Reply to a photo to slap the $CLEAN glove on it"),
        BotCommand("sticker", "Reply to a photo to make a sticker"),
        BotCommand("help", "Show commands"),
    ]
    try:
        await app.bot.set_my_commands(cmds)
        log.info("command menu registered")
    except Exception as e:  # noqa: BLE001
        log.warning("set_my_commands failed: %s", e)
    if MINIAPP_URL.startswith("https://"):
        try:
            await app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="🧤 App", web_app=WebAppInfo(url=MINIAPP_URL)
                )
            )
            log.info("mini app menu button set")
        except Exception as e:  # noqa: BLE001
            log.warning("set_chat_menu_button failed: %s", e)


def main():
    if not BOT_TOKEN:
        raise SystemExit("Set TG_COMMUNITY_TOKEN (from @BotFather).")
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("app", cmd_app))
    app.add_handler(CommandHandler("stake", cmd_stake))
    app.add_handler(CommandHandler("invite", cmd_invite))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("chart", cmd_chart))
    app.add_handler(CommandHandler(["trade", "buy", "sell"], cmd_trade))
    app.add_handler(CommandHandler("meme", cmd_meme))
    app.add_handler(CommandHandler("glove", cmd_glove))
    app.add_handler(CommandHandler("sticker", cmd_sticker))
    # photos uploaded WITH the command as caption — the natural mobile flow
    app.add_handler(
        MessageHandler(
            filters.PHOTO & filters.CaptionRegex(r"^/(meme|glove|sticker)\b"),
            on_photo_caption,
        )
    )
    log.info("Degen Community bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
