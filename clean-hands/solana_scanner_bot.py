#!/usr/bin/env python3
"""
DEGEN SCANNER — Solana contract-address safety bot for a Telegram community.

Behavior (per your spec):
  1. AUTO-SCAN   — watches messages, detects any Solana mint address, scans it,
                   and posts a safety verdict reply.
  2. /scan <CA>  — on-demand check of any mint, usable by anyone.
  3. AUTO-DELETE — if a NON-admin posts a contract that scores HIGH risk / is
                   flagged rugged / has an active freeze authority (honeypot),
                   the message is deleted and a red warning is posted.

Data source: RugCheck public read API (api.rugcheck.xyz) — no key needed.
It owns Solana token safety: mint/freeze authority, LP lock, holder
concentration, metadata mutability, insider/sniper detection.

This is DEFENSIVE tooling — it protects members from rugs/honeypots. It is NOT
financial advice and a "low risk" score is NEVER a guarantee. Always DYOR.

Requires python-telegram-bot v21+ (bundles httpx):
    pip install "python-telegram-bot[job-queue]"

Run:
    export TG_SCANNER_TOKEN="123456:ABC..."   # a SECOND @BotFather bot
    export TG_ADMIN_IDS="11111111,22222222"   # admins exempt from auto-delete
    python solana_scanner_bot.py

Telegram setup: add as admin with DELETE permission; @BotFather /setprivacy -> Disable.

To MERGE into the Guardian bot instead of running separately: copy the three
handlers registered in main() into guardian_bot.py's main() and share one token.
"""

import os
import re
import time
import logging

import httpx
from telegram import Update, BotCommand
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --------------------------------------------------------------------------- #
#  CONFIG                                                                      #
# --------------------------------------------------------------------------- #
BOT_TOKEN = os.environ.get("TG_SCANNER_TOKEN", "")
ADMIN_IDS = {
    int(x) for x in os.environ.get("TG_ADMIN_IDS", "").split(",") if x.strip().isdigit()
}

# Risk bands on RugCheck's normalised score (lower = safer).
LOW_MAX = 30        # < 30  -> 🟢 looks ok
MED_MAX = 60        # 30-60 -> 🟡 caution; >60 -> 🔴 high risk
CACHE_TTL = 300     # seconds to cache a mint's verdict
RUGCHECK_BASE = "https://api.rugcheck.xyz/v1"

# Solana mint = base58, 32-44 chars (excludes 0 O I l). We validate by querying.
MINT_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

# Bluechips/stables legitimately keep mint/freeze authority (Circle, Tether, etc.)
# Never auto-delete these and never flag them HIGH on authority grounds alone.
SAFE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "So11111111111111111111111111111111111111112",   # wSOL
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",   # JUP
}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO
)
log = logging.getLogger("scanner")

_cache: dict[str, tuple[float, dict]] = {}


# --------------------------------------------------------------------------- #
#  RUGCHECK FETCH + VERDICT                                                    #
# --------------------------------------------------------------------------- #
async def fetch_report(mint: str) -> dict | None:
    now = time.time()
    if mint in _cache and now - _cache[mint][0] < CACHE_TTL:
        return _cache[mint][1]
    url = f"{RUGCHECK_BASE}/tokens/{mint}/report"
    headers = {"User-Agent": "degen-scanner/1.0", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url, headers=headers)
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, dict) or "score_normalised" not in data:
            return None
        _cache[mint] = (now, data)
        return data
    except Exception as e:  # noqa: BLE001
        log.warning("rugcheck fetch failed for %s: %s", mint, e)
        return None


