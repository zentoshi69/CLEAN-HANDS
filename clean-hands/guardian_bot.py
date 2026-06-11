#!/usr/bin/env python3
"""
DEGEN GUARDIAN — a security-first gatekeeper bot for a crypto Telegram community.

What it does (all defensive / member-protection):
  1. JOIN GATE      — every new member is muted on entry and must pass a
                      button CAPTCHA within a timeout, or they're removed.
  2. ANTI-SCAM      — unverified + brand-new members can't post links, @mentions,
                      forwards, or known scam phrases. Violations are deleted.
  3. IMPERSONATION  — flags non-admins whose name/username mimics an admin
                      (the #1 way members get drained in crypto groups).
  4. ADMIN TOOLS    — /ban /mute /unmute /warn /scan-style helpers for mods.

Requires python-telegram-bot v21+ with the job-queue extra:
    pip install "python-telegram-bot[job-queue]"

Run:
    export TG_BOT_TOKEN="123456:ABC..."     # from @BotFather
    export TG_ADMIN_IDS="11111111,22222222" # your numeric user id(s)
    python guardian_bot.py

IMPORTANT TELEGRAM SETUP:
  - Add the bot to your group and promote it to admin with at least:
    Delete messages, Ban users, Restrict members.
  - In @BotFather: /setprivacy -> Disable, so the bot can read group messages
    for spam scanning. (Re-enable later if you move scanning to a webhook.)
"""

import os
import re
import html
import logging
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from telegram import (
    Update,
    BotCommand,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# --------------------------------------------------------------------------- #
#  CONFIG — tune these for your community                                      #
# --------------------------------------------------------------------------- #
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
ADMIN_IDS = {
    int(x) for x in os.environ.get("TG_ADMIN_IDS", "").split(",") if x.strip().isdigit()
}

VERIFY_TIMEOUT_SECONDS = 90        # time to solve the CAPTCHA before kick
NEWBIE_LINK_BLOCK_HOURS = 24       # how long after join links are blocked
IMPERSONATION_THRESHOLD = 0.85     # name-similarity ratio that triggers a flag

# Phrases that are almost always scam/drainer bait in a crypto group.
SCAM_PATTERNS = [
    r"\bdm me\b", r"\bdirect message\b", r"\bclaim your\b", r"\bairdrop\b.*\bclaim",
    r"\bconnect (your )?wallet\b", r"\bseed phrase\b", r"\bprivate key\b",
    r"\bvalidate\b.*\bwallet\b", r"\bsync\b.*\bwallet\b", r"\bdouble your\b",
    r"\bguaranteed (returns|profit)\b", r"\bsupport team\b.*\b(dm|message)\b",
    r"\bt\.me/[a-z0-9_]+_?support\b", r"\bcustomer (care|support)\b",
]
SCAM_RE = re.compile("|".join(SCAM_PATTERNS), re.IGNORECASE)
URL_RE = re.compile(r"(https?://|t\.me/|www\.)", re.IGNORECASE)

# Wallet-drainer / phishing domain blocklist. Loaded from a file so you can
# update it without touching code. Substring match against lower-cased text.
BLOCKLIST_PATH = os.environ.get(
    "GUARDIAN_BLOCKLIST", os.path.join(os.path.dirname(__file__), "blocklists", "drainer-domains.txt")
)
# Punycode / IDN homograph domains (xn--) are a classic look-alike phishing
# trick (e.g. "binаnce" with a Cyrillic а). Treat any such link as hostile.
PUNYCODE_RE = re.compile(r"\bxn--", re.IGNORECASE)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO
)
log = logging.getLogger("guardian")