def _fmt_usd(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit, div in (("M", 1e6), ("K", 1e3)):
        if n >= div:
            return f"${n/div:.1f}{unit}"
    return f"${n:.0f}"


def build_verdict(report: dict) -> tuple[str, bool, str]:
    """Return (band, should_delete, message_html)."""
    meta = report.get("tokenMeta") or {}
    name = meta.get("name") or "Unknown"
    symbol = meta.get("symbol") or "?"
    score = report.get("score_normalised")
    rugged = bool(report.get("rugged"))
    mint_auth = report.get("mintAuthority")
    freeze_auth = report.get("freezeAuthority")
    liq = report.get("totalMarketLiquidity")
    holders = report.get("totalHolders")
    top = report.get("topHolders") or []
    top_pct = top[0].get("pct") if top else None

    mint = report.get("mint") or meta.get("mint")
    is_known_safe = mint in SAFE_MINTS

    danger_risks = [
        r for r in (report.get("risks") or []) if r.get("level") == "danger"
    ]

    try:
        s = float(score)
    except (TypeError, ValueError):
        s = None

    # Hard deletes: the patterns that actually trap a buyer/seller.
    criticals = []
    warnings = []  # shown but not auto-delete on their own
    if not is_known_safe:
        if rugged:
            criticals.append("flagged as RUGGED")
        if freeze_auth:
            criticals.append("freeze authority ACTIVE (honeypot — your tokens can be frozen)")
        if mint_auth:
            warnings.append("mint authority ACTIVE (dev can mint more supply)")
        if isinstance(top_pct, (int, float)) and top_pct >= 80:
            warnings.append(f"top holder owns {top_pct:.0f}% of supply")

    if is_known_safe:
        band, emoji, should_delete = "LOW", "🟢", False
    elif criticals or (s is not None and s > MED_MAX):
        band, emoji, should_delete = "HIGH", "🔴", True
    elif warnings or (s is not None and s > LOW_MAX):
        band, emoji, should_delete = "MEDIUM", "🟡", False
    else:
        band, emoji, should_delete = "LOW", "🟢", False

    lines = [
        f"{emoji} <b>{band} RISK</b> — {name} (${symbol})",
        f"RugCheck score: <b>{score}</b> (lower = safer)",
    ]
    flags = []
    flags.append("mint revoked ✅" if not mint_auth else "mint ACTIVE ⚠️")
    flags.append("freeze revoked ✅" if not freeze_auth else "freeze ACTIVE ⚠️")
    lines.append(" · ".join(flags))
    if liq is not None:
        lines.append(f"Liquidity: {_fmt_usd(liq)} · Holders: {holders or '?'}")
    if isinstance(top_pct, (int, float)):
        lines.append(f"Top holder: {top_pct:.1f}%")
    if criticals:
        lines.append("‼️ " + "; ".join(criticals))
    if warnings:
        lines.append("⚠️ " + "; ".join(warnings))
    if not criticals and not warnings and danger_risks:
        lines.append("⚠️ " + "; ".join(d.get("name", "") for d in danger_risks[:3]))
    lines.append("<i>Not financial advice. A score is not a guarantee — DYOR.</i>")

    return band, should_delete, "\n".join(lines)


# --------------------------------------------------------------------------- #
#  HANDLERS                                                                    #
# --------------------------------------------------------------------------- #
async def _scan_and_report(mint: str, update: Update, context, from_admin: bool):
    report = await fetch_report(mint)
    if report is None:
        return False  # not a recognised token; stay silent on auto-scan
    band, should_delete, text = build_verdict(report)
    short = f"<code>{mint[:4]}…{mint[-4:]}</code>"

    if should_delete and not from_admin:
        try:
            await update.message.delete()
        except Exception:  # noqa: BLE001
            pass
        await context.bot.send_message(
            update.effective_chat.id,
            f"🔴 <b>Removed a high-risk contract</b> {short} posted by "
            f"{update.effective_user.mention_html()}.\n\n{text}",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
    return True


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not (msg.text or msg.caption):
        return
    from_admin = bool(update.effective_user and update.effective_user.id in ADMIN_IDS)
    text = msg.text or msg.caption
    seen = set()
    for m in MINT_RE.findall(text):
        if m in seen:
            continue
        seen.add(m)
        scanned = await _scan_and_report(m, update, context, from_admin)
        if scanned:
            break  # one verdict per message is enough


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /scan <solana_mint_address>")
        return
    mint = context.args[0].strip()
    if not MINT_RE.fullmatch(mint):
        await update.message.reply_text("That doesn't look like a Solana mint address.")
        return
    await update.message.reply_text("🔍 Scanning…")
    report = await fetch_report(mint)
    if report is None:
        await update.message.reply_text(
            "Couldn't find a token report for that address (not a token, or RugCheck has no data yet)."
        )
        return
    _, _, text = build_verdict(report)
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔍 Degen Scanner active. I auto-check every Solana contract address posted "
        "and remove high-risk ones from non-admins. Use /scan <mint> anytime."
    )


# --------------------------------------------------------------------------- #
#  BOOTSTRAP                                                                   #
# --------------------------------------------------------------------------- #
async def _post_init(app):
    """Auto-register the slash-command menu so you skip @BotFather /setcommands."""
    try:
        await app.bot.set_my_commands(
            [BotCommand("scan", "Safety-check a Solana mint: /scan <address>")]
        )
        log.info("command menu registered")
    except Exception as e:  # noqa: BLE001
        log.warning("set_my_commands failed: %s", e)


def main():
    if not BOT_TOKEN:
        raise SystemExit("Set TG_SCANNER_TOKEN (a second bot from @BotFather).")

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND & filters.ChatType.GROUPS,
            on_message,
        )
    )
    log.info("Degen Scanner starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