def _load_blocklist(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as fh:
            out = []
            for line in fh:
                line = line.strip().lower()
                if line and not line.startswith("#"):
                    out.append(line)
            log.info("loaded %d drainer-blocklist entries from %s", len(out), path)
            return out
    except FileNotFoundError:
        log.warning("blocklist not found at %s — drainer domain check disabled", path)
        return []
    except Exception as e:  # noqa: BLE001
        log.warning("blocklist load failed: %s", e)
        return []


DRAINER_TERMS = _load_blocklist(BLOCKLIST_PATH)


def drainer_hit(text: str) -> str | None:
    """Return the matched blocklist term / reason if text contains a known
    drainer/phishing signal. Applies to EVERYONE non-admin, not just newbies —
    this is the vector that drains holders."""
    if not text:
        return None
    low = text.lower()
    if PUNYCODE_RE.search(low):
        return "punycode/IDN look-alike domain (xn--)"
    for term in DRAINER_TERMS:
        if term in low:
            return f"blocklisted phishing pattern: {term}"
    return None


# --------------------------------------------------------------------------- #
#  STATE (in-memory; swap for Redis/DB in production)                          #
# --------------------------------------------------------------------------- #
@dataclass
class ChatState:
    pending: dict = field(default_factory=dict)   # user_id -> join unix-time
    verified: set = field(default_factory=set)     # user_id
    joined_at: dict = field(default_factory=dict)  # user_id -> unix-time
    warns: dict = field(default_factory=dict)      # user_id -> int
    admin_names: set = field(default_factory=set)   # lowercased admin names/usernames


STATE: dict[int, ChatState] = {}


def st(chat_id: int) -> ChatState:
    return STATE.setdefault(chat_id, ChatState())


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


async def refresh_admin_names(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Cache admin display names + usernames to detect impersonators."""
    names = set()
    try:
        for adm in await context.bot.get_chat_administrators(chat_id):
            u = adm.user
            if u.full_name:
                names.add(u.full_name.lower())
            if u.username:
                names.add(u.username.lower())
    except Exception as e:  # noqa: BLE001
        log.warning("admin name refresh failed: %s", e)
    st(chat_id).admin_names = names


# --------------------------------------------------------------------------- #
#  JOIN GATE                                                                   #
# --------------------------------------------------------------------------- #
async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = msg.chat
    state = st(chat.id)
    if not state.admin_names:
        await refresh_admin_names(chat.id, context)

    for member in msg.new_chat_members:
        if member.is_bot:
            continue
        uid = member.id

        # Impersonation check at the door.
        flagged = _impersonation_match(member, state)
        if flagged:
            await _notify_admins(
                context, chat.id,
                f"⚠️ Possible impersonator joined: {member.mention_html()} "
                f"resembles admin '{html.escape(flagged)}'. Watch closely.",
            )

        # Mute until verified.
        try:
            await context.bot.restrict_chat_member(
                chat.id, uid, ChatPermissions(can_send_messages=False)
            )
        except Exception as e:  # noqa: BLE001
            log.warning("could not restrict %s: %s", uid, e)

        state.pending[uid] = msg.date.timestamp()
        state.joined_at[uid] = msg.date.timestamp()

        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ I'm human — let me in", callback_data=f"verify:{uid}")]]
        )
        prompt = await context.bot.send_message(
            chat.id,
            f"👋 Welcome {member.mention_html()}!\n\n"
            f"This is a security-gated community. Tap the button within "
            f"{VERIFY_TIMEOUT_SECONDS}s to prove you're human. "
            f"Admins will <b>never</b> DM you first or ask for a seed phrase.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )

        # Schedule the kick-if-unverified job.
        context.job_queue.run_once(
            _kick_unverified,
            VERIFY_TIMEOUT_SECONDS,
            data={"chat_id": chat.id, "user_id": uid, "prompt_id": prompt.message_id},
            name=f"kick:{chat.id}:{uid}",
        )


def _impersonation_match(member, state: ChatState):
    candidates = [member.full_name or ""]
    if member.username:
        candidates.append(member.username)
    for cand in candidates:
        for adm_name in state.admin_names:
            if cand and similar(cand, adm_name) >= IMPERSONATION_THRESHOLD:
                return adm_name
    return None


async def _kick_unverified(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    chat_id, uid, prompt_id = d["chat_id"], d["user_id"], d["prompt_id"]
    state = st(chat_id)
    if uid in state.pending:
        try:
            await context.bot.ban_chat_member(chat_id, uid)
            await context.bot.unban_chat_member(chat_id, uid)  # kick, not permaban
        except Exception as e:  # noqa: BLE001
            log.warning("kick failed: %s", e)
        state.pending.pop(uid, None)
        try:
            await context.bot.delete_message(chat_id, prompt_id)
        except Exception:  # noqa: BLE001
            pass


async def on_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = (query.data or "").split(":")
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await query.answer("This button has expired.", show_alert=True)
        return
    target_uid = int(parts[1])
    presser = query.from_user.id

    if presser != target_uid:
        await query.answer("This button isn't for you.", show_alert=True)
        return

    chat_id = query.message.chat.id
    state = st(chat_id)
    state.pending.pop(target_uid, None)
    state.verified.add(target_uid)

    # Restore full posting permissions.
    try:
        await context.bot.restrict_chat_member(
            chat_id, target_uid,
            ChatPermissions(
                can_send_messages=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("unrestrict failed: %s", e)

    await query.answer("Verified — welcome aboard. 🛡️")
    try:
        await query.message.delete()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
#  ANTI-SCAM MESSAGE FILTER                                                     #
# --------------------------------------------------------------------------- #
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # effective_message also covers EDITED messages — closes the "post clean,
    # pass CAPTCHA, then edit in a phishing link" bypass.
    msg = update.effective_message
    if not msg or not msg.from_user:
        return
    chat_id = msg.chat.id
    uid = msg.from_user.id
    if is_admin(uid):
        return

    state = st(chat_id)
    text = msg.text or msg.caption or ""
    now = msg.date.timestamp()
    age_h = (now - state.joined_at.get(uid, now)) / 3600.0
    is_newbie = age_h < NEWBIE_LINK_BLOCK_HOURS

    reason = None
    # Drainer/phishing links are deleted from ANYONE non-admin (verified or not,
    # old or new) — a phished or compromised member is the #1 way holders lose funds.
    drainer = drainer_hit(text)
    if drainer:
        reason = f"wallet-drainer / phishing link ({drainer})"
    elif SCAM_RE.search(text):
        reason = "matched a known scam/drainer pattern"
    elif is_newbie and (URL_RE.search(text) or msg.entities and
                        any(e.type in ("url", "text_link", "mention") for e in msg.entities)):
        reason = "new member posting links/mentions (blocked for first 24h)"
    elif is_newbie and (msg.forward_origin is not None):
        reason = "new member forwarding messages (blocked for first 24h)"

    if reason:
        try:
            await msg.delete()
        except Exception:  # noqa: BLE001
            pass
        state.warns[uid] = state.warns.get(uid, 0) + 1
        count = state.warns[uid]
        await _notify_admins(
            context, chat_id,
            f"🚫 Removed message from {msg.from_user.mention_html()} "
            f"({reason}). Warning {count}/3.",
        )
        if count >= 3:
            try:
                await context.bot.ban_chat_member(chat_id, uid)
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
#  ADMIN COMMANDS                                                              #
# --------------------------------------------------------------------------- #
async def _require_admin(update: Update) -> bool:
    return bool(update.effective_user and is_admin(update.effective_user.id))


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛡️ Degen Guardian active. Add me as admin (Delete + Ban + Restrict). "
        "New joiners get a CAPTCHA gate; links/scam phrases from fresh accounts "
        "are auto-removed; admin impersonators get flagged."
    )


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📜 Survival rules:\n"
        "• Admins NEVER DM first. Anyone who does is a scammer.\n"
        "• No one legit will ever ask for your seed phrase or to 'connect wallet' in DMs.\n"
        "• 'Support' accounts in DMs = drainers. Report and block.\n"
        "• Verify contract addresses only from the pinned message."
    )


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        await context.bot.ban_chat_member(update.message.chat.id, target.id)
        await update.message.reply_text(f"Banned {target.full_name}.")


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        await context.bot.restrict_chat_member(
            update.message.chat.id, target.id,
            ChatPermissions(can_send_messages=False),
        )
        await update.message.reply_text(f"Muted {target.full_name}.")


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        await context.bot.restrict_chat_member(
            update.message.chat.id, target.id,
            ChatPermissions(
                can_send_messages=True, can_send_polls=True,
                can_send_other_messages=True, can_add_web_page_previews=True,
            ),
        )
        await update.message.reply_text(f"Unmuted {target.full_name}.")


async def cmd_refresh_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    await refresh_admin_names(update.message.chat.id, context)
    await update.message.reply_text("Admin impersonation cache refreshed.")


# --------------------------------------------------------------------------- #
#  ONE-SHOT GROUP SETUP + RAID CONTROLS (the bot configures the group for you) #
# --------------------------------------------------------------------------- #
# Default rights for a normal (verified) member: can chat, but CANNOT add others
# (admins-only adds) — that's the can_invite_users=False below.
MEMBER_PERMS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_invite_users=False,
)
LOCKDOWN_PERMS = ChatPermissions(can_send_messages=False)

RULES_TEXT = (
    "📌 <b>Community Rules — read this</b>\n\n"
    "🚨 <b>Admins will NEVER DM you first.</b> Anyone who DMs you offering support, "
    "an airdrop, or to 'verify/connect your wallet' is a scammer. Block & report.\n"
    "• Never share your seed phrase or private key. No one legit ever needs it.\n"
    "• Only trust contract addresses posted in the official channel.\n"
    "• New members are verified by the Guardian bot before they can post.\n"
    "• Suspicious contracts? The Scanner bot auto-checks every address posted here."
)


async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: configure everything the Bot API allows in one shot."""
    if not await _require_admin(update):
        await update.message.reply_text("Admins only.")
        return
    chat = update.effective_chat
    results = []

    try:
        await context.bot.set_chat_permissions(chat.id, MEMBER_PERMS)
        results.append("✅ 'Add members' locked to admins only")
    except Exception as e:  # noqa: BLE001
        results.append(f"⚠️ Couldn't set permissions: {e}")

    try:
        m = await context.bot.send_message(
            chat.id, RULES_TEXT, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
        await context.bot.pin_chat_message(chat.id, m.message_id, disable_notification=True)
        results.append("✅ Posted & pinned the safety rules")
    except Exception as e:  # noqa: BLE001
        results.append(f"⚠️ Couldn't pin rules (grant me 'Pin Messages'): {e}")

    await refresh_admin_names(chat.id, context)
    results.append("✅ Loaded admin list (impersonation guard armed)")

    try:
        link = await context.bot.create_chat_invite_link(chat.id, creates_join_request=False)
        results.append(f"✅ Fresh invite link:\n{link.invite_link}")
    except Exception as e:  # noqa: BLE001
        results.append(f"⚠️ Couldn't create invite link: {e}")

    results.append(
        "\nℹ️ Owner-only toggles I can't set via the Bot API — do these by hand once:"
        "\n• Slow Mode 30s · Anonymous Admin ON · link a Discussion channel"
    )
    await update.message.reply_text(
        "🛠️ <b>Setup complete</b>\n" + "\n".join(results),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cmd_lockdown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only panic button: mute the whole group during a raid."""
    if not await _require_admin(update):
        return
    try:
        await context.bot.set_chat_permissions(update.effective_chat.id, LOCKDOWN_PERMS)
        await update.message.reply_text("🔒 Lockdown ON — only admins can post. /unlock to lift.")
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"Couldn't lock down: {e}")


async def cmd_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: lift a lockdown, restoring normal member permissions."""
    if not await _require_admin(update):
        return
    try:
        await context.bot.set_chat_permissions(update.effective_chat.id, MEMBER_PERMS)
        await update.message.reply_text("🔓 Lockdown lifted — members can post again.")
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"Couldn't unlock: {e}")


# --------------------------------------------------------------------------- #
#  HELPERS                                                                     #
# --------------------------------------------------------------------------- #
async def _notify_admins(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str):
    """Post a flag in-chat (admins see it). For DMs to admins, loop ADMIN_IDS."""
    try:
        await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
    except Exception as e:  # noqa: BLE001
        log.warning("admin notify failed: %s", e)


# --------------------------------------------------------------------------- #
#  BOOTSTRAP                                                                   #
# --------------------------------------------------------------------------- #
async def _post_init(app):
    """Auto-register the slash-command menu so you skip @BotFather /setcommands."""
    try:
        await app.bot.set_my_commands(
            [
                BotCommand("setup", "One-shot: lock down + pin rules + invite link"),
                BotCommand("lockdown", "Panic: mute everyone (raid control)"),
                BotCommand("unlock", "Lift a lockdown"),
                BotCommand("refreshadmins", "Reload the admin list"),
                BotCommand("ban", "Reply to a user to ban them"),
                BotCommand("mute", "Reply to a user to mute them"),
                BotCommand("unmute", "Reply to a user to unmute them"),
                BotCommand("rules", "Show the safety rules"),
            ]
        )
        log.info("command menu registered")
    except Exception as e:  # noqa: BLE001
        log.warning("set_my_commands failed: %s", e)


def main():
    if not BOT_TOKEN:
        raise SystemExit("Set TG_BOT_TOKEN (get it from @BotFather).")
    if not ADMIN_IDS:
        log.warning("No TG_ADMIN_IDS set — admin commands will be inert.")

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("setup", cmd_setup))
    app.add_handler(CommandHandler("lockdown", cmd_lockdown))
    app.add_handler(CommandHandler("unlock", cmd_unlock))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(CommandHandler("refreshadmins", cmd_refresh_admins))

    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members)
    )
    app.add_handler(CallbackQueryHandler(on_verify, pattern=r"^verify:"))
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND & filters.ChatType.GROUPS,
            on_message,
        )
    )

    log.info("Degen Guardian starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
